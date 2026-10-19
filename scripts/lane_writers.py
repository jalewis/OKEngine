#!/usr/bin/env python3
"""Per-lane write servers in a deployment's ``.hermes-data/config.yaml`` (okengine#1029).

Every contracted model lane writes through its own ``okengine-write-<lane>`` MCP server, bound to
``OKENGINE_WRITE_ACTOR: cron:<lane>``. ensure-runtime registers those servers. It used to derive
the lane set from the engine checkout's shared ``config/cron-plus-jobs.json``, which holds
whichever pack was split LAST in that checkout. Every host therefore gained the previous host's
lanes, and since the reconciler only ever added, foreign identities piled up across the fleet.

This module derives the set from THIS deployment alone: its own fleet, built in memory from the
pack's crons and enabled extensions (``cron_pack_split.build_fleet``), limited to the lanes it
runs (``cron_pack_split.runs``, the same filter the deployed jobs are dumped with), plus the ``cron:``
capabilities of its deploy-materialized policy. It never reads the shared artifact, so it is
correct on a fresh deploy (nothing deployed yet) and independent of what another deploy split.

Reconciling also REMOVES an engine-generated lane writer whose lane is not in that set, so the
next deploy cleans out entries an earlier deploy left, and RE-DERIVES the two values a generated
writer copies from its lane, ``OKENGINE_OUTPUT_CONTRACT_MODE`` and ``OKENGINE_WRITE_TOOLS``
(okengine#1042 [3]/[18]). Registering a writer once and never revisiting it left a pack release
that widened or narrowed a lane's ``write_tools``, or added or dropped its contract, silently
unapplied: write_server takes OKENGINE_WRITE_TOOLS as the lane's whole tool surface. A block is engine-generated only when its
whole shape says so: an ``okengine-write-*`` name the engine derives from the block's own
``cron:`` actor, run as the baked ``write_server.py`` under the baked interpreter. The baseline
writers (``okengine-write``, the chat writer, the source-quality writer) and any server an
operator added are never removed.

Usage:
    lane_writers.py --pack <deployment> [--config <config.yaml>] [--dry-run]

``--dry-run`` writes nothing and prints the writers that would be added, re-derived, retired and
removed, as JSON. ensure-runtime.sh runs it without ``--dry-run``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from json import JSONDecodeError
from pathlib import Path
from typing import Optional

import yaml

SCRIPTS = Path(__file__).resolve().parent
CPS_PATH = SCRIPTS / "cron_pack_split.py"
WRITER_PYTHON = "/opt/hermes/.venv/bin/python"
WRITER_SCRIPT = "/opt/hermes/okengine-mcp/write_server.py"
_SERVER_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(?:#.*)?\Z")
_MCP_RE = re.compile(r"^mcp_servers\s*:\s*(?:#.*)?\Z")
_TOP_LEVEL_RE = re.compile(r"^[^\s#]")
CONTRACT_KEY, TOOLS_KEY = "OKENGINE_OUTPUT_CONTRACT_MODE", "OKENGINE_WRITE_TOOLS"
_MANAGED_RE = re.compile(rf"^      (?:{CONTRACT_KEY}|{TOOLS_KEY})\s*:")
_ACTOR_LINE_RE = re.compile(r"^      OKENGINE_WRITE_ACTOR\s*:")


def load_split():
    """cron_pack_split, loaded by path. The writer name is derived in exactly one place
    (contract_writer_name), so a load failure is fatal: a silently restated rule is the drift
    okengine#840 removed."""
    try:
        spec = importlib.util.spec_from_file_location("okengine_cron_pack_split", CPS_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        raise SystemExit(f"ERROR: cannot load the writer-name derivation from {CPS_PATH}: {exc}")


def reserved_writers(cps) -> frozenset:
    """Servers ensure-runtime registers under a fixed name on every deployment."""
    return frozenset({"okengine-write", *cps.BASELINE_WRITERS})


def load_policy(pack: Path) -> dict:
    path = Path(pack) / ".okengine" / "effective-policy.json"
    if not path.is_file():
        return {}
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, JSONDecodeError):
        raise SystemExit("ERROR: cannot read deploy-materialized effective policy")
    return policy if isinstance(policy, dict) else {}


def deployment_jobs(pack: Path, cps) -> Optional[list]:
    """THIS deployment's fleet, or None for a directory that is not a deployment.

    A directory with neither ``pack.yaml`` nor ``crons/`` is a bare runtime being seeded; it has
    no lane set to render from, so nothing is added from jobs and nothing is pruned."""
    pack = Path(pack)
    if not (pack / "pack.yaml").is_file() and not (pack / "crons").is_dir():
        return None
    try:
        jobs, _summary = cps.build_fleet(pack)
    except SystemExit as exc:
        raise SystemExit(f"ERROR: cannot generate this deployment's cron fleet for writer "
                         f"reconciliation:\n{exc}")
    except Exception as exc:
        raise SystemExit(f"ERROR: cannot generate this deployment's cron fleet for writer "
                         f"reconciliation: {type(exc).__name__}: {exc}")
    return jobs


def wanted_actors(jobs: Optional[list], policy: dict, cps) -> tuple:
    """``({actor: enforce_contract}, {actor: write_tools})`` for this deployment.

    An actor comes from a policy ``cron:`` capability, a contracted model lane, or a lane that
    narrows its tools with ``write_tools`` (forwarded as OKENGINE_WRITE_TOOLS, okengine#664).
    Only lanes the deployment RUNS count (``cps.runs``, the filter the deployed artifact is
    dumped with): ensure-runtime passes the whole in-memory fleet, disabled lanes included, while
    the jobs deploy gate passes the deployed artifact, which never holds them. Filtering here
    makes both sides agree, so a disabled lane gets no writer and an existing one is pruned."""
    actors = {a: False for a in (policy.get("capabilities") or {})
              if isinstance(a, str) and a.startswith("cron:")}
    write_tools = {}
    for job in jobs or []:
        if not isinstance(job, dict) or not cps.runs(job):
            continue
        actor = f"cron:{job.get('name')}"
        if isinstance(job.get("output_contract"), dict) and not job.get("no_agent"):
            actors[actor] = True
        # The fleet gate's own parse (okengine#1044 [8]/[18]): a malformed declaration refuses
        # here too, never reads as "no narrowing" and re-derives the writer to the full surface.
        tools, error = cps.write_tools_declaration(job)
        if error:
            raise SystemExit(f"ERROR: lane {job.get('name')!r}: {error}")
        if tools:
            write_tools[actor] = tools
            actors.setdefault(actor, False)
    return actors, write_tools


def generated_lane_actor(name: str, server, cps) -> Optional[str]:
    """The ``cron:`` actor of an ENGINE-GENERATED lane writer, else None.

    Generated means: not a reserved baseline name, the name is one the engine derives (bounded or
    pre-#840) from the block's own actor, and the block runs the baked write_server under the
    baked interpreter. A hand-added server differs in at least one of those, and is kept."""
    if name in reserved_writers(cps) or not isinstance(server, dict):
        return None
    env = server.get("env")
    actor = env.get("OKENGINE_WRITE_ACTOR") if isinstance(env, dict) else None
    if not isinstance(actor, str) or not actor.startswith("cron:"):
        return None
    lane = actor[len("cron:"):]
    if name not in (cps.contract_writer_name(lane), cps.unbounded_writer_name(lane)):
        return None
    if server.get("command") != WRITER_PYTHON or server.get("args") != [WRITER_SCRIPT]:
        return None
    return actor


def unwanted_lane_writers(runtime_config: dict, actors, cps) -> list:
    """``[(server, actor)]``: engine-generated lane writers whose lane this deployment lacks."""
    servers = (runtime_config or {}).get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    out = []
    for name, server in servers.items():
        actor = generated_lane_actor(str(name), server, cps)
        if actor is not None and actor not in actors:
            out.append((str(name), actor))
    return sorted(out)


def managed_env(actor: str, actors: dict, write_tools: dict) -> dict:
    """The env values a generated writer copies from its lane: ``{key: value or None}``."""
    return {CONTRACT_KEY: "enforce" if actors.get(actor) else None,
            TOOLS_KEY: write_tools.get(actor)}


def _tool_set(value) -> frozenset:
    """OKENGINE_WRITE_TOOLS as write_server reads it: comma-separated, blanks ignored."""
    return frozenset(t.strip() for t in str(value or "").split(",") if t.strip())


def env_drift(server, expected: dict) -> list:
    """``[(key, have, want)]`` where a generated writer's env disagrees with its lane. Tool lists
    compare as sets, the way write_server applies them, so a reordering is not drift."""
    env = server.get("env") if isinstance(server, dict) else None
    env = env if isinstance(env, dict) else {}
    out = []
    if env.get(CONTRACT_KEY) != expected[CONTRACT_KEY]:
        out.append((CONTRACT_KEY, env.get(CONTRACT_KEY), expected[CONTRACT_KEY]))
    if _tool_set(env.get(TOOLS_KEY)) != _tool_set(expected[TOOLS_KEY]):
        out.append((TOOLS_KEY, env.get(TOOLS_KEY), expected[TOOLS_KEY]))
    return out


def drifted_lane_writers(runtime_config: dict, actors: dict, write_tools: dict, cps) -> list:
    """``[(server, actor, drift)]``: generated writers of wanted lanes, under the lane's current
    name, whose contract mode or tool list no longer matches the lane (okengine#1042 [3]/[18])."""
    servers = (runtime_config or {}).get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    out = []
    for name, server in servers.items():
        actor = generated_lane_actor(str(name), server, cps)
        if actor is None or actor not in actors \
                or name != cps.contract_writer_name(actor[len("cron:"):]):
            continue
        drift = env_drift(server, managed_env(actor, actors, write_tools))
        if drift:
            out.append((str(name), actor, drift))
    return sorted(out)


def stale_writer_errors(runtime_config: dict, jobs: list, policy: dict, cps) -> list:
    """Deploy gate: a registered lane writer must have a lane in this deployment's job set, and
    serve that lane's current contract mode and tool list."""
    actors, write_tools = wanted_actors(jobs, policy, cps)
    errors = [f"write server {name!r} is registered for {actor!r}, which is not a lane of this "
              f"deployment (a leftover from another pack's split or a disabled extension) -- run "
              f"ensure-runtime.sh to remove it and recreate the gateway"
              for name, actor in unwanted_lane_writers(runtime_config, actors, cps)]
    for name, actor, drift in drifted_lane_writers(runtime_config, actors, write_tools, cps):
        detail = "; ".join(f"{key} is {have!r}, the lane declares {want!r}"
                           for key, have, want in drift)
        errors.append(f"write server {name!r} for {actor!r} is stale against its lane ({detail}) "
                      f"-- run ensure-runtime.sh to re-derive it and recreate the gateway")
    return errors


def _parsed_servers(text: str) -> dict:
    """``mcp_servers`` as YAML reads it; empty when the config does not parse, so an unreadable
    config is never pruned on a guess."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    servers = doc.get("mcp_servers") if isinstance(doc, dict) else None
    return servers if isinstance(servers, dict) else {}


def _chunks(body: list) -> list:
    """``(start, end)`` spans of the mcp_servers body. A span starts at the first line and at every
    non-blank line indented two columns or less (a server, a marker comment, a column-0 comment),
    so a server's span is exactly its header plus everything nested under it."""
    if not body:
        return []
    cuts = [0] + [i for i, line in enumerate(body[1:], 1)
                  if line.strip() and not line.startswith("   ")]
    return list(zip(cuts, cuts[1:] + [len(body)]))


def _marker(actor: str) -> str:
    return f"# Engine-managed server-bound identity for {actor}."


def _render(name: str, actor: str, enforce: bool, write_tools: Optional[str]) -> str:
    return (f"  {_marker(actor)}\n"
            f"  {name}:\n"
            f"    command: {WRITER_PYTHON}\n"
            "    args:\n"
            f"    - {WRITER_SCRIPT}\n"
            "    tools:\n"
            "      resources: false\n"
            "      prompts: false\n"
            "    env:\n"
            f"      OKENGINE_WRITE_ACTOR: {actor}\n"
            "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n"
            "      OKENGINE_CRON_JOBS: /opt/data/cron-plus/jobs.json\n"
            + ("      OKENGINE_OUTPUT_CONTRACT_MODE: enforce\n" if enforce else "")
            + (f"      OKENGINE_WRITE_TOOLS: {write_tools}\n" if write_tools else "")
            + "\n")


def _disposition(name: str, actor: Optional[str], actors: dict, known: bool, cps):
    """Return "removed", "retired", or None (keep) for one server block."""
    if actor is None:
        return None                       # not engine-generated: never ours to remove
    if actor not in actors:
        return "removed" if known else None   # an unknown lane set is never pruned on a guess
    if name != cps.contract_writer_name(actor[len("cron:"):]):
        # The engine's own pre-#840 name for a lane whose writer is now bounded. Left in place it
        # keeps a second server registered for the lane, under the name that clamps.
        return "retired"
    return None


def _rederive(block: list, name: str, actor: str, expected: dict) -> list:
    """``block`` with its lane-derived env lines replaced by ``expected``; every other line (the
    marker, OKENGINE_PACK, an operator's own key) is kept where it is. A block whose actor is not a
    plain env line (a flow-style ``env: {...}``) is rendered afresh; ensure-runtime's later steps
    bind the pack again."""
    lines = [line for line in block if not _MANAGED_RE.match(line)]
    at = next((i for i, line in enumerate(lines) if _ACTOR_LINE_RE.match(line)), None)
    if at is None:
        return _render(name, actor, expected[CONTRACT_KEY] is not None,
                       expected[TOOLS_KEY]).splitlines(keepends=True)
    managed = [f"      {key}: {value}\n" for key, value in expected.items() if value]
    return lines[:at + 1] + managed + lines[at + 1:]


def reconcile(text: str, jobs: Optional[list], policy: dict, cps) -> tuple:
    """Return ``(new_text, changes)`` with ``changes`` = ``{added, rederived, retired, removed}``.

    ``jobs`` None means the lane set is unknown: writers are added for the policy actors only and
    nothing is pruned or re-derived. A generated writer of a wanted lane is rewritten only where its contract
    mode or tool list disagrees with the lane; any other entry is never rewritten."""
    changes = {"added": [], "rederived": [], "retired": [], "removed": []}
    lines = text.splitlines(keepends=True)
    mcp_start = next((i for i, line in enumerate(lines)
                      if _MCP_RE.match(line.rstrip("\n"))), None)
    if mcp_start is None:
        return text, changes
    mcp_end = next((i for i in range(mcp_start + 1, len(lines))
                    if _TOP_LEVEL_RE.match(lines[i])), len(lines))
    body, kept = lines[mcp_start + 1:mcp_end], []
    actors, write_tools = wanted_actors(jobs, policy, cps)
    servers = _parsed_servers(text)

    for start, end in _chunks(body):
        match = _SERVER_RE.match(body[start].rstrip("\n"))
        name = match.group(1) if match else ""
        actor = generated_lane_actor(name, servers.get(name), cps)
        kind = _disposition(name, actor, actors, jobs is not None, cps)
        drift = (env_drift(servers.get(name), managed_env(actor, actors, write_tools))
                 if kind is None and actor is not None and jobs is not None else [])
        if drift:
            block = _rederive(body[start:end], name, actor, managed_env(actor, actors, write_tools))
            if not block[0].startswith(f"  {name}:") and kept and kept[-1].strip() == _marker(actor):
                kept.pop()                # a fresh render brings its own marker
            kept.extend(block)
            changes["rederived"].append(name)
        elif kind is None:
            kept.extend(body[start:end])  # not a server, or a server that stays
        else:
            if kept and kept[-1].strip() == _marker(actor):
                kept.pop()                # the engine marker goes with its block
            changes[kind].append(name)

    existing = {m.group(1) for line in kept if (m := _SERVER_RE.match(line.rstrip("\n")))}
    for actor in sorted(actors):
        name = cps.contract_writer_name(actor[len("cron:"):])
        if name not in existing:
            kept.append(_render(name, actor, actors[actor], write_tools.get(actor)))
            changes["added"].append(name)
    return "".join(lines[:mcp_start + 1] + kept + lines[mcp_end:]), changes


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Render this deployment's per-lane write servers.")
    ap.add_argument("--pack", required=True, help="the deployment directory")
    ap.add_argument("--config", help="config.yaml (default <pack>/.hermes-data/config.yaml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="write nothing; print the writers that would change, as JSON")
    args = ap.parse_args(argv)
    pack = Path(args.pack).resolve()
    cfg = Path(args.config) if args.config else pack / ".hermes-data" / "config.yaml"
    cps = load_split()
    policy = load_policy(pack)
    jobs = deployment_jobs(pack, cps)
    text = cfg.read_text(encoding="utf-8")
    new_text, changes = reconcile(text, jobs, policy, cps)
    if args.dry_run:
        print(json.dumps({"config": str(cfg), "lane_set_known": jobs is not None, **changes},
                         indent=2))
        return 0
    if new_text != text:
        cfg.write_text(new_text, encoding="utf-8")
        parts = [f"added {len(changes['added'])} policy-bound cron writer(s)"]
        if changes["rederived"]:
            parts.append(f"re-derived {len(changes['rederived'])} writer(s) from their lane's "
                         f"current contract and write_tools: " + ", ".join(changes["rederived"]))
        if changes["retired"]:
            parts.append(f"retired {len(changes['retired'])} pre-bound name(s): "
                         + ", ".join(changes["retired"]))
        if changes["removed"]:
            parts.append(f"removed {len(changes['removed'])} writer(s) for lanes this deployment "
                         f"does not run: " + ", ".join(changes["removed"]))
        print(f"reconciled: {cfg} ({'; '.join(parts)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
