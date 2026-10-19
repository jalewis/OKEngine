#!/usr/bin/env python3
"""cron-pack-split — the engine/domain-pack cron generator (M3 keystone, prototyped in M2).

Per docs/engine-domain-boundary.md §3, the deployed cron-plus-jobs.json must be a
GENERATED merge, not a hand-bundled file:

    engine cron defs            (tier: engine          — full defs)
  + engine-template SCRIPT defs (tier: engine-template — schedule/script, NO prompt)
  + domain-pack cron defs       (tier: domain          — full defs)
  + engine-template PROMPTS     (supplied by the domain pack, keyed by job name)
  ───────────────────────────────────────────────────────────────────────────────
  = cron-plus-jobs.json

This tool implements both directions, keyed by config/cron-tiers.yaml:

  split  cron-plus-jobs.json  ->  engine/ + domain-pack/   (3 artifacts)
  merge  engine/ + domain-pack/  ->  cron-plus-jobs.json
  check  round-trip: split then merge must reproduce the input exactly

Decision-independent: works the same whether engine + pack end up in one repo or
two. `check` is the success criterion — if the boundary is real, the round-trip
is lossless.

Usage:
  cron_pack_split.py check                       # round-trip self-test (default)
  cron_pack_split.py split [--out DIR]
  cron_pack_split.py merge --in DIR [--out FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
import artifact_contract
import output_contract

REPO = Path(__file__).resolve().parent.parent
JOBS = REPO / "config" / "cron-plus-jobs.json"
TIERS = REPO / "config" / "cron-tiers.yaml"

# Engine artifact: the engine's cron half (engine full + engine-template scripts).
ENGINE_CRONS = "engine-crons.json"
# Domain-pack artifacts.
DOMAIN_CRONS = "domain-crons.json"
DOMAIN_PROMPTS = "engine-template-prompts.json"
# Extension-tier jobs (okengine#141): folded by the extension pass, NOT a cron-tiers.yaml
# source — carried through split/merge as their own partition so the boundary stays lossless.
EXTENSIONS = "extensions"

# Slice-2 source-of-truth locations (the deployed cron-plus-jobs.json is GENERATED):
#   engine half  -> config/engine-crons.json  (this repo)
#   domain half  -> <pack>/crons/{domain-crons,engine-template-prompts}.json
ENGINE_CRONS_FILE = REPO / "config" / ENGINE_CRONS
# The deployment (host) directory regen, regen_composed and dump act on. Unset or empty leaves the
# placeholder, which host_dir() refuses: every caller must name the deployment (okengine#970).
PACK_DIR_PLACEHOLDER = "/path/to/pack"
PACK_DIR = Path(os.environ.get("CRON_PACK_DIR") or PACK_DIR_PLACEHOLDER)

# Runtime fields stripped when capturing from the live scheduler state.
RUNTIME_FIELDS = {"next_run_at", "last_run_at", "last_run_success", "last_completed_at",
                  "last_error", "last_delivery_error", "after_claim", "after_consumed"}
# A runtime PAUSE (budget-guard cost cap, or a manual `cron-plus pause`) is stored IN jobs.json as
# {enabled: false, paused_at: ...}. `paused_at` is the marker that distinguishes a pause from an
# intentional source-level `enabled: false` (a ship-disabled placeholder).
PAUSE_MARKERS = {"paused_at", "paused_reason", "paused_by"}

_MCP_DISCOVERY_CONTRACT = (
    "\n\nMCP DISCOVERY CONTRACT: OKEngine's connected MCP servers expose named tools; "
    "they do not expose prompt templates or arbitrary filesystem resources. Do not call "
    "`list_prompts` or `get_prompt`, and never request guessed names such as `preflight`, "
    "`schema`, or `source-scoring-rubric`. Use the instructions already present in this "
    "prompt. Read explicit local paths with `file_read`/`read_file`, not `read_resource`. "
    "If a future prompt explicitly requires an MCP resource, call `list_resources` first "
    "and pass only a URI returned verbatim; never invent `file://`, `okengine://`, or any "
    "other resource URI."
)

_SOURCE_QUALITY_RUBRIC = (
    "\n\nSOURCE QUALITY RUBRIC: Apply this complete Admiralty baseline directly; do "
    "not look up another rubric. Reliability grades the publishing channel: "
    "A=completely reliable, B=usually reliable, C=fairly reliable, D=not usually "
    "reliable, E=unreliable, F=reliability cannot be judged. Credibility grades the "
    "reported information: 1=confirmed by other independent sources, 2=probably "
    "true, 3=possibly true, 4=doubtful, 5=improbable, 6=truth cannot be judged. Use "
    "the publisher, source kind, provenance, and locally supplied page metadata; do "
    "not browse."
)


def sanitize(jobs: list[dict]) -> list[dict]:
    out = []
    for j in jobs:
        sj = {k: v for k, v in j.items() if k not in RUNTIME_FIELDS}
        # A runtime pause must NOT be captured as source truth: `dump` writes the sanitized jobs into
        # config/engine-crons.json + the pack's domain-crons.json, and regen() drops every
        # enabled:false job from the deployed artifact — so a `dump` run while the budget guard has
        # cost-bearing crons paused would SILENTLY REMOVE them from the fleet on the next deploy
        # (invariant-audit HIGH). A pause is runtime state; un-pause it on capture. TRUTHINESS, not
        # key-presence: cron-plus RESUME sets paused_at back to None but KEEPS the key, so a
        # paused-then-resumed job carries paused_at:null forever — a key-presence check would then
        # force-flip a later OPERATOR-disabled (plain enabled:false) job back to enabled on dump
        # (re-verify regression). Only a job actually paused NOW has a truthy paused_at.
        if sj.get("paused_at"):
            for m in PAUSE_MARKERS:
                sj.pop(m, None)
            sj["enabled"] = True
        else:
            sj.pop("paused_at", None)          # drop a lingering paused_at:null (never source truth)
        if isinstance(sj.get("repeat"), dict) and "completed" in sj["repeat"]:
            sj["repeat"] = {k: v for k, v in sj["repeat"].items() if k != "completed"}
        out.append(sj)
    return out


def _by_name(jobs: list[dict]) -> list[dict]:
    return sorted(jobs, key=lambda j: j.get("name", ""))


def _normalize_schedule(job: dict) -> dict:
    """cron-plus requires `schedule` to be a DICT ({kind:cron, expr:...}); the pack-authoring docs
    AND framework_validate._cron_expr also accept a BARE STRING ("0 13 * * SUN") and a TOP-LEVEL
    `expr` (no schedule key). Normalize BOTH looser shapes into the dict HERE — the single chokepoint
    that writes the deployed jobs.json — so a job that VALIDATES GREEN can't deploy in a shape
    cron-plus silently never fires:
      - bare string → 'str has no attribute get' crashes every tick, stalling the fleet (audit #1);
      - top-level {expr} → deploys with NO schedule key, so compute_next_run returns None,
        next_run_at stays null, and Phase 2 skips the job every tick forever, no error logged
        (audit HIGH #5)."""
    s = job.get("schedule")
    if isinstance(s, str) and s.strip():
        job = dict(job)
        job["schedule"] = {"kind": "cron", "expr": s.strip()}
    elif s is None and isinstance(job.get("expr"), str) and job["expr"].strip():
        job = dict(job)
        job["schedule"] = {"kind": "cron", "expr": job["expr"].strip()}
    return job


def _ensure_id(job: dict) -> dict:
    """Every deployed cron MUST carry an `id`. The pinned cron-plus scheduler's null-next_run_at heal
    logs via `job.get("name", job["id"])` — Python evaluates the default arg EAGERLY, so an id-less
    job raises KeyError('id') on the FIRST tick (even with a name present), the exception escapes the
    claim before anything persists, and the ticker re-crashes every 60s FOREVER — stalling the WHOLE
    fleet, engine lanes included, with every gate green (invariant-audit CRITICAL). The pack-authoring
    docs never require id, so mint a stable one from the (already pack-prefixed, unique) name at this
    single deploy chokepoint — no id-less job can reach the deployed jobs.json."""
    if not str(job.get("id") or "").strip():
        job = dict(job)
        # Stable non-security identifier; collision resistance is not an auth boundary.
        job["id"] = hashlib.sha1(
            str(job.get("name") or "").encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:12]
    return job


def _stamp_output_contract(job: dict) -> dict:
    """Bind the generated lane to the exact contract enforcement must apply."""
    if not isinstance(job.get("output_contract"), dict):
        return job
    job = dict(job)
    job["output_contract_digest"] = output_contract.digest(job["output_contract"])
    env = dict(job.get("env") or {})
    env["OKENGINE_LANE_ID"] = str(job.get("id") or "")
    env["OKENGINE_CONTRACT_DIGEST"] = job["output_contract_digest"]
    if job["output_contract"].get("completion") == "per-selected-item":
        manifest = selection_manifest(job)
        job["selection_manifest"] = manifest
        # Per-item completion is not meaningful unless the runner actually
        # verifies a receipt against the selector-owned manifest. Keep these
        # coupled at the deployment choke point so extension-composed jobs
        # cannot silently advertise per-item completion while running in the
        # legacy, unverified "run" mode.
        job.setdefault("receipt_mode", "enforce")
        job.setdefault("receipt_hash_mode", "readback")
        env["OKENGINE_SELECTION_MANIFEST"] = manifest
    job["env"] = env
    return job


def template_lane(job_name: str) -> str:
    """The lane a job name runs: merge_packs names a composed engine-template instance
    `<job>@<pack>`, so a check that names an engine lane must compare this, not the raw name
    (okengine#961). The write path's twin is write_services.state.lane_actor on the `cron:`
    actor; a test pins that the two agree on every name merge_packs mints."""
    return str(job_name or "").partition("@")[0]


def domain_job_name_errors(defs: list[dict]) -> list[str]:
    """`@` in a pack-authored domain job name is an error (okengine#969).

    The suffix after `@` is reserved for the `<job>@<pack>` instances merge_packs mints, and
    template_lane / lane_actor strip it. A domain job named `entity-backfill@shadow` therefore
    runs on the write path AS entity-backfill: its tool fence, lane checks and capability. Every
    def passed here must be pack-authored; framework validate passes the pack's own
    domain-crons.json, and fleet_errors passes the jobs carrying the pack provenance marker."""
    return [f"domain job {d.get('name')!r} contains '@', which is reserved for engine-generated "
            f"<lane>@<pack> instances: the write path would treat it as lane "
            f"{template_lane(d.get('name'))!r} -- rename the job"
            for d in defs if "@" in str(d.get("name") or "")]


def validate_domain_job_names(jobs: list[dict]) -> list[str]:
    """The fleet half of domain_job_name_errors. A job is pack-authored iff it carries the `pack`
    provenance marker: regen and merge_packs stamp it on every domain job and never on an engine
    or engine-template job, so a composed `<lane>@<pack>` instance (no marker) stays legal. The
    key's presence decides, not its value, so a pack cannot opt out by declaring `pack: ""`."""
    return domain_job_name_errors([j for j in jobs if "pack" in j])


def domain_shadow_errors(pname: str, domain: list[dict], engine_names) -> list[str]:
    """A pack domain job may not take the bare name of an engine or engine-template lane: the one
    rule for regen, merge_packs and framework validate (okengine#969).

    On the write path the job's actor would then BE that lane (`cron:<lane>`), with its tool
    fence, lane checks and capability. On a single-pack regen an engine-template lane the pack
    does not drive is skipped, so nothing else collides with the twin and it ships under the
    lane's name. In merge_packs the twin also makes an intra-pack after: target ambiguous: it
    would rebind to the domain job instead of the engine lane the author meant (invariant-audit
    batch-3 re-verify). Driving an engine-template lane is done with a prompt in
    engine-template-prompts.json, never with a domain job of the same name."""
    prefix = f"{pname}: " if pname else ""
    return [f"{prefix}domain job {nm!r} shadows an engine/engine-template lane of the same name "
            "-- it would run as that lane on the write path and make after: targets ambiguous; "
            "rename the domain job (drive an engine-template lane through "
            "engine-template-prompts.json instead)"
            for nm in sorted({j.get("name") for j in domain
                              if isinstance(j.get("name"), str)} & set(engine_names))]


def engine_shadow_errors(defs: list[dict]) -> list[str]:
    """domain_shadow_errors against this engine's own cron half, for framework validate, which
    holds a pack's domain-crons.json but not the engine list. Raises if the engine half cannot be
    loaded; the caller reports that as UNDETECTABLE, never as a pass."""
    return domain_shadow_errors("", defs, {j["name"] for j in _load_jobs(ENGINE_CRONS_FILE)})


def _stamp_mcp_discovery_contract(job: dict) -> dict:
    """Make the no-guessed-MCP rule systemic for every generated model lane."""
    prompt = job.get("prompt")
    if job.get("no_agent") or not isinstance(prompt, str) or not prompt.strip():
        return job
    stamped = dict(job)
    additions = ""
    if (
        template_lane(job.get("name")) == "source-quality-backfill"
        and "A=completely reliable" not in prompt
    ):
        additions += _SOURCE_QUALITY_RUBRIC
    if "MCP DISCOVERY CONTRACT:" not in prompt:
        additions += _MCP_DISCOVERY_CONTRACT
    if not additions:
        return job
    stamped["prompt"] = prompt.rstrip() + additions
    return stamped


def _stamp_prompt_metrics(job: dict) -> dict:
    prompt = job.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return job
    stamped = dict(job)
    size = len(prompt.encode("utf-8"))
    stamped["prompt_metrics"] = {"bytes": size, "estimated_tokens": (size + 3) // 4}
    return stamped


def runs(job: dict) -> bool:
    """Whether a source job is one of the lanes a deployment RUNS: the single definition of the
    deployed set. `enabled: false` placeholders stay in the in-memory fleet but never deploy.
    Everything that must agree with the deployed artifact (the dump, the ordering and id gates,
    ensure-runtime's lane writers, okengine#1029) filters with this, so the sides cannot drift."""
    return bool(job.get("enabled", True))


def _dump_jobs(jobs: list[dict]) -> str:
    """Canonical cron-plus-jobs.json text — name-sorted. DISABLED jobs
    (`enabled: false` placeholders) are NOT written to the deployed artifact:
    cron-plus would otherwise validate their never-fires sentinel expr (`0 0 30 2 *`)
    every tick and log 'invalid cron expr' noise (#27). They stay in the SOURCE
    (the pack's domain-crons.json); flip enabled:true + set a real expr to deploy
    one. The in-memory merge keeps them, so split/compose stay lossless."""
    live = [_stamp_prompt_metrics(_stamp_mcp_discovery_contract(
                _stamp_output_contract(_ensure_id(_normalize_schedule(j)))))
            for j in jobs if runs(j)]
    return json.dumps({"jobs": _by_name(live)}, indent=2, ensure_ascii=False) + "\n"


def _dump_list(jobs: list[dict]) -> str:
    return json.dumps(_by_name(jobs), indent=2, ensure_ascii=False) + "\n"


def _dump_prompts(prompts: dict) -> str:
    return json.dumps(prompts, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _load_jobs(path: Path) -> list[dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    jobs = d["jobs"] if isinstance(d, dict) else d
    base = REPO if path.resolve() == ENGINE_CRONS_FILE.resolve() else path.parent.parent
    out = []
    for job in jobs:
        job = dict(job)
        reference = job.get("prompt_file")
        if isinstance(reference, str) and "prompt" not in job:
            prompt_path = (base / reference).resolve()
            try:
                prompt_path.relative_to(base.resolve())
            except ValueError as exc:
                raise ValueError(f"prompt_file escapes source root: {reference}") from exc
            job["prompt"] = prompt_path.read_text(encoding="utf-8").rstrip()
        out.append(job)
    return out


def _load_prompts(path: Path) -> dict:
    values = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent.parent
    out = {}
    for name, value in values.items():
        if not isinstance(value, dict) or not isinstance(value.get("prompt_file"), str):
            out[name] = value
            continue
        prompt_path = (base / value["prompt_file"]).resolve()
        try:
            prompt_path.relative_to(base.resolve())
        except ValueError as exc:
            raise ValueError(f"prompt_file escapes pack root: {value['prompt_file']}") from exc
        resolved = dict(value)
        resolved["prompt"] = prompt_path.read_text(encoding="utf-8").rstrip()
        out[name] = resolved
    return out


def _tier_map(path: Path) -> dict[str, str]:
    t = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = {}
    for tier in ("engine", "engine-template", "domain"):
        for name in t.get(tier) or []:
            out[name] = tier
    return out


def _is_extension_job(j: dict, tier_of: dict[str, str]) -> bool:
    """An extension-tier job (okengine#141): carries the composer's `extension` marker, or
    (legacy artifacts) is unclassified by cron-tiers but namespaced like an extension id
    (`<id>` / `<id>:<op>` — chars engine/pack cron names never use)."""
    if j.get("extension"):
        return True
    if j.get("pack"):
        # A pack-provenance-marked job (okengine#143) is a DOMAIN cron, never an extension — even if
        # its name contains '.'/':'. Without this, a pack cron like `acme.fetch` (pack: okpack-x)
        # was routed to the EXTENSIONS partition and then silently ERASED by dump_from_live, which
        # only writes engine/domain/prompts (invariant-audit #24).
        return False
    name = j.get("name", "")
    return tier_of.get(name) is None and ("." in name or ":" in name)


def split(jobs: list[dict], tier_of: dict[str, str]) -> dict[str, object]:
    engine, domain, prompts, extensions = [], [], {}, []
    for j in jobs:
        name = j["name"]
        if _is_extension_job(j, tier_of):
            extensions.append(j)              # regenerated by the extension pass, not cron-tiers
            continue
        if j.get("pack"):                     # okengine#143: pack-domain provenance marker —
            domain.append(j)                  # cron-tiers `domain:` is empty by design, so the
            continue                          # marker is how split routes a pack's own crons
        tier = tier_of.get(name)
        if tier == "engine":
            source = dict(j)
            if isinstance(source.get("prompt_file"), str):
                source.pop("prompt", None)
            engine.append(source)
        elif tier == "engine-template":
            stub = {k: v for k, v in j.items() if k not in {"prompt", "prompt_file"}}
            engine.append(stub)               # engine ships the script/schedule
            if isinstance(j.get("prompt_file"), str):
                value = {"prompt_file": j["prompt_file"]}
                if isinstance(j.get("output_contract"), dict):
                    value["output_contract"] = j["output_contract"]
                prompts[name] = value
            elif "prompt" in j:
                if isinstance(j.get("output_contract"), dict):
                    prompts[name] = {
                        "prompt": j["prompt"],
                        "output_contract": j["output_contract"],
                    }
                else:
                    prompts[name] = j["prompt"]   # pack supplies the prompt
        elif tier == "domain":
            domain.append(j)
        else:
            raise SystemExit(f"unclassified job (not in cron-tiers.yaml): {name!r}")
    return {ENGINE_CRONS: engine, DOMAIN_CRONS: domain, DOMAIN_PROMPTS: prompts,
            EXTENSIONS: extensions}


def _prompt_parts(value, jobname: str) -> tuple[str, dict | None]:
    """Accept legacy inline prompts and resolved prompt-file objects."""
    if isinstance(value, str):
        return value, None
    if not isinstance(value, dict) or not isinstance(value.get("prompt"), str):
        raise ValueError(
            f"engine-template prompt {jobname!r} must be a string or an object "
            "with a resolved prompt"
        )
    unknown = sorted(set(value) - {"prompt", "prompt_file", "output_contract"})
    if unknown:
        raise ValueError(f"engine-template prompt {jobname!r} has unknown key(s): {unknown}")
    return value["prompt"], value.get("output_contract")


def validate_output_contracts(jobs: list[dict], *, require_declared: bool = False) -> list[str]:
    errors: list[str] = []
    for job in jobs:
        name = job.get("name", "<unnamed>")
        contract = job.get("output_contract")
        is_model_writer = not job.get("no_agent") and "okengine-write" in (job.get("enabled_toolsets") or [])
        if contract is not None:
            errors.extend(output_contract.validate(contract, f"job {name!r} output_contract"))
        elif require_declared and is_model_writer and not job.get("output_contract_exempt"):
            errors.append(f"job {name!r} is model-writing but has no output_contract or explicit exemption")
    return errors


def validate_artifact_contracts(jobs: list[dict]) -> list[str]:
    errors: list[str] = []
    for job in jobs:
        contract = job.get("artifact_contract")
        if contract is not None:
            if job.get("no_agent") is not True:
                errors.append(
                    f"job {job.get('name')!r} artifact_contract is only valid for no_agent jobs")
            errors.extend(artifact_contract.validate(
                contract, f"job {job.get('name')!r} artifact_contract"))
    return errors


def validate_agent_bounds(jobs: list[dict]) -> list[str]:
    """Every model-bearing job must declare its own finite turn budget.

    Inheriting Hermes's interactive default makes a selector-bounded lane
    operationally unbounded: one bad item can occupy a model slot for up to 90
    turns. Validate the final composed fleet so engine, extension, and pack jobs
    are held to the same deploy-time contract.
    """
    errors = []
    for job in jobs:
        if job.get("no_agent") is True:
            continue
        value = job.get("max_iterations")
        if not isinstance(value, int) or isinstance(value, bool) or value < 2:
            errors.append(
                f"job {job.get('name')!r}: cost-bearing agent lane requires "
                "integer max_iterations >= 2 (work plus terminal accounting)"
            )
    return errors


def validate_agent_prompts(jobs: list[dict]) -> list[str]:
    """Every enabled model lane must reach the model with its instructions.

    A lane whose `prompt_file` was never resolved deploys with an empty prompt and runs anyway:
    the model gets the cron wrapper and its selector's output and improvises the rest. One pack's
    four model lanes ran that way for a week, with no rule against strengthening a claim and no
    instruction to write before the turn budget ran out.
    """
    errors = []
    for job in jobs:
        if job.get("no_agent") is True or job.get("enabled") is False:
            continue
        if not str(job.get("prompt") or "").strip():
            source = job.get("prompt_file")
            why = f"prompt_file {source!r} did not resolve" if source else "no prompt or prompt_file"
            errors.append(f"job {job.get('name')!r}: model lane has an empty prompt ({why}); "
                          "it would run on its selector output alone")
    return errors


# Hermes dispatches an MCP tool as ``mcp__<server>__<tool>`` and clamps anything over 64 chars to
# ``<truncated>_<8 hex>`` (tools/mcp_tool_schema.mcp_prefixed_tool_name). The clamp keeps the first
# 64-9=55 characters, so a server name long enough to push the server/tool ``__`` past that loses
# the separator: the dispatched name arrives with TWO ``__``-separated components, not three.
#
# The run-telemetry parser identifies a write by that second component, so once it is gone EVERY
# write the lane makes is dropped from its receipt -- and its completion contract then fails a run
# that genuinely wrote. Measured on one live vault: 7 lanes, all 11 write tools, for weeks; a
# prediction page went v199->v200 on disk while its receipt recorded nothing (okengine#840).
#
# Component sanitization is character-for-character (``re.sub`` over a negated class), so it never
# changes a length and the server name alone decides this.
_MCP_TOOL_NAME_MAX = 64          # tools/mcp_tool_schema._MCP_TOOL_NAME_MAX_LENGTH
_MCP_TOOL_NAME_HASH = 8          # tools/mcp_tool_schema._MCP_TOOL_NAME_HASH_LENGTH
_MCP_TOOL_NAME_PREFIX = "mcp__"  # tools/mcp_tool_schema.MCP_TOOL_NAME_PREFIX
# Only the FIRST of the separator's two chars has to survive truncation: the hash suffix is
# ``"_" + <8 hex>``, so a half-cut ``_`` is rejoined into a ``__`` by the suffix's own leading
# underscore. The boundary is therefore one char looser than it looks -- verified against the
# real mcp_prefixed_tool_name, where a 49-char server round-trips intact and a 50-char one does
# not. test_the_writer_name_limit_is_exactly_where_the_separator_survives pins it to the
# algorithm so this cannot be widened by assertion.
_KEPT = _MCP_TOOL_NAME_MAX - _MCP_TOOL_NAME_HASH - 1                   # chars kept before the suffix
CONTRACT_WRITER_NAME_MAX = _KEPT - 1 - len(_MCP_TOOL_NAME_PREFIX)      # == 49
_WRITER_DIGEST = 8                                                  # hex chars in a bounded name
# CONTRACT_WRITER_NAME_MAX is the line past which writes are LOST. A derived name aims tighter: short
# enough that no write tool clamps at all, so every dispatched name is the literal
# `mcp__<server>__<tool>` a prompt can name and a model can read. Between the two, telemetry
# survives but each long tool is reduced to a hash -- at 49 chars all eleven are.
_LONGEST_WRITE_TOOL = len("record_machine_review")   # pinned against write_server.py by a test
# Every tool okengine-mcp/write_server.py registers -- the only names a lane's `write_tools` may
# list. Pinned to the @mcp.tool registrations by a test, so a new tool fails there first.
WRITE_TOOL_NAMES = frozenset({
    "create_entity", "update_entity", "score_source", "tombstone_entity", "flag_for_review",
    "patch_entity", "append_to_section", "record_machine_review", "converge_entity",
    "converge_source", "converge_concept",
})
WRITER_NAME_TARGET = (_MCP_TOOL_NAME_MAX - len(_MCP_TOOL_NAME_PREFIX) - 2
                      - _LONGEST_WRITE_TOOL)                          # == 36


def unbounded_writer_name(job_name: str) -> str:
    """The pre-#840 derivation. Kept only so deploy can recognise -- and retire -- a server block
    the engine itself registered under a name that has since been bounded."""
    slug = re.sub(r"[^a-z0-9]+", "-", job_name.lower()).strip("-")
    return f"okengine-write-{slug}"


def contract_writer_name(job_name: str) -> str:
    """The per-lane write-server name, always short enough to survive MCP dispatch.

    A name within :data:`WRITER_NAME_TARGET` is returned unchanged, so no lane whose tools already
    dispatch unclamped is renamed. A longer one keeps a readable prefix and ends in a digest of the
    FULL name:
    deterministic (deploy and regen derive the same identity independently) and distinct for two
    lanes that share a long common prefix. ensure-runtime.sh loads this function rather than
    restating it, so the registered server and the job's toolset cannot disagree.
    """
    name = unbounded_writer_name(job_name)
    if len(name) <= WRITER_NAME_TARGET:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_WRITER_DIGEST]
    keep = WRITER_NAME_TARGET - _WRITER_DIGEST - 1
    return f"{name[:keep].rstrip('-')}-{digest}"


def write_tools_declaration(job: dict) -> tuple[str | None, str | None]:
    """A lane's ``write_tools`` as its writer's OKENGINE_WRITE_TOOLS: ``(tools, error)``.

    Absent means no narrowing, ``(None, None)``. A present value must be a non-empty list of known
    write tool names (okengine#1044 [8]/[18]). A bare string, an empty or blank list, a non-string
    entry or a misspelled tool used to fall through to the full tool surface or to an allowlist
    that matches nothing, and every gate agreed because each read the same unchecked value."""
    if "write_tools" not in job:
        return None, None
    value = job["write_tools"]
    if not isinstance(value, list):
        return None, f"write_tools must be a list of write tool names, not {type(value).__name__}"
    names = []
    for item in value:
        if not isinstance(item, str):
            return None, f"write_tools entry {item!r} is not a tool name"
        if item.strip():
            names.append(item.strip())
    if not names:
        return None, ("write_tools names no tool; omit the key to keep the full tool surface, "
                      "or list the tools the lane uses")
    unknown = sorted(set(names) - WRITE_TOOL_NAMES)
    if unknown:
        return None, (f"unknown write tool {', '.join(map(repr, unknown))} in write_tools (the "
                      f"writer would serve none of it); known: {', '.join(sorted(WRITE_TOOL_NAMES))}")
    return ",".join(names), None


def validate_write_tools(jobs: list[dict]) -> list[str]:
    """Every ``write_tools`` declaration in the fleet is well formed (okengine#1044 [8]/[18])."""
    errors = []
    for job in jobs:
        _tools, error = write_tools_declaration(job)
        if error:
            errors.append(f"job {job.get('name')!r}: {error}")
    return errors


def validate_contract_writer_names(jobs: list[dict]) -> list[str]:
    """Every write-server name a lane can reach must survive dispatch and be one lane's alone.

    Validated on the final composed fleet, like :func:`validate_agent_bounds`, so engine,
    extension, and pack jobs are held to one deploy-time contract. Three ways it can go wrong:

    * a DERIVED name over the limit -- impossible while contract_writer_name bounds it, so this
      guards the derivation itself against a regression;
    * an EXPLICIT ``okengine-write-*`` in ``enabled_toolsets`` -- a pack may name a writer
      directly, which bypasses the derivation entirely;
    * two lanes deriving ONE name -- a writer carries a single OKENGINE_WRITE_ACTOR, so they
      would silently write as each other.
    """
    errors = []
    owners: dict[str, str] = {}
    for job in jobs:
        name = str(job.get("name") or "")
        if job.get("no_agent") is not True and isinstance(job.get("output_contract"), dict):
            writer = contract_writer_name(name)
            if len(writer) > CONTRACT_WRITER_NAME_MAX:
                errors.append(_writer_too_long(name, writer))
            if writer in owners and owners[writer] != name:
                errors.append(
                    f"jobs {owners[writer]!r} and {name!r} collide on write server {writer!r}; "
                    f"one server carries one OKENGINE_WRITE_ACTOR, so they would write as each "
                    f"other. Rename one lane.")
            owners.setdefault(writer, name)
        for tool in job.get("enabled_toolsets") or []:
            tool = str(tool)
            if tool.startswith("okengine-write-") and len(tool) > CONTRACT_WRITER_NAME_MAX:
                errors.append(_writer_too_long(name, tool))
    return list(dict.fromkeys(errors))


def unregistered_writers(jobs: list[dict], runtime_config: dict) -> list[str]:
    """Write servers a job names that the runtime config does not register.

    The job file and config.yaml are written by different steps: regen stamps writer names onto
    toolsets, ensure-runtime registers the matching servers. A stage-only jobs deploy that skips
    ensure-runtime -- after a writer rename, or for a newly contracted lane -- would ship lanes
    naming servers the gateway never starts, and those lanes would have no write path at all.
    Fail the deploy instead (okengine#840).
    """
    servers = (runtime_config or {}).get("mcp_servers")
    registered = set(servers) if isinstance(servers, dict) else set()
    missing: dict[str, list[str]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        for tool in job.get("enabled_toolsets") or []:
            tool = str(tool)
            if (tool == "okengine-write" or tool.startswith("okengine-write-")) \
                    and tool not in registered:
                missing.setdefault(tool, []).append(str(job.get("name") or "?"))
    return [f"write server {server!r} is named by {sorted(jobs_)} but not registered in "
            f"config.yaml mcp_servers -- run ensure-runtime.sh and recreate the gateway first"
            for server, jobs_ in sorted(missing.items())]


def _writer_too_long(job_name: str, writer: str) -> str:
    return (f"job {job_name!r}: write server {writer!r} is {len(writer)} chars "
            f"(max {CONTRACT_WRITER_NAME_MAX}) -- the dispatched MCP tool name would clamp past "
            f"its '__' separator and every write this lane makes would be dropped from its run "
            f"receipt, failing the completion contract on a run that wrote (okengine#840).")


# A prompt or selector names its write tools as `mcp__<server>__<tool>`, and <server> depends on
# how the lane is bound (per-lane, bounded by #840, or the generic writer). Hardcoded names went
# stale twice -- every first-party prompt named the generic server (#607), then #840's bounding
# renamed the per-lane ones -- and the lanes kept "working" only because models resolve tools from
# the list, not from the prose (okengine#852). So the name is derived HERE, where the binding is
# decided: a prompt writes the placeholder, and a selector reads the prefix from its job env.
WRITER_PLACEHOLDER = "mcp__OKENGINE_WRITER__"
WRITE_TOOL_PREFIX_ENV = "OKENGINE_WRITE_TOOL_PREFIX"
_WRITE_TOOL_RE = re.compile(r"mcp__(okengine_write[A-Za-z0-9_]*?)__([a-z_]+)")


def write_tool_prefix(server: str) -> str:
    """`mcp__<server>__` exactly as Hermes publishes it: non-word characters become `_`."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_]", "_", server) + "__"


def _lane_writers(job: dict) -> list[str]:
    return [t for t in (job.get("enabled_toolsets") or [])
            if t == "okengine-write" or str(t).startswith("okengine-write-")]


def _publish_writer(job: dict) -> None:
    """Expand the prompt placeholder and export the prefix, for a lane with exactly one writer."""
    writers = _lane_writers(job)
    if len(writers) != 1 or job.get("no_agent"):
        return
    prefix = write_tool_prefix(writers[0])
    if isinstance(job.get("prompt"), str) and WRITER_PLACEHOLDER in job["prompt"]:
        job["prompt"] = job["prompt"].replace(WRITER_PLACEHOLDER, prefix)
    job["env"] = {**(job.get("env") or {}), WRITE_TOOL_PREFIX_ENV: prefix}


def validate_prompt_writer_bindings(jobs: list[dict]) -> list[str]:
    """A prompt may name only write tools its own lane is given (okengine#852).

    Runs on the composed, writer-bound fleet, so it judges the name the model will actually see
    against the server the lane actually binds -- for engine, extension and pack prompts alike."""
    errors = []
    for job in jobs:
        prompt = job.get("prompt")
        if job.get("no_agent") or not isinstance(prompt, str):
            continue
        if WRITER_PLACEHOLDER in prompt:
            errors.append(f"{job.get('name')}: prompt uses {WRITER_PLACEHOLDER}<tool> but the lane "
                          f"binds {len(_lane_writers(job))} write servers, so it cannot be resolved")
            continue
        bound = {write_tool_prefix(w)[5:-2] for w in _lane_writers(job)}
        for named, tool in sorted(set(_WRITE_TOOL_RE.findall(prompt))):
            if named not in bound:
                errors.append(
                    f"{job.get('name')}: prompt names mcp__{named}__{tool} but the lane binds "
                    f"{sorted(bound) or 'no write server'} -- write {WRITER_PLACEHOLDER}{tool} instead")
    return errors


def bind_contract_writers(jobs: list[dict]) -> list[dict]:
    """Route contracted model lanes away from the generic/admin writer, then publish each agent
    lane's write-tool prefix to its prompt placeholder and job env (okengine#852)."""
    out = []
    for source in jobs:
        job = dict(source)
        if not job.get("no_agent") and isinstance(job.get("output_contract"), dict):
            name = str(job.get("name") or "")
            bound = contract_writer_name(name)
            # The lane's own PRE-BOUND name is rewritten too, not kept beside the new one: a `dump`
            # of a live artifact generated before #840 carries it explicitly, and keeping both would
            # hand the lane two write servers -- one of them the clamping name this fix retires.
            stale = unbounded_writer_name(name)
            tools = [bound if t in ("okengine-write", stale) else t
                     for t in (job.get("enabled_toolsets") or [])]
            if bound not in tools:
                tools.append(bound)
            job["enabled_toolsets"] = list(dict.fromkeys(tools))
        _publish_writer(job)
        out.append(job)
    return out


# ensure-runtime.sh installs this write server under a FIXED name, not one derived from its lane,
# and registers it on every deployment; so it is reserved by name (okengine#969).
BASELINE_WRITERS = {
    "okengine-write-source-quality":
        "the baseline server ensure-runtime registers as cron:source-quality-backfill",
    # okengine#983: a pack job named `chat` would otherwise derive this name and write as chat.
    "okengine-write-chat":
        "the Agent Chat writer ensure-runtime registers as chat:api_server",
}


def writer_identity_errors(jobs: list[dict], reserved=None, trusted_extensions=frozenset()
                           ) -> list[str]:
    """A pack job may bind no write server that is another lane's identity (okengine#969).

    A write server carries ONE OKENGINE_WRITE_ACTOR, and the write path keys the lane's tool
    fence, lane checks and capability on that actor. Identity is the SERVER NAME, not the job
    name: contract_writer_name lowercases and folds every non-[a-z0-9] run to '-', so a domain
    job `Trends-Refresh` or `trends_refresh` derives `okengine-write-trends-refresh`, the engine
    lane's own server, which ensure-runtime keeps registered as `cron:trends-refresh`. A job can
    also list a server explicitly in enabled_toolsets, with no contract at all.

    `jobs` are writer-bound (bind_contract_writers has run). Every server an UNTRUSTED job binds
    (a pack-marked domain job, or an operation of an extension outside `trusted_extensions`, the
    tier-1 ids; see _untrusted_origin) is compared with `reserved` ({server: owner}, see reserved_writers) and with the servers of
    every OTHER job in `jobs`, by the same contract/unbounded derivation ensure-runtime uses. So
    a composed `<lane>@<pack>` instance's server, an extension's, and a sibling pack job's are off
    limits too. The generic `okengine-write` names no lane and is not compared here (see
    validate_pack_agent_writers)."""
    owners: dict[str, dict] = {server: {("reserved", server): owner}
                               for server, owner in (reserved or {}).items()}
    for i, job in enumerate(jobs):
        lane = str(job.get("name") or "")
        if lane:
            for server in (contract_writer_name(lane), unbounded_writer_name(lane)):
                owners.setdefault(server, {})[("job", i)] = f"lane {job.get('name')!r}"
    errors = []
    for i, job in enumerate(jobs):
        origin = _untrusted_origin(job, trusted_extensions)
        if not origin:
            continue
        for server in _lane_writers(job):
            others = sorted(set(owner for key, owner in owners.get(server, {}).items()
                                if key != ("job", i)))
            if others:
                errors.append(
                    f"{origin} job {job.get('name')!r} binds write server {server!r}, the identity of "
                    f"{', '.join(others)}: one server carries one OKENGINE_WRITE_ACTOR, so the job "
                    "would write as that lane, with its fence and capability -- rename the job, or "
                    "drop the explicit writer from enabled_toolsets")
    return list(dict.fromkeys(errors))


def _engine_lane_names() -> list[str]:
    """Every engine cron's name, shipped or not."""
    data = json.loads(ENGINE_CRONS_FILE.read_text(encoding="utf-8"))
    return [str(j.get("name") or "") for j in (data["jobs"] if isinstance(data, dict) else data)]


def _untrusted_origin(job, trusted_extensions) -> str:
    """Who authored `job`, if it is not trusted with any write identity but its own, else "".

    Engine lanes and TIER-1 (first-party, `okengine.*`) extension operations are engine code. A
    pack-marked domain job is not, and neither is an operation of a pack- or operator-tier
    extension: its job carries the `extension` id but never the `pack` marker, so keying on the
    marker alone left it unchecked (okengine#969). `trusted_extensions` is the set of tier-1 ids
    (see _tier1_extensions); an extension id outside it is untrusted, whatever it is called."""
    if not isinstance(job, dict):
        return ""
    if "pack" in job:
        return "pack"
    if "extension" in job and job["extension"] not in trusted_extensions:
        return "pack/operator-tier extension"
    return ""


def _tier1_extensions() -> tuple[frozenset, list[str]]:
    """(ids, operation job names) of every DISCOVERED tier-1 extension, enabled or not: a disabled
    extension's server can still be registered on a deployment that once enabled it. Discovery
    scans only the engine root here, so these are exactly the first-party records."""
    m = _extension_compose()
    if m is None:
        return frozenset(), []
    records, _ = m._discovery_mod().discover(None)   # engine root only: tier-1
    names: list[str] = []
    for record in records:
        ops, _ = m._collect_ops(record["manifest"], record.get("dir"))
        names += [record["id"] if op is None else f"{record['id']}:{op}" for op, _ in ops]
    return frozenset(record["id"] for record in records), names


def reserved_writers(extension_lanes=None) -> dict[str, str]:
    """{server: owner} for every write identity a pack job may never bind: the baseline servers,
    and the contract and unbounded servers of every engine lane (a single-pack regen drops an
    engine-template lane the pack does not drive, so it is not in the fleet) and of every tier-1
    extension operation."""
    reserved = dict(BASELINE_WRITERS)
    if extension_lanes is None:
        extension_lanes = _tier1_extensions()[1]
    for kind, names in (("engine lane", _engine_lane_names()),
                        ("extension lane", extension_lanes)):
        for lane in filter(None, names):
            for server in (contract_writer_name(lane), unbounded_writer_name(lane)):
                reserved.setdefault(server, f"{kind} {lane!r}")
    return reserved


def pack_extension_writer_errors(pack_dir) -> list[str]:
    """framework validate's form of extension_compose.foreign_writer_errors: every DISCOVERED
    pack- and operator-tier extension of `pack_dir`, enabled or not, so a pack fails before it is
    enabled anywhere. Raises if the composer is absent; the caller reports UNDETECTABLE."""
    m = _extension_compose()
    if m is None:
        raise RuntimeError("extension_compose.py is absent")
    records, _ = m._discovery_mod().discover(Path(pack_dir))
    return [error for record in records for error in m.foreign_writer_errors(record)]


def validate_untrusted_writers(jobs: list[dict]) -> list[str]:
    """The fleet gate: writer_identity_errors against reserved_writers(), and
    validate_pack_agent_writers, for every untrusted job, with tier-1 discovered once."""
    trusted, lanes = _tier1_extensions()
    return (writer_identity_errors(jobs, reserved_writers(lanes), trusted)
            + validate_pack_agent_writers(jobs, trusted))


def engine_writer_errors(defs: list[dict]) -> list[str]:
    """The pack-gate form, for framework validate: bind a pack's own domain defs exactly as regen
    will, mark them pack-authored, and check them against reserved_writers(). Raises if the engine
    half cannot be loaded; the caller reports that as UNDETECTABLE, never as a pass."""
    bound = bind_contract_writers([{**d, "pack": d.get("pack", "")} for d in defs])
    return writer_identity_errors(bound, reserved_writers())


def validate_pack_agent_writers(jobs: list[dict], trusted_extensions=frozenset()) -> list[str]:
    """An untrusted (_untrusted_origin) MODEL lane that writes must carry a contract, and never the generic writer.

    The generic `okengine-write` server has no OKENGINE_WRITE_ACTOR, and the write path treats an
    actor-less caller as admin. bind_contract_writers moves every contracted lane onto its own
    server, so a pack agent lane still binding the generic one is uncontracted, and would write
    with no fence at all. framework validate already refused an uncontracted model-writing lane,
    but `deploy.sh --skip-validate` or a standalone deploy-cron-plus-jobs.sh never ran it; this
    is the deploy-path half (okengine#969). no_agent lanes run scripts, not a model, and are
    unaffected. An `output_contract_exempt` lane may write through its own server, never the
    generic one."""
    errors = []
    for job in jobs:
        origin = _untrusted_origin(job, trusted_extensions)
        if not origin or job.get("no_agent"):   # no_agent truthy, as bind_contract_writers reads it
            continue
        writers = _lane_writers(job)
        if "okengine-write" in writers:
            errors.append(f"{origin} model lane {job.get('name')!r} binds the generic, actor-less "
                          "okengine-write server, which the write path treats as admin -- declare "
                          "an output_contract so the lane is bound to its own writer")
        elif writers and not isinstance(job.get("output_contract"), dict) \
                and not job.get("output_contract_exempt"):
            errors.append(f"{origin} model lane {job.get('name')!r} binds {writers} but declares no "
                          "output_contract -- a model-writing lane must declare one")
    return errors


def foreign_writer_bindings(jobs: list[dict], runtime_config: dict, trusted_extensions=None
                            ) -> list[str]:
    """Deploy-time ownership: every write server an untrusted job (_untrusted_origin) binds that the runtime config already
    registers must be registered FOR that job (OKENGINE_WRITE_ACTOR == cron:<job name>).

    The regen-time rule can only reserve identities the engine knows of. A live config also keeps
    servers it no longer has a lane for -- another pack's, a disabled extension's, an operator's
    -- and ensure-runtime never rewrites an existing entry's actor, so a pack lane naming one would
    write as its actor. A server not registered yet is not judged here: ensure-runtime creates it for
    the lane, and unregistered_writers fails the deploy if it has not run (okengine#969)."""
    servers = (runtime_config or {}).get("mcp_servers")
    servers = servers if isinstance(servers, dict) else {}
    if trusted_extensions is None:
        trusted_extensions = _tier1_extensions()[0]
    errors = []
    for job in jobs:
        origin = _untrusted_origin(job, trusted_extensions)
        if not origin:
            continue
        expected = f"cron:{job.get('name')}"
        # The generic server names no lane (validate_pack_agent_writers keeps model lanes off it).
        for server in [w for w in _lane_writers(job) if w.startswith("okengine-write-")]:
            block = servers.get(server)
            if not isinstance(block, dict):
                continue
            env = block.get("env")
            actor = env.get("OKENGINE_WRITE_ACTOR") if isinstance(env, dict) else None
            if actor != expected:
                errors.append(f"{origin} job {job.get('name')!r} binds write server {server!r}, which "
                              f"this deployment registers as {actor!r}, not {expected!r}: the job "
                              "would write as that identity -- rename the job or drop the writer")
    return errors


def merge(engine: list[dict], domain: list[dict], prompts: dict[str, object],
          tier_of: dict[str, str] | None = None,
          extensions: list[dict] | None = None) -> list[dict]:
    """Re-attach pack prompts onto the engine half.

    A pack OPTS IN to a shared `engine-template` job by supplying its prompt: when
    `tier_of` is given, an engine-template stub with NO pack prompt is SKIPPED
    (otherwise it would ship enabled + promptless = broken — the multi-pack bug).
    Pure-`engine` jobs always ship. `tier_of=None` preserves the legacy behavior
    (ship every engine cron) so the round-trip check stays a pure symmetry test."""
    out = []
    for j in engine:
        name = j["name"]
        if tier_of is not None and tier_of.get(name) == "engine-template" \
                and name not in prompts:
            continue                          # pack didn't opt into this template job
        j = dict(j)
        if name in prompts:                   # re-attach engine-template prompt + tighten contract
            prompt, policy = _prompt_parts(prompts[name], name)
            j["prompt"] = prompt
            if isinstance(prompts[name], dict) and isinstance(
                prompts[name].get("prompt_file"), str
            ):
                j["prompt_file"] = prompts[name]["prompt_file"]
            j["output_contract"] = output_contract.compose(j.get("output_contract"), policy,
                                                              f"job {name!r} output_contract")
        out.append(j)
    out.extend(domain)
    out.extend(extensions or [])              # carry extension-tier jobs through (#141)
    return bind_contract_writers(out)


def merge_packs(engine: list[dict], packs: list[dict],
                tier_of: dict[str, str]) -> tuple[list[dict], list[str]]:
    """N-way compose: engine half + N packs -> one job list (composable okpacks P3).

    Each pack is ``{"name": str, "domain": [jobs], "prompts": {job: prompt}}``.
    Rules (additive / disjoint / fail-loud):
      - pure-`engine` jobs always ship; an `engine-template` stub ships only as the
        instance ``<job>@<pack>`` of the ONE pack that drives it (okengine#968). It keeps the
        engine job's id, so run history, receipts and next_run_at carry over, and its selector
        state and selection manifest stay single-owner. A second pack driving the same lane is
        an ERROR naming both, matching install-domain, whose coinstall preflight reports an
        "engine-template prompt collision ... the shared engine lane can carry ONE prompt:
        HOST WINS" (coinstall_preflight.py);
      - domain jobs are **pack-prefixed** ``<pack>:<job>`` (no silent collision);
      - a prompt for a non-engine-template job, or a duplicate job id, is an ERROR.
    Returns ``(jobs, errors)`` — a non-empty error list means do not deploy.
    """
    errors: list[str] = []
    out: list[dict] = [dict(j) for j in engine if tier_of.get(j["name"]) != "engine-template"]
    engine_by_name = {j["name"]: j for j in engine}
    seen: dict[str, str] = {}
    drivers: dict[str, str] = {}

    for pk in packs:
        pname = pk["name"]
        # Build the pack's FULL rename map first: engine-template stubs the pack drives ->
        # <job>@<pack>, domain jobs -> <pack>:<job>. An intra-pack `after:` target names a SIBLING that
        # was ALSO renamed (a domain job OR a driven engine-template lane) — rewrite it against this map
        # so the dependency resolves to the renamed fleet member instead of dangling at the bare
        # pre-rename name (which validate_ordering then rejects, making the pack undeployable). A target
        # naming a PURE-engine job stays bare — those ship unrenamed (invariant-audit + batch-3 re-verify).
        template_driven = {jn for jn in (pk.get("prompts") or {})
                           if engine_by_name.get(jn) is not None and tier_of.get(jn) == "engine-template"}
        # The rename map is keyed by BARE name across three namespaces (pure-engine / driven-template /
        # domain), so a domain job shadowing an engine lane is fatal here too (domain_shadow_errors).
        errors.extend(domain_shadow_errors(pname, pk.get("domain") or [], engine_by_name))
        rename: dict[str, str] = {}
        for jn in template_driven:
            rename[jn] = f"{jn}@{pname}"
        for j in (pk.get("domain") or []):
            rename[j.get("name")] = f"{pname}:{j.get('name')}"

        def _rewrite_after(job: dict) -> dict:
            if job.get("after"):
                job["after"] = [rename.get(a, a) for a in job["after"]]
            return job

        for jobname, prompt_value in (pk.get("prompts") or {}).items():
            base = engine_by_name.get(jobname)
            if base is None or tier_of.get(jobname) != "engine-template":
                errors.append(f"{pname}: prompt for '{jobname}' which is not an "
                              "engine-template job")
                continue
            driver = drivers.setdefault(jobname, pname)
            if driver != pname:
                errors.append(f"engine-template lane {jobname!r} is driven by both {driver!r} and "
                              f"{pname!r}: a composed host runs ONE instance of an engine lane "
                              "(one id, one selector state), so exactly one pack may supply its "
                              "prompt -- remove the prompt from one pack")
                continue
            inst = _rewrite_after(dict(base))   # the inherited engine after: may name a lane this pack drives
            inst["name"] = f"{jobname}@{pname}"
            try:
                prompt, policy = _prompt_parts(prompt_value, jobname)
                inst["prompt"] = prompt
                if isinstance(prompt_value, dict) and isinstance(
                    prompt_value.get("prompt_file"), str
                ):
                    inst["prompt_file"] = prompt_value["prompt_file"]
                inst["output_contract"] = output_contract.compose(
                    inst.get("output_contract"), policy, f"job {jobname!r} output_contract")
            except ValueError as exc:
                errors.append(f"{pname}: {exc}")
                continue
            if inst["name"] in seen:
                errors.append(f"job-id collision: {inst['name']}")
            seen[inst["name"]] = pname
            out.append(inst)
        for j in (pk.get("domain") or []):
            j2 = _rewrite_after(dict(j))
            j2["name"] = f"{pname}:{j.get('name')}"
            j2.setdefault("pack", pname)        # provenance marker (okengine#143)
            if j2["name"] in seen:
                errors.append(f"job-id collision: {j2['name']}")
            seen[j2["name"]] = pname
            out.append(j2)
    return out, errors


def host_dir() -> Path:
    """PACK_DIR, the deployment directory, or a loud SystemExit if it was never named or is missing.

    Every entry point that reads it calls this first (okengine#970). Without the guard, an unset
    CRON_PACK_DIR left the literal placeholder, and the extension pass for a directory that does
    not exist quietly returned only the core-default extensions. So regen_composed exited 0 with
    the host's enabled extensions missing, and a single-pack regen shipped an engine-only fleet
    with no domain lanes. deploy-cron-plus-jobs.sh passes the placeholder itself when its caller
    leaves CRON_PACK_DIR unset, so "set" alone is not enough: the directory must exist."""
    if str(PACK_DIR) == PACK_DIR_PLACEHOLDER:
        raise SystemExit("CRON_PACK_DIR is not set: name the deployment directory "
                         "(CRON_PACK_DIR=<deployment>); refusing to generate a fleet for the "
                         f"placeholder {PACK_DIR_PLACEHOLDER}")
    if not PACK_DIR.is_dir():
        raise SystemExit(f"CRON_PACK_DIR={PACK_DIR} is not a directory: refusing to generate a "
                         "fleet without the deployment's crons and enabled extensions")
    return PACK_DIR


def _pack_name(pack_dir) -> str:
    """The pack's declared name (pack.yaml) for the #143 provenance marker; falls back to
    the directory name ONLY when there is no pack.yaml.

    A pack.yaml that exists but cannot be read is an error, never a fallback (okengine#857):
    the directory name is usually NOT the declared name (`okpack-house-test` vs `okpack-house`),
    so a silent fallback stamps every lane with a foreign pack marker and the deploy's
    FOREIGN-PACK gate aborts after the containers were already recreated -- blaming a "stale
    artifact" for what was an unimportable pack_meta."""
    if not (Path(pack_dir) / "pack.yaml").is_file():
        return Path(pack_dir).name
    try:
        meta = _pack_meta().load_pack_meta(pack_dir)
    except Exception as exc:
        raise RuntimeError(f"cannot read the pack name from {pack_dir}/pack.yaml: "
                           f"{type(exc).__name__}: {exc}") from exc
    if not meta:
        raise RuntimeError(f"cannot read the pack name from {pack_dir}/pack.yaml: "
                           "unparseable or not a mapping")
    return meta["name"]


def _pack_meta():
    """Load the sibling pack_meta module by path (no package assumptions)."""
    import importlib.util
    p = Path(__file__).resolve().parent / "pack_meta.py"
    spec = importlib.util.spec_from_file_location("pack_meta", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def discover_packs(packs_dir: Path) -> list[dict]:
    """Enumerate installed packs — subdirs of `packs_dir` that carry a `pack.yaml`
    (presence-based). Each -> ``{name, meta, domain, prompts}``."""
    pm = _pack_meta()
    out: list[dict] = []
    packs_dir = Path(packs_dir)
    if not packs_dir.is_dir():
        return out
    for d in sorted(packs_dir.iterdir()):
        if not d.is_dir():
            continue
        meta = pm.load_pack_meta(d)
        if meta is None:
            continue
        dc, dp = d / "crons" / DOMAIN_CRONS, d / "crons" / DOMAIN_PROMPTS
        out.append({
            "name": meta["name"], "meta": meta,
            "domain": _load_jobs(dc) if dc.is_file() else [],
            "prompts": _load_prompts(dp) if dp.is_file() else {},
        })
    return out


def compose(packs_dir: Path) -> tuple[list[dict], list[str]]:
    """Compose the engine half + ALL installed packs -> ``(jobs, errors)``. Errors
    come from composition validation (disjoint ownership / requires / single trust)
    AND the N-way job merge. A non-empty error list means DO NOT deploy. The jobs are the
    MERGED list, not yet deployable: regen_composed runs them through finalize_fleet."""
    pm = _pack_meta()
    engine = _load_jobs(ENGINE_CRONS_FILE)
    packs = discover_packs(packs_dir)
    errors = pm.validate_composition([p["meta"] for p in packs])
    jobs, merge_errors = merge_packs(engine, packs, _tier_map(TIERS))
    return jobs, errors + merge_errors


def regen_composed(packs_dir: Path) -> list[dict]:
    """compose + write config/cron-plus-jobs.json. Raises if the composition is
    unsound (writes nothing) — fail-loud before a bad deploy.

    The merged list goes through finalize_fleet, exactly as regen's does (okengine#968): the
    composed path used to skip the extension pass and writer binding, so every composed lane
    named the generic, actor-less (admin) writer and its prompt kept the writer placeholder.
    Extensions are resolved for PACK_DIR, the deployment directory regen also uses; an unset or
    missing one is refused before anything is composed (host_dir, okengine#970)."""
    host = host_dir()                         # before any work: nothing is written (okengine#970)
    jobs, errors = compose(packs_dir)
    if errors:
        raise SystemExit("composition errors (not deploying):\n  " + "\n  ".join(errors))
    jobs = finalize_fleet(jobs, host)
    JOBS.write_text(_dump_jobs(jobs), encoding="utf-8")
    return jobs


def selection_manifest(job: dict) -> str:
    """The selection manifest a job's selector and receipt verifier share, or "" if none.

    One rule for _stamp_output_contract (which exports it) and the uniqueness gate: an explicit
    `selection_manifest`, else the per-name default for a per-selected-item contract."""
    explicit = str(job.get("selection_manifest") or "")
    contract = job.get("output_contract")
    if explicit or not isinstance(contract, dict) or \
            contract.get("completion") != "per-selected-item":
        return explicit
    return f"/opt/data/cron-plus/selections/{job.get('name')}.json"


def validate_unique_selection_manifests(jobs: list[dict]) -> list[str]:
    """No two lanes may share a selection manifest: each lane's selector rewrites it every run and
    its receipt is verified against it, so a shared one makes each lane complete (or fail) on the
    other's selection (okengine#968)."""
    owners: dict[str, str] = {}
    errors = []
    for job in jobs:
        path = selection_manifest(job)
        if not path:
            continue
        other = owners.setdefault(path, str(job.get("name")))
        if other != str(job.get("name")):
            errors.append(f"jobs {other!r} and {job.get('name')!r} share selection manifest {path!r}")
    return errors


def fleet_errors(jobs: list[dict]) -> list[tuple[str, list[str]]]:
    """Every deploy gate on a final fleet, as (label, errors) in fail-first order. The ONE list,
    for regen, regen_composed and the compose CLI alike -- they used to keep three diverging
    copies, and the composed one lost writer binding entirely (okengine#968)."""
    _, order_errors = validate_ordering(jobs)          # okengine#129: broken/cyclic after:
    return [
        ("cron domain job-name errors", validate_domain_job_names(jobs)),  # okengine#969
        ("cron write-identity errors", validate_untrusted_writers(jobs)),  # okengine#969
        ("cron ordering errors", order_errors),
        ("cron id/name collisions", validate_unique_ids(jobs)),          # M37
        ("cron selection-manifest collisions", validate_unique_selection_manifests(jobs)),
        ("cron output-contract errors",
         validate_output_contracts(jobs) + validate_artifact_contracts(jobs)),
        ("cron agent-bound errors", validate_agent_bounds(jobs) + validate_agent_prompts(jobs)),
        ("cron write-server name errors",
         validate_contract_writer_names(jobs) + validate_prompt_writer_bindings(jobs)),
        ("cron write-tools errors", validate_write_tools(jobs)),         # okengine#1044 [8]/[18]
    ]


def finalize_fleet(merged: list[dict], host_dir: Path) -> list[dict]:
    """Merged engine+pack jobs -> the deployable fleet: fold in the host's enabled extensions,
    bind every contracted model lane to its own actor-bound writer (expanding the prompt writer
    placeholder), then run every deploy gate. Raises SystemExit on the first failing gate."""
    ext_jobs, ext_errors = _extension_pass(host_dir, merged)
    if ext_errors:
        raise SystemExit("extension composition errors (not deploying):\n  "
                         + "\n  ".join(ext_errors))
    jobs = bind_contract_writers(merged + ext_jobs)
    for label, errors in fleet_errors(jobs):
        if errors:
            raise SystemExit(f"{label} (not deploying):\n  " + "\n  ".join(errors))
    return jobs


def _write_split(parts: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for fname, data in parts.items():
        (out / fname).write_text(json.dumps(data, indent=2, sort_keys=False) + "\n",
                                 encoding="utf-8")


def _canon(jobs: list[dict]) -> list:
    """Order-insensitive, field-identical comparison key over the jobs' SOURCE.

    A `prompt` on a job that declares `prompt_file` is DERIVED: the file is the source of truth,
    `split` drops the resolved text on purpose (see the prompt_file split test), and the deploy
    path re-resolves it. Comparing it here makes the symmetry check compare a post-resolution
    artifact against a pre-resolution merge and report the boundary lossy when nothing was lost —
    on one host, 6 engine drains differing in exactly that one derived field and no other
    (okengine#814). Everything else, including a prompt on a job with no prompt_file, still
    participates.
    """
    def source_only(job: dict) -> dict:
        if not job.get("prompt_file") or "prompt" not in job:
            return job
        return {k: v for k, v in job.items() if k != "prompt"}
    return sorted((json.dumps(source_only(j), sort_keys=True) for j in jobs))


def _extension_compose():
    """The sibling extension_compose module, loaded by path, or None when it is absent."""
    p = Path(__file__).resolve().parent / "extension_compose.py"
    if not p.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("extension_compose", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _extension_pass(pack_dir: Path, existing: list[dict]) -> tuple[list[dict], list[str]]:
    """Fold enabled-extension cron jobs into the deployed fleet (#113 composer).

    A no-op when the pack has no `.okengine/extensions.yaml` or the composer module
    is absent — so this is zero-impact until an operator enables an extension."""
    m = _extension_compose()
    if m is None:
        return [], []
    return m.extension_jobs(pack_dir, {j["name"] for j in existing})


def build_fleet(pack_dir: Path) -> tuple[list[dict], str]:
    """The deployable fleet for ONE deployment, computed without touching any file.

    Returns ``(jobs, summary)``. regen writes these jobs to the shared checkout artifact; a
    per-deployment consumer (ensure-runtime's lane writers, okengine#1029) calls this directly, so
    it never reads another pack's job set from whichever deployment was split last in the checkout.
    Raises SystemExit on a composition or gate error, exactly as regen does."""
    pack_dir = Path(pack_dir)
    engine = _load_jobs(ENGINE_CRONS_FILE)
    # A pack with ONLY engine crons has no crons/ dir; tolerate its absence (empty domain set) so
    # regen can run for EVERY pack. Otherwise the deploy skipped regen for such packs and shipped a
    # stale leftover — possibly another pack's job set on a multi-pack host (invariant-audit #12).
    _dc, _dp = pack_dir / "crons" / DOMAIN_CRONS, pack_dir / "crons" / DOMAIN_PROMPTS
    # _load_jobs, not a bare json.loads: a domain lane's `prompt_file` must resolve against the
    # pack, as it does on the composed path (discover_packs). Skipping it deployed promptless lanes.
    domain = _load_jobs(_dc) if _dc.is_file() else []
    pname = _pack_name(pack_dir)
    for j in domain:                          # provenance marker (okengine#143) so split/dump
        j.setdefault("pack", pname)           # can route a pack's own crons back to domain
    prompts = _load_prompts(_dp) if _dp.is_file() else []
    # Before merging: an engine-template lane this pack does not drive is dropped by merge, so a
    # domain twin of it would collide with nothing and ship under the lane's name (okengine#969).
    shadows = domain_shadow_errors(pname, domain, {j["name"] for j in engine})
    if shadows:
        raise SystemExit("cron domain job-name errors (not deploying):\n  " + "\n  ".join(shadows))
    merged = merge(engine, domain, prompts, tier_of=_tier_map(TIERS))
    ext_count = -len(merged)
    merged = finalize_fleet(merged, pack_dir)
    ext_count += len(merged)
    return merged, (f"{len(engine)} engine-half + {len(domain)} domain + {len(prompts)} "
                    f"prompts + {ext_count} extension")


def regen() -> list[dict]:
    """Generate config/cron-plus-jobs.json from the engine half + the domain pack +
    any enabled extensions (#113). Fail-loud before writing on a composition error."""
    merged, summary = build_fleet(host_dir())   # okengine#970: never the placeholder
    JOBS.write_text(_dump_jobs(merged), encoding="utf-8")
    print(f"regen: {summary} -> {JOBS.name} ({len(merged)} jobs)")
    return merged


def _unpublish_writer(job: dict) -> dict:
    """Undo _publish_writer for a live->source dump: the prefix is derived at every regen, so
    baking one bound name back into a source is the staleness #852 removed."""
    env = job.get("env")
    if not isinstance(env, dict) or WRITE_TOOL_PREFIX_ENV not in env:
        return job
    job = dict(job)
    env = dict(env)
    prefix = env.pop(WRITE_TOOL_PREFIX_ENV)
    if env:
        job["env"] = env
    else:
        job.pop("env", None)
    if isinstance(job.get("prompt"), str) and isinstance(prefix, str) and prefix:
        job["prompt"] = job["prompt"].replace(prefix, WRITER_PLACEHOLDER)
    return job


def _restore_source_reprs(jobs: list[dict]) -> list[dict]:
    """dump-from-live reads the DEPLOYED jobs.json, whose schedules + models were EXPANDED by the
    deploy-only transform (deploy-cron-plus-jobs.sh): cron_jitter turned `@jitter:*`/`@morning[:MM]`
    into a concrete PER-INSTALL cron expr, and model_profiles expanded an `@profile` model ref into
    concrete provider/base_url/model/ollama_num_ctx. Writing those resolved values back to the SHARED
    source would bake ONE install's jitter minute + endpoint into everyone's source, permanently
    destroying the sentinel / profile indirection (invariant-audit M20). For every job that still
    exists in the current source, restore the source's sentinel schedule + `@profile` model (and drop
    the deploy-baked profile fields), so a live->source round-trip is idempotent for the deploy-only
    transforms. Non-transformed edits (enabled, prompt, after, new jobs) are still captured from live."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import cron_jitter
        import model_profiles
    except Exception:
        undone = [_unpublish_writer(j) for j in jobs]  # resolver libs absent -> capture live verbatim
        return jobs if all(a is b for a, b in zip(undone, jobs)) else undone
    src_by_name: dict[str, dict] = {}
    src_by_id: dict[str, dict] = {}
    for f in (ENGINE_CRONS_FILE, PACK_DIR / "crons" / DOMAIN_CRONS):
        try:
            for j in json.loads(f.read_text()):
                src_by_name.setdefault(j.get("name"), j)
                if j.get("id"):
                    src_by_id.setdefault(j.get("id"), j)
        except Exception:
            pass
    out = []
    for j in jobs:
        j = _unpublish_writer(j)
        s = src_by_name.get(j.get("name")) or src_by_id.get(j.get("id"))   # also match a RENAMED job by id
        if s:
            j = dict(j)
            # Schedule: use cron_jitter._job_expr — it reads ALL THREE documented shapes (dict,
            # bare-string, top-level `expr`); a hand-rolled `schedule.expr` read misses the top-level
            # shape and re-bakes its per-install jitter (re-verify). Restore the source's schedule
            # representation verbatim (drop the live resolved fields first).
            if cron_jitter.is_sentinel(cron_jitter._job_expr(s) or "") \
                    or cron_jitter.is_morning_sentinel(cron_jitter._job_expr(s) or ""):
                j.pop("schedule", None)
                j.pop("expr", None)
                if "schedule" in s:
                    j["schedule"] = s["schedule"]
                if "expr" in s:
                    j["expr"] = s["expr"]
            # Model: restore the @profile ref, but only DROP a profile field the deploy actually baked
            # (absent from source); a field the SOURCE set independently of the profile is preserved.
            if model_profiles.is_ref(s.get("model") or ""):
                j["model"] = s["model"]
                for pf in model_profiles.PROFILE_FIELDS:
                    if pf == "model":
                        continue
                    if pf in s:
                        j[pf] = s[pf]                   # source-set field -> keep source's value
                    else:
                        j.pop(pf, None)                 # deploy-baked endpoint field -> drop
        out.append(j)
    return out


def dump_from_live(livefile: str) -> None:
    """Capture live scheduler state -> engine half + pack, then regenerate."""
    host_dir()                                # before writing either half (okengine#970)
    live = json.loads(Path(livefile).read_text())
    jobs = sanitize(live["jobs"] if isinstance(live, dict) else live)
    jobs = _restore_source_reprs(jobs)   # keep source sentinels/@profiles, not the deploy-baked values (M20)
    parts = split(jobs, _tier_map(TIERS))
    ENGINE_CRONS_FILE.write_text(_dump_list(parts[ENGINE_CRONS]), encoding="utf-8")
    (PACK_DIR / "crons").mkdir(parents=True, exist_ok=True)
    (PACK_DIR / "crons" / DOMAIN_CRONS).write_text(_dump_list(parts[DOMAIN_CRONS]), encoding="utf-8")
    (PACK_DIR / "crons" / DOMAIN_PROMPTS).write_text(_dump_prompts(parts[DOMAIN_PROMPTS]), encoding="utf-8")
    print(f"dump: live -> engine-crons.json ({len(parts[ENGINE_CRONS])}) + pack "
          f"domain ({len(parts[DOMAIN_CRONS])}) + prompts ({len(parts[DOMAIN_PROMPTS])})")
    regen()


def validate_ordering(jobs: list[dict]) -> tuple[list[str], list[str]]:
    """Validate cross-job `after:` dependencies (okengine#129): every `after` target must name a
    job in the fleet, and the dependency graph must be acyclic. Returns (topological order of job
    names, errors). A non-empty errors list means the fleet's ordering is unsound — do not deploy.

    This is the enforcement GATE for the ordering contract: `after:` is a HARD dependency (a lane
    that consumes another lane's output), unlike `tier:` (an advisory kickstart-stage hint).
    Runtime ordering (staggered schedules / wake-gate freshness) is a later phase; this catches a
    broken or circular dependency before it ships.

    Gates the DEPLOYED shape: only ENABLED jobs (disabled `enabled:false` placeholders never reach
    jobs.json — see _dump_jobs), so an enabled lane whose `after:` names a DISABLED job is a dangling
    dependency in the shipped artifact and must fail HERE, not deploy silently. Before this, ordering
    validated the full pre-filter set (disabled targets counted as present) while _dump_jobs dropped
    them — a real gate hole. Mirrors validate_unique_ids, which gates the same enabled/deployed set
    (invariant-audit #351)."""
    from collections import deque
    jobs = [j for j in jobs if runs(j)]      # the deployed set (disabled never ship)
    by_name = {j["name"]: j for j in jobs}
    errors: list[str] = []
    adj: dict[str, list[str]] = {n: [] for n in by_name}
    indeg: dict[str, int] = {n: 0 for n in by_name}
    for j in jobs:
        for a in (j.get("after") or []):
            if not isinstance(a, str):
                # A non-string after: entry is a malformed dependency; dropping it silently (the old
                # `continue`) vanished an intended ordering edge with every gate green. Fail loud,
                # like the missing-target / self / cycle branches below (invariant-audit #351).
                errors.append(f"job {j['name']!r} has a non-string after: entry {a!r} "
                              f"(after: targets must be job-name strings)")
                continue
            if a == j["name"]:
                errors.append(f"job {j['name']!r} declares after: itself")
                continue
            if a not in by_name:
                errors.append(f"job {j['name']!r} declares after: {a!r} but no such job in the fleet")
                continue
            adj[a].append(j["name"])
            indeg[j["name"]] += 1
    q = deque(sorted(n for n, d in indeg.items() if d == 0))
    order: list[str] = []
    while q:
        n = q.popleft()
        order.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if len(order) != len(by_name):
        cyc = sorted(n for n in by_name if n not in set(order))
        errors.append(f"cyclic after: dependency among jobs: {cyc}")
    return order, errors


def validate_unique_ids(jobs: list[dict]) -> list[str]:
    """cron-plus keys every job by `id` (minted from `name` when absent — see _ensure_id), so a
    duplicate deployed id — or name, which mints a colliding id — means one job silently runs the
    OTHER's definition and the twin NEVER fires: a whole lane vanishes with every gate green
    (invariant-audit M37). The multi-pack composer prefixes names to avoid this, but single-pack
    merge + the extension pass have no gate, and validate_ordering/_by_name both collapse a dup name
    into one dict entry (so they can't see it). Check the FINAL deployed shape (post _ensure_id) over
    the ENABLED jobs (disabled placeholders never deploy). Returns errors — non-empty = do not deploy."""
    errors: list[str] = []
    seen_id: dict[str, str] = {}
    seen_name: dict[str, str] = {}
    for j in (_ensure_id(x) for x in jobs if runs(x)):
        jid, nm = j.get("id"), j.get("name")
        if jid in seen_id:
            errors.append(f"duplicate cron id {jid!r}: {seen_id[jid]!r} and {nm!r} "
                          "(cron-plus keys by id — one runs the other's def, the twin never fires)")
        else:
            seen_id[jid] = nm
        if nm in seen_name:
            errors.append(f"duplicate cron name {nm!r} (mints a colliding id)")
        else:
            seen_name[nm] = jid
    return errors


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="check",
                    choices=["check", "split", "merge", "regen", "compose", "dump"])
    ap.add_argument("--in", dest="indir")
    ap.add_argument("--out", dest="out")
    ap.add_argument("--live", dest="live", help="live jobs.json for the dump command")
    ap.add_argument("--packs", dest="packs",
                    help="packs directory for the compose command (N-way)")
    args = ap.parse_args(argv)

    if args.cmd == "regen":
        regen()
        return 0
    if args.cmd == "compose":
        pdir = args.packs or os.environ.get("CRON_PACKS_DIR")
        if not pdir:
            ap.error("compose requires --packs DIR (or CRON_PACKS_DIR)")
        # The CLI IS regen_composed: one path, one gate list (okengine#968). It kept its own
        # shorter validator list and skipped writer binding, so its output diverged from the
        # deploy path's and named the generic admin writer for every composed lane.
        try:
            jobs = regen_composed(Path(pdir))
        except SystemExit as exc:
            print(f"composition errors (not writing):\n{exc}")
            return 1
        print(f"composed {len(jobs)} jobs from {pdir} -> {JOBS.name}")
        return 0
    if args.cmd == "dump":
        if not args.live:
            ap.error("dump requires --live <jobs.json>")
        dump_from_live(args.live)
        return 0

    tier_of = _tier_map(TIERS)

    if args.cmd == "split":
        jobs = _load_jobs(JOBS)
        parts = split(jobs, tier_of)
        out = Path(args.out or (REPO / "build" / "cron-pack"))
        _write_split(parts, out)
        print(f"split {len(jobs)} jobs -> {out}/")
        print(f"  {ENGINE_CRONS}: {len(parts[ENGINE_CRONS])} (engine + engine-template scripts)")
        print(f"  {DOMAIN_CRONS}: {len(parts[DOMAIN_CRONS])} (domain)")
        print(f"  {DOMAIN_PROMPTS}: {len(parts[DOMAIN_PROMPTS])} engine-template prompts")
        return 0

    if args.cmd == "merge":
        d = Path(args.indir or (REPO / "build" / "cron-pack"))
        engine = json.loads((d / ENGINE_CRONS).read_text())
        domain = json.loads((d / DOMAIN_CRONS).read_text())
        prompts = json.loads((d / DOMAIN_PROMPTS).read_text())
        ext_file = d / EXTENSIONS
        extensions = json.loads(ext_file.read_text()) if ext_file.exists() else []
        merged = merge(engine, domain, prompts, extensions=extensions)
        text = json.dumps({"jobs": merged}, indent=2) + "\n"
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"merged {len(merged)} jobs -> {args.out}")
        else:
            sys.stdout.write(text)
        return 0

    # check: round-trip. Extension-tier jobs aren't a cron-tiers source — they're folded fresh
    # by the composer at deploy — so split routes them to their own EXTENSIONS partition, which
    # must pass back through merge unchanged for the round-trip to be lossless (#152/#141).
    jobs = _load_jobs(JOBS)
    with tempfile.TemporaryDirectory() as td:
        parts = split(jobs, tier_of)
        _write_split(parts, Path(td))
        engine = json.loads((Path(td) / ENGINE_CRONS).read_text())
        domain = json.loads((Path(td) / DOMAIN_CRONS).read_text())
        prompts = json.loads((Path(td) / DOMAIN_PROMPTS).read_text())
        extensions = json.loads((Path(td) / EXTENSIONS).read_text())
        merged = merge(engine, domain, prompts, extensions=extensions)
    ok = _canon(jobs) == _canon(merged)
    print(f"round-trip: input={len(jobs)} jobs, "
          f"split=({len(parts[ENGINE_CRONS])} engine-half / "
          f"{len(parts[DOMAIN_CRONS])} domain / {len(parts[DOMAIN_PROMPTS])} prompts), "
          f"merged={len(merged)} jobs")
    # The deploy gates, from the one list regen and compose run (okengine#970): this subcommand
    # kept its own shorter copy, so an artifact could pass `check` and still fail the deploy.
    gate_errors = [(label, e) for label, errors in fleet_errors(jobs) for e in errors]
    if gate_errors:
        print("✗ GATES", file=sys.stderr)
        for label, e in gate_errors:
            print(f"  {label}: {e}", file=sys.stderr)
    if ok and not gate_errors:
        ndeps = sum(len(j.get("after") or []) for j in jobs)
        print(f"✓ lossless — boundary is real (merge(split(x)) == x); "
              f"after: graph acyclic ({ndeps} dep edge(s))")
        return 0
    if not ok:
        a, b = set(_canon(jobs)), set(_canon(merged))
        print("✗ MISMATCH", file=sys.stderr)
        for j in list(a - b)[:3]:
            print("  only in input:", j[:200], file=sys.stderr)
        for j in list(b - a)[:3]:
            print("  only in merged:", j[:200], file=sys.stderr)
    return 1


def missing_lane_scripts(jobs, scripts_root):
    """Lane `script`s not staged under scripts_root — the deploy guard's logic. A lane whose
    script does not resolve runs the agent with NO wake-gate and writes nothing (silent no-op);
    this is exactly how an enabled extension whose scripts were never deploy-cron-scripts'd goes
    dark. Returns [(lane_name, script)] for each missing one."""
    import os
    PREFIX = "/opt/data/scripts/"
    out = []
    for j in jobs:
        s = (j.get("script") or "").strip()
        if not s:
            continue
        rel = s[len(PREFIX):] if s.startswith(PREFIX) else s
        if not os.path.isfile(os.path.join(scripts_root, rel)):
            out.append((j.get("name"), s))
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
