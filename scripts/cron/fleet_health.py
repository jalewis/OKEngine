#!/usr/bin/env python3
"""fleet_health.py — make the cron fleet notice its own failures (okengine#161).

An LLM-maintained KB runs dozens of unattended lanes; this session found ~5 silent failures by
hand (a stuck wake-gate, a brief degrading to a FREE model, a stale staged script, …). This is a
no_agent monitor: it reads the deployed cron-plus fleet (jobs.json) + the run logs and flags, per
enabled lane:

  STALE      — last run older than the schedule cadence × warning grace
  CRITICAL-STALE — last run older than the schedule cadence × critical grace
  ERRORED    — the most recent run log ends in an error / traceback
  OFF-MODEL  — the lane ran on a FREE/fallback model though configured for a real one (the
               brief→nemotron degradation; a synthesis lane silently producing junk)
  NEVER-RUN  — enabled lane with no run log yet

Writes wiki/dashboards/fleet-health.md (🟢/🟡/🔴 + tables) + a loud stdout summary so a red shows
up in the run output. Also writes `.fleet-lanes.json`, the machine-readable lane-identity handoff
health_export uses for transition alerts. Domain-agnostic; reads runtime only (no wiki content).

Env: WIKI_PATH (/opt/vault) · CRON_JOBS (/opt/data/cron-plus/jobs.json) ·
     CRON_LOGS (/opt/data/logs/cron-plus) · FLEET_STALE_GRACE (3.0) ·
     FLEET_CRITICAL_STALE_GRACE (9.0)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slot_starvation  # noqa: E402
import knowledge_quality  # noqa: E402

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
JOBS = Path(os.environ.get("CRON_JOBS", "/opt/data/cron-plus/jobs.json"))


def _data_dir() -> Path:
    """The directory holding cron-plus/ — what slot_starvation.scan() expects.

    Derived from JOBS when it sits at the canonical <data>/cron-plus/jobs.json, so a deployment
    that relocates CRON_JOBS carries the run-record path with it. A NON-canonical layout falls
    back to HERMES_HOME rather than guessing `JOBS.parent.parent`, which would silently address
    some unrelated directory and report UNDETECTABLE forever while looking configured.
    """
    if JOBS.parent.name == "cron-plus":
        return JOBS.parent.parent
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


STARVE_WINDOW_H = float(os.environ.get("FLEET_STARVE_WINDOW_H", "24"))
LOGS = Path(os.environ.get("CRON_LOGS", "/opt/data/logs/cron-plus"))
GRACE = float(os.environ.get("FLEET_STALE_GRACE", "3.0"))
CRITICAL_GRACE = float(os.environ.get("FLEET_CRITICAL_STALE_GRACE", "9.0"))
# Pre-marker cron-plus and deterministic no_agent logs have no explicit completion line.
# Once their file has been quiet this long, it cannot be a normal queued/generating request
# (30s admission + 300s generation); retain a generous margin for shutdown/delivery.
LEGACY_TERMINAL_AGE_S = float(os.environ.get("FLEET_LEGACY_TERMINAL_AGE", "3600"))
# A REAL run failure: an ERROR/CRITICAL LOG LEVEL (uppercase — not lowercase "error" inside a
# benign WARNING like a blocked-tool response) or a traceback or a non-zero exit. Case-sensitive on
# the level tokens is deliberate (the false-positive the monitor's own first run surfaced).
_ERR = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)|exit code [1-9]")
_FREE = re.compile(r"model=[^\s]*:free|provider=openrouter", re.I)
_RUN_STAMP = re.compile(r"-(\d{8})-(\d{6})\.log$")
_SUCCESS = re.compile(
    r"\bcron-plus runner completed\b|"
    r"\bagent returned \[SILENT\] [—-] skipping delivery\b"
)
_TERMINAL_ERROR = re.compile(
    r"\b(?:ERROR|CRITICAL) cron-plus\.runner: "
    r"(?:agent run failed|invalid completion receipt|script (?:exited|failed)|"
    r"generation (?:failed|timed out)|delivery failed)",
    re.I,
)
_HARD_TIMEOUT = re.compile(
    r"(?:TimeoutError:\s*)?cron-plus run exceeded hard timeout of\s+(\d+(?:\.\d+)?)s",
    re.I,
)
_DEPLOYMENT_VERDICT = re.compile(r"\bdeployment-validate:\s+(PASS|FAIL)\b")
# A PASSING deployment-validate is a silent no_agent run: cron-plus persists neither its stdout nor
# an output body, so the log above carries a verdict only when the run FAILED (the runner embeds
# stdout in the error). The report the run writes is where a PASS is recorded — read it, but only
# when this run wrote it, so a stale report cannot vouch for a run that never produced one (#757).
DEPLOYMENT_REPORT = WIKI / "operational" / "deployment-validation.md"
_DEPLOYMENT_REPORT_VERDICT = re.compile(r"^\*\*(PASS|FAIL)\*\* — \d+ fail\b", re.MULTILINE)
# Every classification bucket, in one place: the sidecar publishes each as a lane list and
# observability_validate cross-checks all of them, so the two must never enumerate separately.
LANE_BUCKETS = ("stale", "critical-stale", "errored", "timed-out", "saturated", "undetectable",
                "invalid-schedule", "off-model", "never-run", "ok")


def _cron_error(expr: str) -> str | None:
    """Distinguish a dead schedule from a valid lane awaiting its first run."""
    try:
        from croniter import croniter
    except ImportError:
        return "croniter parser unavailable in the gateway"
    try:
        if not isinstance(expr, str) or not croniter.is_valid(expr):
            return f"croniter rejects {expr!r}"
        croniter(expr, datetime(2026, 1, 1, tzinfo=timezone.utc)).get_next(datetime)
    except (TypeError, ValueError, KeyError) as exc:
        return f"croniter cannot compute a next fire: {exc}"
    return None


def _interval_s(expr: str) -> float | None:
    """Seconds between fires for a cron expr (via croniter); None if unknown."""
    try:
        from croniter import croniter
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        it = croniter(expr, base)
        a = it.get_next(datetime)
        b = it.get_next(datetime)
        return (b - a).total_seconds()
    except Exception:
        return None


def _run_timestamp(path: Path) -> float:
    """UTC start timestamp encoded by cron-plus; mtime is only a legacy fallback."""
    match = _RUN_STAMP.search(path.name)
    if match:
        try:
            return datetime.strptime(
                "".join(match.groups()), "%Y%m%d%H%M%S"
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def _deployment_verdict(tail: str, run_ts: float) -> str | None:
    """PASS/FAIL for a completed deployment-validate run; None when the run left no verdict."""
    logged = _DEPLOYMENT_VERDICT.search(tail)
    if logged:
        return logged.group(1)
    try:
        if DEPLOYMENT_REPORT.stat().st_mtime < run_ts:
            return None
        report = DEPLOYMENT_REPORT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    recorded = _DEPLOYMENT_REPORT_VERDICT.search(report)
    return recorded.group(1) if recorded else None


def _terminal_outcome(text: str) -> str | None:
    """Return the final explicit runner outcome, ignoring still-running logs."""
    success = [m.start() for m in _SUCCESS.finditer(text)]
    failure = [m.start() for m in _TERMINAL_ERROR.finditer(text)]
    if not success and not failure:
        return None
    return "success" if success and success[-1] > (failure[-1] if failure else -1) else "error"


def _duration(seconds: float) -> str:
    """Human duration that does not render sub-hour cadences as misleading ``0h``."""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _latest_log(name: str):
    """Newest terminal (path, UTC run timestamp, outcome), never an active run."""
    prefix = name.replace(":", "_")
    candidates = []
    if LOGS.is_dir():
        for p in LOGS.glob(f"{prefix}-*.log"):  # glob-ok: cron-plus log dir (flat), not a sharded wiki namespace
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            outcome = _terminal_outcome(text)
            quiet_age = datetime.now(timezone.utc).timestamp() - p.stat().st_mtime
            if outcome is None and quiet_age >= LEGACY_TERMINAL_AGE_S:
                # Compatibility for historical and deterministic logs. Preserve the monitor's
                # prior error semantics for these closed files, while a recently written
                # markerless file remains an active run and is excluded.
                outcome = "error" if _ERR.search(text[-4000:]) else "success"
            if outcome:
                candidates.append((_run_timestamp(p), p.name, p, outcome))
    if not candidates:
        return None, None, None
    stamp, _filename, path, outcome = max(candidates)
    return path, stamp, outcome


# ── qmd search health ────────────────────────────────────────────────────────────────────────────
# okengine#410 fixed search saturation with a 2-slot admission gate and recommended "expose
# timeout/saturation distinctly in fleet health". That half never shipped, so the conditions that
# would justify revisiting the search layer — rising p95, routine saturation — were unobservable
# (okengine#568). A cap you cannot see hitting is indistinguishable from a cap you never reach.
#
# The read-MCP publishes a snapshot to its own writable mount; the gateway sees the same directory.
QMD_STATS_PATH = Path(os.environ.get("OKENGINE_QMD_STATS", "/opt/data/qmd/search-telemetry.json"))
# A p95 above this is the trigger okengine#568 names for revisiting the search layer. It is a
# WARNING, not an error: slow search degrades lanes, it does not break them.
QMD_P95_WARN_MS = int(os.environ.get("FLEET_HEALTH_QMD_P95_WARN_MS", "5000"))
# Saturation is different. It means a lane was REFUSED, so work did not happen.
QMD_SATURATION_WARN = int(os.environ.get("FLEET_HEALTH_QMD_SATURATION_WARN", "1"))
QMD_STALE_HOURS = float(os.environ.get("FLEET_HEALTH_QMD_STALE_HOURS", "24"))
MEMORY_EVENTS = Path(os.environ.get("OKENGINE_MEMORY_EVENTS", "/sys/fs/cgroup/memory.events"))
QUALITY_ADJUDICATION = Path(os.environ.get(
    "OKENGINE_QUALITY_ADJUDICATION", "/opt/data/quality/adjudication.json"))


def memory_pressure(path: Path = MEMORY_EVENTS) -> tuple[str, str]:
    try:
        events = {key: int(value) for key, value in
                  (line.split() for line in path.read_text(encoding="utf-8").splitlines())}
    except (OSError, ValueError):
        return "unknown", f"memory counters unavailable at {path}"
    kills = events.get("oom_kill", 0)
    maximum = events.get("max", 0)
    if kills:
        return "error", f"kernel recorded {kills} OOM kill(s) and {maximum} memory-cap hit(s)"
    if maximum:
        return "warn", f"kernel recorded {maximum} memory-cap hit(s), no OOM kills"
    return "ok", "no memory-cap hits or OOM kills recorded"


def read_qmd_telemetry():
    """The published search telemetry, or None when it is absent or not JSON.

    Read ONCE per fleet-health run. The read-MCP republishes on every qmd call, so reading it
    separately for the dashboard line and the sidecar snapshot let the two describe different
    versions — and observability_validate reported the mismatch as a disagreement (#757).
    """
    try:
        return json.loads(QMD_STATS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # json.JSONDecodeError is a ValueError
        return None


_READ_TELEMETRY = object()


def qmd_search_health(now: datetime, snap=_READ_TELEMETRY) -> tuple[str, str]:
    """(status, detail) for the search layer: 'ok' | 'warn' | 'unknown'.

    'unknown' is a first-class outcome and is NEVER reported as healthy. An absent snapshot means
    the read-MCP has not published one — an image predating this telemetry, a stopped container, or
    an unwritable mount. Reporting that as green would recreate exactly the blind spot this exists
    to remove.
    """
    if snap is _READ_TELEMETRY:
        snap = read_qmd_telemetry()
    if snap is None:
        return "unknown",(f"no search telemetry at {QMD_STATS_PATH} — the read-MCP has not "
                           f"published one (image predating it, container down, or mount "
                           f"unwritable). Search health is UNMEASURED, not healthy.")
    if not isinstance(snap, dict):
        return "unknown", "search telemetry is not an object"

    updated = str(snap.get("updated") or "")
    age_note = ""
    try:
        stamp = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_h = (now - stamp).total_seconds() / 3600.0
        if age_h > QMD_STALE_HOURS:
            # Stale is not automatically bad: a quiet vault issues no searches. Say so rather than
            # inferring a fault from silence.
            age_note = (f" — telemetry is {age_h:.0f}h old, so this reflects the last search "
                        f"activity, not the present moment")
    except ValueError:
        age_note = " — telemetry carries no readable timestamp"

    # SEARCH and MAINTENANCE are judged separately. They share the two slots, so saturation is one
    # number, but their latencies are different populations: pooling them made a 7.6s index refresh
    # render as "search p95 over 5000ms" on a vault whose searches run in ~0.5s. A threshold that
    # fires on the wrong population is how a warning becomes something people learn to ignore.
    search = snap.get("search") if isinstance(snap.get("search"), dict) else {}
    maint = snap.get("maintenance") if isinstance(snap.get("maintenance"), dict) else {}
    if not search and not maint:
        return "unknown", ("search telemetry predates the search/maintenance split — the read-MCP "
                           "image is older than this check. UNMEASURED, not healthy.")

    saturated = int(snap.get("saturated") or 0)
    s_timeouts = int(search.get("timeouts") or 0)
    m_errors = int(maint.get("errors") or 0)
    m_timeouts = int(maint.get("timeouts") or 0)
    s_calls = int(search.get("calls") or 0)
    s_p95 = search.get("p95_ms")
    m_p95 = maint.get("p95_ms")
    detail = (f"search {s_calls} call(s) p95 {s_p95 if s_p95 is not None else '—'}ms · "
              f"maintenance {int(maint.get('calls') or 0)} p95 "
              f"{m_p95 if m_p95 is not None else '—'}ms · saturated {saturated}, "
              f"search timeouts {s_timeouts}, maintenance errors {m_errors}, "
              f"maintenance timeouts {m_timeouts}, "
              f"concurrency {snap.get('concurrency_limit')}{age_note}")

    if saturated >= QMD_SATURATION_WARN or s_timeouts:
        return "warn", ("search REFUSED work — " + detail
                        + ". A saturated call is a lane that did not get its answer.")
    if m_errors or m_timeouts:
        return "warn", ("index maintenance FAILED — " + detail
                        + ". Search may be serving a stale last-known-good index.")
    if isinstance(s_p95, (int, float)) and s_p95 >= QMD_P95_WARN_MS:
        return "warn", f"SEARCH p95 over {QMD_P95_WARN_MS}ms — {detail}"
    if s_calls == 0:
        # Maintenance-only telemetry says the index is being kept fresh and nothing has searched.
        # That is not a fault and must not be dressed as one.
        return "ok", "no searches yet this cycle (maintenance only) — " + detail
    return "ok", detail


# ── escalation ────────────────────────────────────────────────────────────────────────────────
# This monitor renders red and, until now, ALWAYS emitted wakeAgent: False. When the actor
# assessment lane broke on a stale review key it stayed 🔴 for six hours — about twenty-four runs
# of this job at its 15-minute cadence — and every one of them drew the row and told nobody. A
# dashboard is not a watchdog. Nobody reads a dashboard at 04:00; that is the entire point of
# having a wake channel.
#
# Escalation is deliberately hysteretic. Waking on the FIRST red would page on every transient
# blip and train the reader to ignore it, which is how a channel goes quiet without anyone
# deciding to silence it. So a lane must stay red across ESCALATE_AFTER consecutive runs before it
# wakes anyone, and a lane that stays red re-wakes only every REWAKE_EVERY runs after that — loud
# enough not to be forgotten, quiet enough to stay worth reading.
ESCALATE_AFTER = int(os.environ.get("FLEET_HEALTH_ESCALATE_AFTER", "3"))
REWAKE_EVERY = int(os.environ.get("FLEET_HEALTH_REWAKE_EVERY", "24"))
# 🔴 conditions plus critical-stale: a lane whose last run FAILED, that the runner KILLED, that ran
# on the wrong model, or that has gone quiet well past its schedule. `stale`, `saturated` and
# `never-run` are deliberately excluded — the first two are load, and never-run is usually just a
# weekly lane whose day has not come round yet.
ESCALATING_STATUSES = ("errored", "timed-out", "off-model", "critical-stale",
                       "invalid-schedule")


def _escalation_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}                      # no history is "first sighting", never "nothing is wrong"


def escalate(state: dict, lane_sets: dict, nowiso: str) -> tuple[dict, list[str]]:
    """(next state, lanes to wake for). Counts CONSECUTIVE red runs per lane."""
    red_now = {name: key for key in ESCALATING_STATUSES for name in lane_sets.get(key, [])}
    prior = state.get("lanes") if isinstance(state.get("lanes"), dict) else {}
    lanes, wake = {}, []
    for name, status in sorted(red_now.items()):
        was = prior.get(name) if isinstance(prior.get(name), dict) else {}
        # A lane that changes ONE red status for another (errored -> timed-out) is still broken and
        # must not have its counter reset by the change.
        run = int(was.get("consecutive") or 0) + 1
        lanes[name] = {"status": status, "consecutive": run,
                       "red_since": was.get("red_since") or nowiso}
        if run == ESCALATE_AFTER or (run > ESCALATE_AFTER
                                     and (run - ESCALATE_AFTER) % REWAKE_EVERY == 0):
            wake.append(name)
    return {"updated": nowiso, "lanes": lanes}, wake


def main() -> int:
    if not JOBS.is_file():
        print(f"fleet-health: no jobs.json at {JOBS}", file=sys.stderr)
        return 1
    jobs = json.loads(JOBS.read_text(encoding="utf-8")).get("jobs", [])
    now = datetime.now(timezone.utc)
    nowts = now.timestamp()
    rows = []          # (status, name, detail)
    counts = {key: 0 for key in LANE_BUCKETS}
    lane_sets = {key: [] for key in counts}
    for j in jobs:
        if not j.get("enabled", True):
            continue
        name = j["name"]
        expr = (j.get("schedule") or {}).get("expr", "")
        model = j.get("model")
        if schedule_error := _cron_error(expr):
            counts["invalid-schedule"] += 1
            lane_sets["invalid-schedule"].append(name)
            rows.append(("🔴 INVALID-SCHEDULE", name, schedule_error))
            continue
        log, run_ts, outcome = _latest_log(name)
        if log is None:
            counts["never-run"] += 1
            lane_sets["never-run"].append(name)
            rows.append(("🟡 never-run", name, f"enabled, no completed run ({expr})"))
            continue
        age = max(0, nowts - run_ts)
        try:
            tail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            tail = ""
        status, detail = "🟢 ok", f"ran {int(age // 3600)}h ago"
        interval = _interval_s(expr)
        hard_timeout = _HARD_TIMEOUT.search(tail)
        # A hard-timeout is normally logged at ERROR and therefore also yields an error terminal
        # marker. Classify the specific, actionable condition before the generic failure bucket.
        if hard_timeout:
            status = "🔴 TIMEOUT"
            detail = f"cron-plus exceeded its {hard_timeout.group(1)}s hard timeout"
            counts["timed-out"] += 1
            lane_sets["timed-out"].append(name)
        elif outcome == "error":
            status = "🔴 ERRORED"
            hit = next(
                (ln.strip()[:80] for ln in reversed(tail.splitlines())
                 if _TERMINAL_ERROR.search(ln)),
                "terminal runner error",
            )
            detail = f"last run: {hit}"
            counts["errored"] += 1
            lane_sets["errored"].append(name)
        elif name == "deployment-validate" and (verdict := _deployment_verdict(tail, run_ts)) is None:
            status = "🟠 UNDETECTABLE"
            detail = "completed without a deployment-validate PASS/FAIL verdict; not a clean run"
            counts["undetectable"] += 1
            lane_sets["undetectable"].append(name)
        elif name == "deployment-validate" and verdict == "FAIL":
            status = "🔴 ERRORED"
            detail = "last run: deployment-validate reported FAIL"
            counts["errored"] += 1
            lane_sets["errored"].append(name)
        elif re.search(r"SEARCH_SATURATED|search saturated|qmd capacity|HTTP 503", tail, re.I):
            status = "🟠 SATURATED"
            detail = "search capacity exhausted; client should retry with backoff"
            counts["saturated"] += 1
            lane_sets["saturated"].append(name)
        elif re.search(r"SEARCH_TIMEOUT|search timed out|qmd timed out|MCP.{0,40}timed out", tail, re.I):
            status = "🔴 TIMEOUT"
            detail = "search exceeded its bounded execution timeout"
            counts["timed-out"] += 1
            lane_sets["timed-out"].append(name)
        elif model and ":free" not in str(model) and _FREE.search(tail):
            status = "🔴 OFF-MODEL"
            detail = f"configured {model} but ran on a free/fallback model"
            counts["off-model"] += 1
            lane_sets["off-model"].append(name)
        elif interval and age > interval * CRITICAL_GRACE:
            status = "🟠 CRITICAL-STALE"
            detail = f"last run {_duration(age)} ago (cadence ~{_duration(interval)}; >{CRITICAL_GRACE:g}×)"
            counts["critical-stale"] += 1
            lane_sets["critical-stale"].append(name)
        elif interval and age > interval * GRACE:
            status = "🟡 STALE"
            detail = f"last run {_duration(age)} ago (cadence ~{_duration(interval)}; >{GRACE:g}×)"
            counts["stale"] += 1
            lane_sets["stale"].append(name)
        else:
            counts["ok"] += 1
            lane_sets["ok"].append(name)
        rows.append((status, name, detail))

    # Runs killed at the hard timeout having executed ZERO turns never got a model response --
    # they queued for an inference slot until killed. That is capacity, not lane work, and it
    # accumulates invisibly because a killed run leaves no artifact (okengine#614: this used to be
    # measured only by the operator-invoked fleet_status.py, so nobody saw it for six weeks).
    data_dir = _data_dir()
    starve = slot_starvation.scan(
        str(data_dir), nowts - STARVE_WINDOW_H * 3600, slot_starvation.agent_lanes_of(jobs))
    pressure = slot_starvation.timeout_pressure(
        str(data_dir), nowts - STARVE_WINDOW_H * 3600, jobs)
    for lane, metric in sorted(pressure.items()):
        rows.append(("🟠 TIMEOUT-PRESSURE", lane,
                     f"successful-run p99 {metric['p99']}s is {metric['ratio']:.0%} of "
                     f"the {metric['timeout']}s hard timeout ({metric['runs']} runs)"))
    # Starvation and stalling are REPORTED SEPARATELY and never summed. A run denied a slot and a
    # run that held one and returned nothing look identical in the old "zero turns" reading, but
    # need opposite fixes -- capacity versus latency. Combining them points the operator at
    # whichever cause happens to dominate, which on one live deployment was 0 starvation against
    # 15 stalls under a heading that read "raise model_concurrency".
    def _worst(d):
        return ", ".join(f"{n} ×{c}" for n, c in sorted(d.items(), key=lambda kv: -kv[1])[:5])

    if not starve["measurable"]:
        starve_icon, starve_detail = "🟡", (
            f"UNDETECTABLE — no run records under {data_dir}/cron-plus/runs/ "
            f"(not a pass; the check could not run)")
    elif starve["starved"]:
        starve_icon, starve_detail = "🔴", (
            f"{starve['starved']} run(s) never got an inference slot in "
            f"{int(STARVE_WINDOW_H)}h ({starve['runs']} scanned) — CAPACITY: raise "
            f"model_concurrency to what the endpoint serves, or route lanes off it. "
            f"Worst: {_worst(starve['lanes'])}")
        rows.append(("🔴 slot-starved", "(endpoint capacity)", starve_detail))
    else:
        starve_icon, starve_detail = "🟢", (
            f"none — {starve['runs']} run(s) in {int(STARVE_WINDOW_H)}h, no run denied a slot")

    if starve["stalled"]:
        stall_icon, stall_detail = "🔴", (
            f"{starve['stalled']} run(s) HELD a slot and produced no tool-call turn before the "
            f"deadline in {int(STARVE_WINDOW_H)}h — the model was reachable and returned nothing "
            f"usable in time. LATENCY/LANE, not capacity: raising model_concurrency will not "
            f"change this. Worst: {_worst(starve['stalled_lanes'])}")
        rows.append(("🔴 stalled", "(model returned nothing)", stall_detail))
    elif starve["killed"]:
        stall_icon, stall_detail = "🟢", (
            f"none — {starve['killed']} hard-timeout kill(s), all with work executed")
    else:
        stall_icon, stall_detail = "🟢", "none — no hard-timeout kills"

    red = (counts["errored"] + counts["timed-out"] + counts["off-model"]
           + counts["invalid-schedule"]
           + (1 if starve["starved"] else 0) + (1 if starve["stalled"] else 0))
    memory_state, memory_detail = memory_pressure()
    memory_icon = {"ok": "🟢", "warn": "🟠", "error": "🔴", "unknown": "⚪"}[memory_state]
    if memory_state in {"warn", "error"}:
        rows.append((f"{memory_icon} MEMORY", "(gateway cgroup)", memory_detail))
    red = red + (1 if memory_state == "error" else 0)
    orange = (counts["critical-stale"] + counts["saturated"] + counts["undetectable"]
              + len(pressure) + (1 if memory_state == "warn" else 0))
    yellow = counts["stale"] + counts["never-run"]
    overall = (
        "🔴 attention needed" if red else
        "🟠 degraded" if orange else
        "🟡 warning" if yellow else
        "🟢 healthy"
    )
    nowiso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    refresh_interval = _interval_s(
        next(((j.get("schedule") or {}).get("expr", "") for j in jobs
              if j.get("name") == "fleet-health"), "")
    ) or 900
    stale_after = int(refresh_interval * GRACE)
    L = ["---", "type: dashboard", 'title: "Fleet health"', f"updated: {nowiso}",
         f"freshness_checked: {nowiso}", f"stale_after_seconds: {stale_after}", "---", "",
         f"# Fleet health — {nowiso}", "", f"**{overall}**", "",
         f"_Generated now (age 0m); this dashboard is considered stale when older than {stale_after // 60}m. "
         "The page `updated` field is the authoritative UTC freshness timestamp._", "",
         f"- 🟢 ok: {counts['ok']}  ·  🟡 stale: {counts['stale']} "
         f"· 🟠 critical-stale: {counts['critical-stale']} · 🔴 errored: {counts['errored']}  "
         f"· 🔴 timeout: {counts['timed-out']} · 🟠 saturated: {counts['saturated']} "
         f"· 🟠 undetectable: {counts['undetectable']} "
         f"· 🔴 invalid-schedule: {counts['invalid-schedule']} "
         f"· 🔴 off-model: {counts['off-model']}  ·  🟡 never-run: {counts['never-run']}", ""]
    bad_rows = [r for r in rows if r[0].startswith(("🔴", "🟠", "🟡"))]
    if bad_rows:
        L += ["## Needs attention", "", "| Status | Lane | Detail |", "|---|---|---|"]
        L += [f"| {s} | {n} | {d} |" for s, n, d in bad_rows] + [""]
    qmd_telemetry = read_qmd_telemetry()
    qmd_state, qmd_detail = qmd_search_health(now, qmd_telemetry)
    qmd_snapshot = qmd_telemetry if isinstance(qmd_telemetry, dict) else {}
    qmd_icon = {"ok": "🟢", "warn": "🟡", "unknown": "⚪"}[qmd_state]
    L += ["## Search layer (qmd)", "",
          f"{qmd_icon} **{qmd_state}** — {qmd_detail}", "",
          "_Saturation means a lane was refused, not merely delayed. `unknown` means unmeasured, "
          "which is not the same as healthy (okengine#410, #568)._", ""]
    L += ["## Gateway memory", "", f"{memory_icon} **{memory_state}** — {memory_detail}", ""]
    quality = knowledge_quality.rollup(
        data_dir / "cron-plus" / "receipts", QUALITY_ADJUDICATION)
    L += knowledge_quality.markdown(quality)
    L += ["## All enabled lanes", "", "| Status | Lane | Detail |", "|---|---|---|"]
    priority = {"🔴": 0, "🟠": 1, "🟡": 2, "🟢": 3}
    L += [f"| {s} | {n} | {d} |" for s, n, d in
          sorted(rows, key=lambda r: (priority.get(r[0][:1], 4), r[1]))]
    L.append("")
    out = WIKI / "dashboards" / "fleet-health.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text("\n".join(L), encoding="utf-8")
        # Atomic producer/consumer handoff: health_export must never parse a half-written JSON file
        # and silently fall back to count transitions during a real composition change.
        sidecar = out.parent / ".fleet-lanes.json"
        tmp = sidecar.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "updated": nowiso,
            "knowledge_quality": quality,
            "search_telemetry": qmd_snapshot,
            **{key: sorted(names) for key, names in lane_sets.items()},
        }, indent=2) + "\n", encoding="utf-8")
        tmp.replace(sidecar)
    except OSError as e:
        # The dashboard file is foreign-owned (root, from a bare `docker exec` write), so the lane
        # uid can't overwrite it — the exact uid-desync condition check_ownership/fix-vault-ownership
        # exist for. A raw PermissionError here would crash the monitor ON ITS OWN OUTPUT with no
        # peer, and a downstream pipeline that scrapes this dashboard (health_export #9) would read
        # the frozen last-green copy forever. Fail loud with the remedy instead (okengine#178).
        print(f"fleet-health: ERROR cannot write dashboard/sidecar under {out.parent}: {e} — likely "
              "a foreign-owned (root) file. Repair: scripts/fix-vault-ownership.sh <deployment-dir>",
              file=sys.stderr)
        # ALWAYS wake. The monitor failing on its own output is the one fault no other lane can
        # report, and every consumer downstream will happily keep reading the frozen last-green
        # copy. There is no such thing as a transient version of this, so there is no hysteresis.
        print(json.dumps({"wakeAgent": True}))
        return 1

    summary = (f"fleet-health: {overall} — ok {counts['ok']}, stale {counts['stale']}, "
               f"critical-stale {counts['critical-stale']}, "
               f"errored {counts['errored']}, timeout {counts['timed-out']}, "
               f"saturated {counts['saturated']}, invalid-schedule {counts['invalid-schedule']}, "
               f"off-model {counts['off-model']}, "
               f"never-run {counts['never-run']} -> wiki/dashboards/fleet-health.md")
    print(summary)
    print(f"  {qmd_icon} search (qmd): {qmd_state} — {qmd_detail}")
    print(f"  {starve_icon} slot starvation: {starve_detail}")
    print(f"  {stall_icon} stalled runs: {stall_detail}")
    for s, n, d in bad_rows:                         # loud: reds in the run output
        print(f"  {s} {n}: {d}")

    esc_path = out.parent / ".fleet-escalation.json"
    next_state, wake_for = escalate(_escalation_state(esc_path), lane_sets, nowiso)
    try:
        tmp = esc_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(next_state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(esc_path)
    except OSError as e:
        # Losing the history means losing the ability to say "still". Escalate on what we can see
        # right now rather than degrading to silence.
        print(f"fleet-health: WARNING cannot persist escalation state ({e}) — consecutive-run "
              f"tracking is unavailable this run", file=sys.stderr)

    if wake_for:
        detail = ", ".join(f"{n} ({next_state['lanes'][n]['status']}, "
                           f"{next_state['lanes'][n]['consecutive']} consecutive runs since "
                           f"{next_state['lanes'][n]['red_since']})" for n in wake_for)
        print(f"fleet-health: ESCALATING — {len(wake_for)} lane(s) red across at least "
              f"{ESCALATE_AFTER} consecutive runs: {detail}. Read the lane's own run log or "
              f"receipt; this monitor reports the state, it does not diagnose it.")
    print(json.dumps({"wakeAgent": bool(wake_for)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
