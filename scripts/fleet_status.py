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
import json
import os
import re
import sys
import time

# --- run-outcome classification (one log's text -> a verdict) ----------------

def classify_log(text: str) -> str:
    """ok | silent | incomplete. Conservative: only a logged completion is 'ok'; a no_agent /
    wake-gate-declined run is 'silent'; anything that didn't clearly finish is 'incomplete'
    (could be a timeout, a crash, or still running) — the signals scan explains why."""
    if "completed successfully" in text:
        return "ok"
    if "[SILENT]" in text or "wakeAgent=false" in text:
        return "silent"
    return "incomplete"


# --- silent-failure signals (the stuff that hides in WARNING lines) ----------

SIGNALS: dict[str, str] = {
    "vault write denied (#140)":      r"Write denied:.*protected system/credential",
    "provider payment/credit error":  r"payment\s*/\s*credit error|insufficient[^\n]*credit",
    "job-overlap skip (runs > interval)": r"skipped\s+—\s+previous run still active",
    "execute_code blocked (cron safety)": r"BLOCKED: execute_code",
    "agent read-loop blocked":        r"called read_file on this exact region",
    "agent tool error":               r"agent\.tool_executor: Tool \w+ returned error",
}
# signals that mean something is actually broken (drive the exit code), vs expected/benign noise.
CRITICAL = {"vault write denied (#140)", "provider payment/credit error"}


def scan_signals(text: str) -> dict[str, int]:
    return {label: len(re.findall(pat, text, re.I)) for label, pat in SIGNALS.items()}


# --- model usage (which model served, and the free/paid split) ---------------

_MODEL = re.compile(r"model=([a-z0-9/._:-]+)")


def is_free_model(model_id: str) -> bool:
    """A free OpenRouter tier (`:free`) or the Free Models Router — what costs $0. Everything
    else (a paid/standard tier) is paid, for the cost-offload stat."""
    m = model_id.lower()
    return m.endswith(":free") or m == "openrouter/free"


def count_models(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in _MODEL.findall(text):
        if m == "model":            # a 'model=model' placeholder log artifact, not a real id
            continue
        out[m] = out.get(m, 0) + 1
    return out


# --- I/O + report ------------------------------------------------------------

_TS = re.compile(r"(\d{8})-(\d{6})\.log$")


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


def _job_of(logpath: str) -> str:
    # <jobname>-YYYYMMDD-HHMMSS.log  ->  <jobname>
    return re.sub(r"-\d{8}-\d{6}\.log$", "", os.path.basename(logpath))


def build_report(data_dir: str = "/opt/data", window_h: float = 24.0) -> tuple[str, int]:
    """-> (report_text, exit_code). exit_code is 1 when a CRITICAL signal fired."""
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
    signals_total: dict[str, int] = {k: 0 for k in SIGNALS}
    models: dict[str, int] = {}                   # model id -> agent calls served (captured from logs)
    for f in logs:
        try:
            text = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        latest[_job_of(f)] = (classify_log(text), os.path.getmtime(f))
        for k, n in scan_signals(text).items():
            signals_total[k] += n
        for k, n in count_models(text).items():
            models[k] = models.get(k, 0) + n
    # the aggregate errors.log catches signals from runs whose own log rotated/trimmed
    errlog = os.path.join(data_dir, "logs", "errors.log")
    if os.path.isfile(errlog):
        try:
            tail = open(errlog, encoding="utf-8", errors="ignore").read()[-200_000:]
            for k, n in scan_signals(tail).items():
                signals_total[k] = max(signals_total[k], n)
        except OSError:
            pass

    counts = {"ok": 0, "silent": 0, "incomplete": 0}
    for _, (o, _m) in latest.items():
        counts[o] = counts.get(o, 0) + 1
    incomplete = sorted(j for j, (o, _m) in latest.items() if o == "incomplete")

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
             f"{counts['incomplete']} incomplete")
    if incomplete:
        L.append("  incomplete (no completion logged — timeout / crash / still-running):")
        for name in incomplete[:12]:
            L.append(f"    ⏳ {name}")
    if overdue:
        L.append(f"  overdue (next run is in the past): {', '.join(sorted(set(overdue))[:8])}")

    if models:
        tot = sum(models.values())
        free = sum(v for k, v in models.items() if is_free_model(k))
        L.append("")
        L.append(f"Model usage (last {int(window_h)}h · {tot} agent calls · "
                 f"{100 * free // tot}% on free tiers = cost offload):")
        for k, v in sorted(models.items(), key=lambda kv: -kv[1]):
            tag = "free" if is_free_model(k) else "PAID"
            L.append(f"  {v:>6} ({100 * v // tot:>2}%) [{tag}]  {k}")

    L.append("")
    L.append(f"Health signals (last {int(window_h)}h):")
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
    verdict = "ATTENTION" if (crit or incomplete or not ticking or stalled) else "healthy"
    L.append(f"{verdict}: {crit} critical signal(s), {len(incomplete)} incomplete, "
             f"{len(overdue)} overdue{', SCHEDULER STALLED' if stalled else ''}.")
    return "\n".join(L), (1 if (crit or stalled) else 0)


def main(argv: list[str]) -> int:
    data_dir = argv[1] if len(argv) > 1 else os.environ.get("OKENGINE_DATA_DIR", "/opt/data")
    window = float(argv[2]) if len(argv) > 2 else 24.0
    report, code = build_report(data_dir, window)
    print(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
