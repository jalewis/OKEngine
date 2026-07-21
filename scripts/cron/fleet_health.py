#!/usr/bin/env python3
"""fleet_health.py — make the cron fleet notice its own failures (okengine#161).

An LLM-maintained KB runs dozens of unattended lanes; this session found ~5 silent failures by
hand (a stuck wake-gate, a brief degrading to a FREE model, a stale staged script, …). This is a
no_agent monitor: it reads the deployed cron-plus fleet (jobs.json) + the run logs and flags, per
enabled lane:

  STALE      — last run older than the schedule cadence × grace (a daily lane idle for days)
  ERRORED    — the most recent run log ends in an error / traceback
  OFF-MODEL  — the lane ran on a FREE/fallback model though configured for a real one (the
               brief→nemotron degradation; a synthesis lane silently producing junk)
  NEVER-RUN  — enabled lane with no run log yet

Writes wiki/dashboards/fleet-health.md (🟢/🟡/🔴 + tables) + a loud stdout summary so a red shows
up in the run output. Also writes `.fleet-lanes.json`, the machine-readable lane-identity handoff
health_export uses for transition alerts. Domain-agnostic; reads runtime only (no wiki content).

Env: WIKI_PATH (/opt/vault) · CRON_JOBS (/opt/data/cron-plus/jobs.json) ·
     CRON_LOGS (/opt/data/logs/cron-plus) · FLEET_STALE_GRACE (3.0)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
JOBS = Path(os.environ.get("CRON_JOBS", "/opt/data/cron-plus/jobs.json"))
LOGS = Path(os.environ.get("CRON_LOGS", "/opt/data/logs/cron-plus"))
GRACE = float(os.environ.get("FLEET_STALE_GRACE", "3.0"))
# A REAL run failure: an ERROR/CRITICAL LOG LEVEL (uppercase — not lowercase "error" inside a
# benign WARNING like a blocked-tool response) or a traceback or a non-zero exit. Case-sensitive on
# the level tokens is deliberate (the false-positive the monitor's own first run surfaced).
_ERR = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)|exit code [1-9]")
_FREE = re.compile(r"model=[^\s]*:free|provider=openrouter", re.I)


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


def _latest_log(name: str):
    """(path, mtime) of the newest run log for a job, or (None, None)."""
    prefix = name.replace(":", "_")
    best, bm = None, None
    if LOGS.is_dir():
        for p in LOGS.glob(f"{prefix}-*.log"):  # glob-ok: cron-plus log dir (flat), not a sharded wiki namespace
            m = p.stat().st_mtime
            if bm is None or m > bm:
                best, bm = p, m
    return best, bm


def main() -> int:
    if not JOBS.is_file():
        print(f"fleet-health: no jobs.json at {JOBS}", file=sys.stderr)
        return 1
    jobs = json.loads(JOBS.read_text(encoding="utf-8")).get("jobs", [])
    now = datetime.now(timezone.utc)
    nowts = now.timestamp()
    rows = []          # (status, name, detail)
    counts = {"stale": 0, "errored": 0, "off-model": 0, "never-run": 0, "ok": 0}
    lane_sets = {key: [] for key in counts}
    for j in jobs:
        if not j.get("enabled", True):
            continue
        name = j["name"]
        expr = (j.get("schedule") or {}).get("expr", "")
        model = j.get("model")
        log, mt = _latest_log(name)
        if log is None:
            counts["never-run"] += 1
            lane_sets["never-run"].append(name)
            rows.append(("🟡 never-run", name, f"enabled, no run log ({expr})"))
            continue
        age = nowts - mt
        try:
            tail = log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            tail = ""
        status, detail = "🟢 ok", f"ran {int(age // 3600)}h ago"
        interval = _interval_s(expr)
        if interval and age > interval * GRACE:
            status, detail = "🔴 STALE", f"last run {int(age // 3600)}h ago (cadence ~{int(interval // 3600)}h)"
            counts["stale"] += 1
            lane_sets["stale"].append(name)
        elif _ERR.search(tail):
            status = "🔴 ERRORED"
            hit = next((ln.strip()[:80] for ln in tail.splitlines() if _ERR.search(ln)), "error")
            detail = f"last run: {hit}"
            counts["errored"] += 1
            lane_sets["errored"].append(name)
        elif model and ":free" not in str(model) and _FREE.search(tail):
            status = "🔴 OFF-MODEL"
            detail = f"configured {model} but ran on a free/fallback model"
            counts["off-model"] += 1
            lane_sets["off-model"].append(name)
        else:
            counts["ok"] += 1
            lane_sets["ok"].append(name)
        rows.append((status, name, detail))

    bad = counts["stale"] + counts["errored"] + counts["off-model"]
    overall = "🔴 attention needed" if bad else ("🟡 some lanes never run" if counts["never-run"] else "🟢 healthy")
    nowiso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Fleet health"', f"updated: {nowiso}", "---", "",
         f"# Fleet health — {nowiso}", "", f"**{overall}**", "",
         f"- 🟢 ok: {counts['ok']}  ·  🔴 stale: {counts['stale']}  ·  🔴 errored: {counts['errored']}  "
         f"·  🔴 off-model: {counts['off-model']}  ·  🟡 never-run: {counts['never-run']}", ""]
    bad_rows = [r for r in rows if r[0].startswith("🔴")]
    if bad_rows:
        L += ["## Needs attention", "", "| Status | Lane | Detail |", "|---|---|---|"]
        L += [f"| {s} | {n} | {d} |" for s, n, d in bad_rows] + [""]
    L += ["## All enabled lanes", "", "| Status | Lane | Detail |", "|---|---|---|"]
    L += [f"| {s} | {n} | {d} |" for s, n, d in sorted(rows, key=lambda r: (not r[0].startswith("🔴"), r[1]))]
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
        print(json.dumps({"wakeAgent": False}))
        return 1

    summary = (f"fleet-health: {overall} — ok {counts['ok']}, stale {counts['stale']}, "
               f"errored {counts['errored']}, off-model {counts['off-model']}, "
               f"never-run {counts['never-run']} -> wiki/dashboards/fleet-health.md")
    print(summary)
    for s, n, d in bad_rows:                         # loud: reds in the run output
        print(f"  {s} {n}: {d}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
