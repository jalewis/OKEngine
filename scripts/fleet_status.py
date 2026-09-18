#!/usr/bin/env python3
"""Fleet health view — okengine#64 (observability), first slice.

Surfaces what you'd otherwise only find by grepping the gateway's container logs: per-lane
run outcomes and the *silent-failure* signals (vault-write denials, provider payment/credit
errors, job overlaps, blocked tools, read-loops). Reads the deployed cron-plus state + run
logs under a data dir (default `/opt/data`) — domain-agnostic, no engine source needed.

Run it via ``scripts/fleet-status.sh`` (which execs it inside the gateway). The pure
functions (``classify_log`` / ``scan_signals`` / ``build_report``) are unit-tested.
"""
from __future__ import annotations

import glob
import importlib.util
import ipaddress
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

# --- run-outcome classification (one log's text -> a verdict) ----------------

def classify_log(text: str) -> str:
    """A completion line cannot clear a real MCP registry or transport error."""
    if _MCP_REGISTRY_CALL_ERROR.search(text):
        return "registry_lost"
    if _MCP_UNREACHABLE_CALL_ERROR.search(text):
        return "mcp_unreachable"
    if "completed successfully" in text:
        return "ok"
    if "[SILENT]" in text or "wakeAgent=false" in text:
        return "silent"
    return "incomplete"


# --- silent-failure signals (the stuff that hides in WARNING lines) ----------

_EXECUTOR_ENTRY = (
    r"(?m)^(?:\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[,.]\d+)?\s+\w+\s+"
    r"(?:\[[^\]\n]+\]\s+)?)?agent\.tool_executor:\s*"
)
_MCP_REGISTRY_CALL_ERROR = re.compile(
    _EXECUTOR_ENTRY
    + r"Tool\s+mcp__[\w-]+\s+returned error[^\n]*Unknown tool:\s*mcp__[\w-]+", re.I
)
_MCP_UNREACHABLE_CALL_ERROR = re.compile(
    _EXECUTOR_ENTRY
    + r"Tool\s+mcp__[\w-]+\s+returned error[^\n]*MCP server '[^'\n]+' "
    r"(?:is unreachable after \d+ consecutive failures|transport is down\b)", re.I
)
SIGNALS: dict[str, str] = {
    "vault write denied (#140)":      r"Write denied:.*protected system/credential",
    "provider payment/credit error":  r"payment\s*/\s*credit error|insufficient[^\n]*credit",
    "job-overlap skip (runs > interval)": r"skipped\s+—\s+previous run still active",
    "execute_code blocked (cron safety)": r"BLOCKED: execute_code",
    "agent read-loop blocked":        r"called read_file on this exact region",
    "agent tool error":               r"agent\.tool_executor: Tool \w+ returned error",
    "MCP registry loss (#608)":       _MCP_REGISTRY_CALL_ERROR.pattern,
    "MCP transport unavailable (#608)": _MCP_UNREACHABLE_CALL_ERROR.pattern,
    "direct-writer slug collision (#647)": r"deterministic writer introduced separator-equivalent page",
}
# signals that mean something is actually broken (drive the exit code), vs expected/benign noise.
CRITICAL = {"vault write denied (#140)", "provider payment/credit error",
            "MCP registry loss (#608)", "MCP transport unavailable (#608)",
            "direct-writer slug collision (#647)"}


def scan_signals(text: str) -> dict[str, int]:
    return {label: len(re.findall(pat, text, re.I)) for label, pat in SIGNALS.items()}


# --- model usage (which model served, and the free/paid split) ---------------

_MODEL = re.compile(r"model=([a-z0-9/._:-]+)")
# The provider line the agent logs per call: `... base_url=<url> model=<id> ...`. Only about 60%
# of `model=` occurrences carry it — the rest are bare progress lines ("API call #3: model=...") —
# so endpoints are mapped model-wide across the window rather than matched call-by-call.
_ENDPOINT_MODEL = re.compile(r"base_url=(\S+)\s+model=([a-z0-9/._:-]+)")


def is_local_endpoint(url: str) -> bool:
    """True when the URL names a host we serve ourselves — loopback, a private/link-local
    address, or a LAN-only name. Self-hosted inference is what costs $0.

    Classified by ADDRESS, never by an allowlist of hostnames: the engine ships no domain
    knowledge, so a deployment's own inference host must be recognised without naming it, and
    a newly added local model needs no entry here.
    """
    # urlsplit needs a scheme to find a host; a bare `host:port` gets one.
    host = urllib.parse.urlsplit(url if "//" in url else f"//{url}").hostname or ""
    if not host:
        return False
    host = host.lower()
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith(
            (".local", ".lan", ".internal", ".localdomain"))
    # is_private is the WHOLE test: it already covers loopback (127/8, ::1), link-local
    # (169.254/16, fe80::/10) and unique-local (fc00::/7). Spelling those out again as extra
    # `or` clauses reads as defensive but is dead — no address satisfies them without also
    # satisfying is_private, so the clauses can never change the answer.
    return addr.is_private


def is_free_model(model_id: str, endpoints: set[str] | None = None) -> bool:
    """What costs $0, for the cost-offload stat. Two independent ways a call is free:

      * the model NAME is an OpenRouter free tier (`:free`, `openrouter/free`);
      * the model was SERVED from a local endpoint (self-hosted inference).

    The endpoint test is why this needs more than a name (#610). After the llama.cpp migration
    most fleet traffic goes to a locally served model whose name carries no free-tier marker,
    and the name-only rule reported every deployment at `0% on free tiers` while ~95% of calls
    cost nothing — a number that read 0% both before and after the migration it was meant to
    measure, and would not move if the local host died and every lane failed over to a paid API.

    A model observed at BOTH a local and a remote endpoint counts as PAID. The bias is
    deliberate: understating offload is a cheap mistake, overstating it hides real spend.
    """
    m = model_id.lower()
    if m.endswith(":free") or m == "openrouter/free":
        return True
    return bool(endpoints) and all(is_local_endpoint(u) for u in endpoints)


def count_models(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in _MODEL.findall(text):
        if m == "model":            # a 'model=model' placeholder log artifact, not a real id
            continue
        out[m] = out.get(m, 0) + 1
    return out


def map_model_endpoints(text: str) -> dict[str, set[str]]:
    """model id -> the set of base_urls observed serving it. A model absent from the result was
    never logged with a provider, which is NOT the same as being paid — see the `PAID?` tag."""
    out: dict[str, set[str]] = {}
    for url, model in _ENDPOINT_MODEL.findall(text):
        if model == "model":
            continue
        out.setdefault(model, set()).add(url)
    return out


# --- I/O + report ------------------------------------------------------------

_TS = re.compile(r"(\d{8})-(\d{6})\.log$")
_ERROR_ENTRY_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:[,.]\d+)?\s")
_ERROR_LOG_NAME = re.compile(r"errors\.log(?:\.\d+)?$")


def _now() -> float:
    return time.time()


def _load_jobs(data_dir: str) -> list[dict]:
    p = os.path.join(data_dir, "cron-plus", "jobs.json")
    try:
        j = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return j["jobs"] if isinstance(j, dict) else j


def _recent_logs(data_dir: str, window_h: float) -> list[str]:
    cutoff = _now() - window_h * 3600
    out = []
    for f in glob.glob(os.path.join(data_dir, "logs", "cron-plus", "*.log")):  # glob-ok: flat cron-plus logs dir, not a sharded namespace
        try:
            if os.path.getmtime(f) >= cutoff:
                out.append(f)
        except OSError:
            pass
    return out


def _recent_error_signals(data_dir: str, cutoff: float) -> tuple[dict[str, int], bool, bool]:
    """Read current and numeric rotated aggregate logs by event time, not inode mtime.

    Continuation lines inherit the previous entry timestamp. A matching signal
    before any parseable timestamp has unknown age; an unreadable matched file
    is reported separately so the health view cannot silently certify a gap.
    """
    counts = {label: 0 for label in SIGNALS}
    unknown_age = False
    unreadable = False
    # glob-ok: aggregate logs are flat files in one logs directory, not sharded wiki pages.
    for path in glob.glob(os.path.join(data_dir, "logs", "errors.log*")):
        if not _ERROR_LOG_NAME.fullmatch(os.path.basename(path)):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as stream:
                event_time: float | None = None
                for line in stream:
                    match = _ERROR_ENTRY_TS.match(line)
                    if match:
                        try:
                            event_time = datetime.fromisoformat(match.group(1)).timestamp()
                        except ValueError:
                            event_time = None
                    if event_time is not None and event_time < cutoff:
                        continue
                    line_counts = scan_signals(line)
                    if event_time is None and any(line_counts.values()):
                        unknown_age = True
                    for label, number in line_counts.items():
                        counts[label] += number
        except OSError:
            unreadable = True
    return counts, unknown_age, unreadable


def _job_of(logpath: str) -> str:
    # <jobname>-YYYYMMDD-HHMMSS.log  ->  <jobname>
    return re.sub(r"-\d{8}-\d{6}\.log$", "", os.path.basename(logpath))


def _receipt_counts(data_dir: str, cutoff: float) -> dict[str, int]:
    totals = {k: 0 for k in ("selected", "accepted", "rejected", "deferred", "undisposed")}
    pattern = os.path.join(data_dir, "cron-plus", "receipts", "*", "*.json")
    for path in glob.glob(pattern):  # glob-ok: fixed cron-plus receipt layout, not a wiki namespace
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            counts = json.load(open(path, encoding="utf-8")).get("counts") or {}
            for key in totals:
                totals[key] += int(counts.get(key) or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return totals


_starvation_module = None
_starvation_looked_up = False


def _starvation_lib(data_dir: str):
    """The shared scan (scripts/cron/slot_starvation.py), or None where it cannot be resolved.

    Resolved BY PATH because this file has no importable siblings in its primary layout: it is
    streamed into the gateway on stdin by fleet-status.sh (`python3 - /opt/data ...`), where
    __file__ does not exist and sys.path holds nothing useful. Two locations cover every way this
    runs -- the STAGED copy beside the cron scripts inside a gateway, and the repo checkout when
    loaded as a file.
    """
    global _starvation_module, _starvation_looked_up
    if _starvation_looked_up:
        return _starvation_module
    _starvation_looked_up = True
    candidates = [os.path.join(data_dir, "scripts", "slot_starvation.py")]
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "cron", "slot_starvation.py"))
    except NameError:                      # streamed via stdin: no __file__, staged copy only
        pass
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            spec = importlib.util.spec_from_file_location("slot_starvation", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _starvation_module = module
            break
        except Exception:                  # a broken lib must not take the whole report down
            continue
    return _starvation_module


def _slot_starvation(data_dir: str, cutoff: float, agent_lanes: set[str]) -> dict:
    """Delegate to the shared library. An unresolvable library is UNKNOWN, never a clean zero."""
    lib = _starvation_lib(data_dir)
    if lib is None:
        # Shape MUST match slot_starvation.scan()'s. A fallback missing a key crashes the whole
        # report in exactly the degraded case it exists to survive.
        return {"starved": 0, "stalled": 0, "killed": 0, "runs": 0, "measurable": False,
                "lanes": {}, "stalled_lanes": {}}
    return lib.scan(data_dir, cutoff, agent_lanes)


def build_report(data_dir: str = "/opt/data", window_h: float = 24.0) -> tuple[str, int]:
    """-> (report_text, exit_code). exit_code is 1 on a CRITICAL signal, a stalled
    scheduler, or slot starvation (runs killed having executed no work at all)."""
    jobs = _load_jobs(data_dir)
    enabled = [j for j in jobs if j.get("enabled", True)]
    n_ext = sum(1 for j in enabled if j.get("extension"))
    ticking = os.path.isfile(os.path.join(data_dir, "cron-plus", ".tick.lock"))
    # .tick.lock presence is NOT liveness: tick() refreshes it BEFORE load_jobs(), so a scheduler
    # that ticks but can't load the store keeps a fresh lock while firing NO lanes. cron-plus drops
    # .scheduler-stalled for exactly that (#197); its only other reader is a cron LANE the stalled
    # scheduler never runs, so surface it HERE too (invariant-audit HIGH #2).
    stalled = ""
    sent = os.path.join(data_dir, "cron-plus", ".scheduler-stalled")
    if os.path.isfile(sent):
        try:
            stalled = json.load(open(sent, encoding="utf-8")).get("error") or "unreadable job store"
        except (OSError, ValueError, AttributeError):
            stalled = "unreadable job store"

    logs = sorted(_recent_logs(data_dir, window_h), key=os.path.getmtime)
    latest: dict[str, tuple[str, float]] = {}     # job -> (outcome, mtime) of its newest run
    mcp_affected_fires: dict[str, dict[str, int]] = {}  # all degraded runs, not only each lane's latest
    signals_total: dict[str, int] = {k: 0 for k in SIGNALS}
    models: dict[str, int] = {}                   # model id -> agent calls served (captured from logs)
    endpoints: dict[str, set[str]] = {}           # model id -> base_urls seen serving it
    for f in logs:
        try:
            text = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        name = _job_of(f)
        outcome = classify_log(text)
        latest[name] = (outcome, os.path.getmtime(f))
        if outcome in {"registry_lost", "mcp_unreachable"}:
            by_mode = mcp_affected_fires.setdefault(name, {"registry_lost": 0, "mcp_unreachable": 0})
            by_mode[outcome] += 1
        for k, n in scan_signals(text).items():
            signals_total[k] += n
        for k, n in count_models(text).items():
            models[k] = models.get(k, 0) + n
        for k, urls in map_model_endpoints(text).items():
            endpoints.setdefault(k, set()).update(urls)
    # A rotated aggregate error can outlive its cron log. Count by event time
    # across the exact flat log set, not by file mtime or current-file tail.
    error_signals, error_age_unknown, error_unreadable = _recent_error_signals(
        data_dir, _now() - window_h * 3600)
    for label, number in error_signals.items():
        signals_total[label] = max(signals_total[label], number)

    counts = {"ok": 0, "silent": 0, "incomplete": 0,
              "registry_lost": 0, "mcp_unreachable": 0}
    for _, (o, _m) in latest.items():
        counts[o] = counts.get(o, 0) + 1
    incomplete = sorted(j for j, (o, _m) in latest.items() if o == "incomplete")
    registry_lost = sorted(j for j, (o, _m) in latest.items() if o == "registry_lost")
    mcp_unreachable = sorted(j for j, (o, _m) in latest.items() if o == "mcp_unreachable")

    # overdue: next_run_at in the past by > 15 min (scheduler not advancing / stuck)
    overdue = []
    for j in enabled:
        nra = j.get("next_run_at")
        if isinstance(nra, str):
            try:
                from datetime import datetime, timezone
                t = datetime.fromisoformat(nra.replace("Z", "+00:00")).timestamp()
                if t < _now() - 900:
                    overdue.append(j["name"])
            except ValueError:
                pass

    L = []
    L.append(f"OKEngine fleet health  ·  {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    L.append("=" * 60)
    L.append(f"Fleet: {len(jobs)} jobs ({len(enabled)} enabled, {n_ext} extension)  ·  "
             f"cron-plus ticking {'✓' if ticking else '✗ — scheduler not running!'}")
    if stalled:
        L.append(f"  ✗ SCHEDULER STALLED — ticking but cannot load jobs.json ({stalled}); "
                 f"NO lanes are firing until the store is repaired and $GW restarted")
    L.append(f"Last {int(window_h)}h: {len(latest)} lanes ran  ·  "
             f"{counts['ok']} ok  ·  {counts['silent']} silent(no-agent)  ·  "
             f"{counts['incomplete']} incomplete  ·  "
             f"{counts['registry_lost']} MCP registry-lost  ·  "
             f"{counts['mcp_unreachable']} MCP unreachable")
    receipts = _receipt_counts(data_dir, _now() - window_h * 3600)
    if receipts["selected"]:
        L.append("Verified model items: " + "  ·  ".join(
            f"{receipts[key]} {key}" for key in
            ("selected", "accepted", "rejected", "deferred", "undisposed")))
    if incomplete:
        L.append("  incomplete (no completion logged — timeout / crash / still-running):")
        for name in incomplete[:12]:
            L.append(f"    ⏳ {name}")
    if registry_lost:
        L.append("  MCP registry-lost (tool call failed even if completion was logged):")
        for name in registry_lost[:12]:
            L.append(f"    ✗ {name}")
    if mcp_unreachable:
        L.append("  MCP unreachable (tool call failed even if completion was logged):")
        for name in mcp_unreachable[:12]:
            L.append(f"    ✗ {name}")
    if mcp_affected_fires:
        L.append("  MCP-affected fires in window (a later clean fire does not erase an earlier blind run):")
        for name, by_mode in sorted(mcp_affected_fires.items())[:12]:
            L.append(f"    ✗ {name}: {by_mode['registry_lost']} registry-lost, "
                     f"{by_mode['mcp_unreachable']} transport-unreachable fire(s)")
    if overdue:
        L.append(f"  overdue (next run is in the past): {', '.join(sorted(set(overdue))[:8])}")

    if models:
        tot = sum(models.values())
        free = sum(v for k, v in models.items() if is_free_model(k, endpoints.get(k)))
        # Calls we could actually price: a known provider, or a name that is free on its face.
        # Without this, a 0% offload is ambiguous between "nothing is offloaded" and "no provider
        # was ever logged", which is the failure this stat had for the whole llama.cpp era.
        known = sum(v for k, v in models.items() if endpoints.get(k) or is_free_model(k))
        offload = f"{100 * free // tot}% free/self-hosted = cost offload"
        if known < tot:
            offload += f", {100 * (tot - known) // tot}% unclassified"
        L.append("")
        L.append(f"Model usage (last {int(window_h)}h · {tot} agent calls · {offload}):")
        for k, v in sorted(models.items(), key=lambda kv: -kv[1]):
            if is_free_model(k, endpoints.get(k)):
                tag = "free"
            elif endpoints.get(k):
                tag = "PAID"
            else:
                tag = "PAID?"       # no provider ever logged for this model — assumed, not known
            L.append(f"  {v:>6} ({100 * v // tot:>2}%) [{tag}]  {k}")

    agent_lanes = {j.get("name") for j in jobs if j.get("no_agent") is not True and j.get("name")}
    starve = _slot_starvation(data_dir, _now() - window_h * 3600, agent_lanes)
    L.append("")
    if not starve["measurable"]:
        L.append("Model-slot health: UNDETECTABLE — no run records under cron-plus/runs/, or "
                 "slot_starvation.py not staged beside the cron scripts "
                 "(not a pass; the check could not run)")
    else:
        # Reported SEPARATELY and never summed: they need opposite remedies, and a combined
        # number sends the operator at whichever cause happens to dominate.
        if starve["starved"]:
            pct = 100 * starve["starved"] // max(1, starve["runs"])
            L.append(f"Slot starvation: ✗ {starve['starved']} run(s) never got an inference slot "
                     f"({pct}% of {starve['runs']} runs) — they queued until their wait expired. "
                     f"CAPACITY: raise model_concurrency to what the endpoint actually serves, "
                     f"or route lanes off it.")
            for lane, n in sorted(starve["lanes"].items(), key=lambda kv: -kv[1])[:8]:
                L.append(f"      {n:>4}  {lane}")
        else:
            L.append(f"Slot starvation: ✓ none — {starve['runs']} run(s) in window, no run was "
                     f"denied a slot")
        if starve["stalled"]:
            L.append(f"Stalled runs: ✗ {starve['stalled']} run(s) HELD a slot and produced no "
                     f"tool-call turn before their deadline — the model was reachable and "
                     f"returned nothing usable in time. LATENCY/LANE, not capacity: raising "
                     f"model_concurrency will not change this.")
            for lane, n in sorted(starve["stalled_lanes"].items(), key=lambda kv: -kv[1])[:8]:
                L.append(f"      {n:>4}  {lane}")
        elif starve["killed"]:
            L.append(f"Stalled runs: ✓ none — {starve['killed']} hard-timeout kill(s) in window, "
                     f"all with work executed (genuine overruns)")

    L.append("")
    L.append(f"Health signals (last {int(window_h)}h):")
    if error_age_unknown:
        L.append("  ⚠ aggregate error signals include events of age unknown")
    if error_unreadable:
        L.append("  ✗ one or more matched aggregate error logs could not be read")
    any_sig = False
    for label in SIGNALS:
        n = signals_total[label]
        if n:
            any_sig = True
            mark = "✗" if label in CRITICAL else "⚠"
            L.append(f"  {mark} {n:>4}  {label}")
    if not any_sig:
        L.append("  ✓ none — no denials, payment errors, overlaps, or tool blocks")

    crit = sum(signals_total[k] for k in CRITICAL)
    L.append("=" * 60)
    verdict = "ATTENTION" if (crit or mcp_affected_fires or incomplete or registry_lost or mcp_unreachable
                              or error_unreadable or not ticking or stalled
                              or starve["starved"] or starve["stalled"]) else "healthy"
    L.append(f"{verdict}: {crit} critical signal(s), {len(incomplete)} incomplete, "
             f"{len(registry_lost)} MCP registry-lost, "
             f"{len(mcp_unreachable)} MCP unreachable, "
             f"{len(overdue)} overdue, {starve['starved']} slot-starved, "
             f"{starve['stalled']} stalled"
             f"{', SCHEDULER STALLED' if stalled else ''}.")
    return "\n".join(L), (1 if (crit or mcp_affected_fires or registry_lost or mcp_unreachable
                                    or error_unreadable or stalled or starve["starved"]
                                    or starve["stalled"]) else 0)


def main(argv: list[str]) -> int:
    data_dir = argv[1] if len(argv) > 1 else os.environ.get("OKENGINE_DATA_DIR", "/opt/data")
    window = float(argv[2]) if len(argv) > 2 else 24.0
    report, code = build_report(data_dir, window)
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
