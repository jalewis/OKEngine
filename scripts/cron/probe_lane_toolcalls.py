#!/usr/bin/env python3
"""Pre-flight: does each write lane's ACTUAL tool array still produce structured tool calls?

The failure this exists for is silent. A serving stack can accept a request, generate a
perfectly correct tool call, fail to parse it into `tool_calls`, and return it as prose with
`finish_reason: stop`. The run looks successful, the lane writes nothing, and nobody notices —
okengine#518 / jlew/ollama-friday#6, where 11 such runs went unremarked across 7,567 lane
receipts until someone went looking.

`detect_narrated_tool_calls` (okengine#477) catches it AFTER a lane has burned a run on real
work. This catches it BEFORE, by asking the only question that matters: given the tool array
this lane actually ships, does the endpoint return a structured call?

WHY IT PROBES THE REAL ARRAY. Whether the parse succeeds turned out to depend on the exact
serialised tool block — not tool count, not byte size, not schema shape, none of which predicted
it. Sizes that fail sit next to sizes that do not. So a generic "can this model tool-call?"
check proves nothing about a specific lane; only the lane's own array does. Each engine-managed
write identity is launched exactly as the gateway launches it (`OKENGINE_WRITE_ACTOR` from
config.yaml) and asked for its tool list, so the array under test is the one the lane sends.

Nothing is written. The probe never executes a returned call — it only checks the shape.

Exit status: 0 all lanes structured, 1 any lane degraded, 2 could not run the probe at all.
An UNPARSEABLE or unreachable endpoint is status 2, never a pass — "no data" is not "data
says yes" (okengine#477).

Usage (inside the gateway):
  python3 /opt/data/scripts/probe_lane_toolcalls.py
  python3 /opt/data/scripts/probe_lane_toolcalls.py --reps 6 --lane cron:raw-backfill
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

# The narration tells, kept identical to run_receipts._NARRATED_TAG so the pre-flight and the
# post-hoc detector agree on what a dropped call looks like.
NARRATED = ("</tool_call>", "<tool_call>", "<tools>", "<function=", "<parameter=")

PROMPT = ("Call your available tool exactly once with placeholder arguments, then stop. "
          "This is a connectivity check — do not explain, do not ask, just make the call.")


def load_config(pack_data: Path) -> dict:
    return yaml.safe_load((pack_data / "config.yaml").read_text(encoding="utf-8")) or {}


def lane_identities(cfg: dict) -> list[tuple[str, str]]:
    """(mcp server key, OKENGINE_WRITE_ACTOR) for every engine-managed write identity."""
    out = []
    for key, spec in (cfg.get("mcp_servers") or {}).items():
        actor = ((spec or {}).get("env") or {}).get("OKENGINE_WRITE_ACTOR")
        if actor:
            out.append((key, str(actor)))
    return sorted(set(out))


async def _tools_for(actor: str) -> list:
    """The tool list the gateway would hand this lane, from the server it actually launches."""
    os.environ["OKENGINE_WRITE_ACTOR"] = actor
    for mod in ("write_server",):
        sys.modules.pop(mod, None)
    sys.path.insert(0, "/opt/hermes/okengine-mcp")
    import write_server as W                    # re-imported per actor: registration is gated
    return await W.mcp.list_tools()


def build_array(server_key: str, tools) -> list[dict]:
    prefix = "mcp__" + server_key.replace("-", "_") + "__"
    return [{"type": "function", "name": prefix + t.name, "strict": False,
             "description": t.description or "", "parameters": t.inputSchema}
            for t in tools]


def is_http_url(url: str) -> bool:
    """`base_url` comes from config.yaml, so it is attacker-adjacent input to `urlopen`, which
    happily opens `file:` and other schemes (bandit B310 / CWE-22). A probe that silently read
    /etc/passwd and reported a verdict would be worse than one that fails."""
    try:
        return urllib.parse.urlparse(url).scheme in ("http", "https")
    except ValueError:
        return False


def probe(url: str, model: str, tools: list[dict], timeout: int) -> str:
    payload = {"model": model, "temperature": 0, "stream": False, "store": False,
               "max_output_tokens": 512, "tool_choice": "auto", "tools": tools,
               "input": [{"role": "user",
                          "content": [{"type": "input_text", "text": PROMPT}]}]}
    if not is_http_url(url):
        return "unreachable"
    req = urllib.request.Request(url.rstrip("/") + "/responses",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        # B310 is suppressed on the next line only because `is_http_url` gates the scheme both
        # here and at startup, so `file:` and custom schemes from config.yaml cannot reach it.
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            body = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return "unreachable"
    calls, text = 0, []
    for item in body.get("output") or []:
        if item.get("type") in ("function_call", "tool_call"):
            calls += 1
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and chunk.get("text"):
                text.append(chunk["text"])
    if calls:
        return "structured"
    return "narrated" if any(t in "\n".join(text) for t in NARRATED) else "silent"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="/opt/data", help="pack .hermes-data mount")
    # Defaults come from env so the cron lane can carry them in its `env` block: the deploy
    # validates `script` with os.path.isfile, so arguments cannot live in that field.
    ap.add_argument("--reps", type=int, default=int(os.environ.get("OKENGINE_PROBE_REPS") or 3))
    ap.add_argument("--lane", default="", help="probe only this OKENGINE_WRITE_ACTOR")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--backoff", type=float, default=5.0,
                    help="seconds to wait before retrying an unreachable probe")
    ap.add_argument("--cron", action="store_true",
                    default=os.environ.get("OKENGINE_PROBE_CRON", "").strip().lower()
                    not in ("", "0", "false", "no", "off"),
                    help="scheduled mode: append a verdict to the vault log and emit the "
                         "no_agent wakeAgent contract")
    ap.add_argument("--wiki", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    args = ap.parse_args(argv)

    data = Path(args.data)
    try:
        cfg = load_config(data)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"probe: cannot read config.yaml: {exc}", file=sys.stderr)
        return 2
    model_cfg = cfg.get("model") or {}
    base_url, model = str(model_cfg.get("base_url") or ""), str(model_cfg.get("default") or "")
    if not base_url or not model:
        print("probe: model.base_url / model.default not configured", file=sys.stderr)
        return 2
    if not is_http_url(base_url):
        print(f"probe: model.base_url must be http(s) — refusing {base_url!r}", file=sys.stderr)
        return 2

    lanes = [(k, a) for k, a in lane_identities(cfg) if not args.lane or a == args.lane]
    if not lanes:
        print("probe: no engine-managed write identities found — nothing to check",
              file=sys.stderr)
        return 2

    # Group lanes by their ACTUAL array and probe each DISTINCT array once. In practice this
    # rarely collapses anything — every lane's tools are named `mcp__<its own server>__tool`, so
    # 52 lanes really are 52 byte-distinct arrays even though 43 of them share schemas. That is
    # the honest answer, not a disappointing one: the failure depends on the exact serialised
    # block, so folding arrays together on "same schemas, different names" would test something
    # the lanes do not send. The grouping stays for the case where lanes DO share a server.
    groups: dict[str, dict] = {}
    unknown, degraded = [], []
    for server_key, actor in lanes:
        try:
            tools = build_array(server_key, asyncio.run(_tools_for(actor)))
        except Exception as exc:                                   # noqa: BLE001
            print(f"probe: {actor}: could not list tools: {exc}", file=sys.stderr)
            unknown.append(actor)
            continue
        if not tools:
            print(f"probe: {actor}: no tools bound — nothing to probe")
            continue
        # Names differ per bound server (mcp__<server>__tool) while schemas are shared, so the
        # fingerprint covers both: two lanes are the same probe only if they send the same bytes.
        fp = json.dumps(tools, sort_keys=True)
        groups.setdefault(fp, {"tools": tools, "lanes": []})["lanes"].append(actor)

    print(f"probe: {model} @ {base_url}")
    print(f"  {len(lanes)} lane(s) -> {len(groups)} distinct tool array(s) x {args.reps} reps")
    print(f"{'array':<38} {'tools':>5} {'lanes':>5} {'structured':>11} {'narrated':>9} {'other':>6}")
    for group in groups.values():
        tools, members = group["tools"], group["lanes"]
        tally = {"structured": 0, "narrated": 0, "silent": 0, "unreachable": 0}
        for _ in range(args.reps):
            verdict = probe(base_url, model, tools, args.timeout)
            if verdict == "unreachable":
                # A full sweep is dozens of generations against an endpoint serving 2 slots
                # under admission control, so it can queue past the timeout and time ITSELF
                # out. That is load, not a broken endpoint, and reporting UNKNOWN for it makes
                # the probe useless exactly when it is doing its job. Retry once, slowly,
                # before believing it — a genuinely unreachable endpoint fails both times.
                time.sleep(args.backoff)
                verdict = probe(base_url, model, tools, args.timeout)
            tally[verdict] += 1
        other = tally["silent"] + tally["unreachable"]
        label = members[0] if len(members) == 1 else f"{members[0]} (+{len(members) - 1})"
        print(f"{label:<38} {len(tools):>5} {len(members):>5} {tally['structured']:>11} "
              f"{tally['narrated']:>9} {other:>6}")
        if tally["unreachable"]:
            unknown.extend(members)
        elif tally["structured"] < args.reps:
            degraded.extend(members)

    if unknown:
        print(f"\nUNKNOWN — endpoint unreachable for {len(unknown)} lane(s): "
              + ", ".join(unknown), file=sys.stderr)
        print("This is NOT a pass. No data is not data saying yes.", file=sys.stderr)
        return _finish(args, 2, f"UNKNOWN — endpoint unreachable for {len(unknown)} lane(s)")
    if degraded:
        print(f"\nDEGRADED — {len(degraded)} lane(s) did not return a structured tool call "
              "on every rep:", file=sys.stderr)
        for actor in degraded:
            print(f"  {actor}", file=sys.stderr)
        print("A lane in this state burns its run and writes nothing, while reporting success.",
              file=sys.stderr)
        return _finish(args, 1, "DEGRADED — tool calls not parsed for: " + ", ".join(degraded))
    print("\nall lanes returned structured tool calls.")
    return _finish(args, 0, f"ok — {len(lanes)} lane(s) returned structured tool calls")


def _finish(args, code: int, summary: str) -> int:
    """Scheduled-mode reporting. Interactive runs are unchanged.

    `wakeAgent` is always false, deliberately: a dropped tool call is a serving-stack fault and
    there is nothing an agent could usefully do about it. The signal belongs where an operator
    looks — a durable line in the vault log, plus a non-zero exit so the run records as failed —
    not in a wake that would burn a model turn to restate the problem.
    """
    if not args.cron:
        return code
    if code != 0:
        try:
            log = Path(args.wiki) / "wiki" / "log.md"
            stamp = _dt.date.today().isoformat()
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"- {stamp} lane-toolcall-probe {summary}\n")
        except OSError as exc:
            print(f"probe: could not write the vault log: {exc}", file=sys.stderr)
    print(json.dumps({"wakeAgent": False}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
