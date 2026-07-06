#!/usr/bin/env python3
"""usage_rollup.py — persist per-model usage from cron run logs into a SQLite ledger (okengine#144).

A deterministic no_agent rollup: parse cron run logs not yet counted, aggregate the per-turn
`model=` lines the agent already writes, and upsert (day, model, lane) -> calls into
``<data_dir>/metrics/usage.db``. Idempotent — each settled log is counted exactly once — so the
ledger is the long-term record the live fleet-status view can't keep once logs rotate.

  python usage_rollup.py                  # rollup /opt/data (the hourly cron)
  python usage_rollup.py report           # offload % + model distribution over time
  python usage_rollup.py report <dir> [N] # report from <dir>, last N days (default 14)

Captures call COUNTS + free/paid (the `model=` lines), not tokens/$ (the agent logs no token
counts). Log-derived = per-vault by construction, so it's correct even if an API key is shared.
"""
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DATA_DIR = "/opt/data"
SETTLE_SECONDS = 120                       # skip a log still being written; count it next rollup
_MODEL = re.compile(r"model=([a-z0-9/._:-]+)")
_LOGNAME = re.compile(r"^(.+?)-(\d{8})-\d{6}\.log$")


# Locally-served models (custom/ollama provider) cost $0 but carry no `:free` marker — the
# deployment declares them so the offload % is honest. Comma-separated, e.g.
# HERMES_LOCAL_MODELS=qwen3.5:27b,llama3:8b.
_LOCAL_FREE = {m.strip().lower() for m in os.environ.get("HERMES_LOCAL_MODELS", "").split(",") if m.strip()}


def is_free_model(m: str) -> bool:
    m = m.lower()
    return m.endswith(":free") or m == "openrouter/free" or m in _LOCAL_FREE


def db_path(data_dir) -> Path:
    return Path(data_dir) / "metrics" / "usage.db"


def connect(data_dir) -> sqlite3.Connection:
    p = db_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # SQLite allows ONE writer at a time (a db-level write lock); concurrent writers get
    # "database is locked". usage-rollup is a single periodic lane, but a manual run or an
    # overlapping tick can contend — wait up to 30s for the lock instead of failing the job.
    c = sqlite3.connect(str(p), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")   # readers don't block the single writer
    c.execute("CREATE TABLE IF NOT EXISTS usage(day TEXT, model TEXT, lane TEXT, "
              "is_free INT, calls INT, PRIMARY KEY(day, model, lane))")
    c.execute("CREATE TABLE IF NOT EXISTS processed(log TEXT PRIMARY KEY)")
    return c


def parse_log(path: Path):
    """(day, lane, {model: calls}) from a run log, or None if not a parseable run log."""
    m = _LOGNAME.match(path.name)
    if not m:
        return None
    lane, ymd = m.group(1), m.group(2)
    day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    counts = {}
    for mm in _MODEL.findall(text):
        if mm != "model":                  # skip the 'model=model' placeholder artifact
            counts[mm] = counts.get(mm, 0) + 1
    return day, lane, counts


def rollup(data_dir, now=None) -> tuple:
    """Count new settled logs into the ledger. Returns (logs_added, calls_added)."""
    now = time.time() if now is None else now
    c = connect(data_dir)
    logdir = Path(data_dir) / "logs" / "cron-plus"
    processed = {r[0] for r in c.execute("SELECT log FROM processed")}
    logs_added = calls_added = 0
    for f in (sorted(logdir.glob("*.log")) if logdir.is_dir() else []):  # glob-ok: flat logs dir, not a sharded namespace
        if f.name in processed:
            continue
        try:
            if now - f.stat().st_mtime < SETTLE_SECONDS:
                continue                   # still settling; next rollup picks it up
        except OSError:
            continue
        parsed = parse_log(f)
        if parsed is None:
            continue
        day, lane, counts = parsed
        for model, n in counts.items():
            c.execute("INSERT INTO usage(day, model, lane, is_free, calls) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(day, model, lane) DO UPDATE SET calls = calls + excluded.calls",
                      (day, model, lane, 1 if is_free_model(model) else 0, n))
            calls_added += n
        c.execute("INSERT OR IGNORE INTO processed(log) VALUES(?)", (f.name,))
        logs_added += 1
    c.commit()
    c.close()
    return logs_added, calls_added


def reclassify(c) -> None:
    """Recompute is_free for every stored model per the CURRENT free logic, so a change to
    HERMES_LOCAL_MODELS (e.g. adding a local model) applies retroactively to historical rows."""
    for (model,) in c.execute("SELECT DISTINCT model FROM usage").fetchall():
        c.execute("UPDATE usage SET is_free=? WHERE model=?", (1 if is_free_model(model) else 0, model))
    c.commit()


def report(data_dir, days=14) -> str:
    c = connect(data_dir)
    reclassify(c)
    rows = c.execute(
        "SELECT day, SUM(calls), SUM(CASE WHEN is_free THEN calls ELSE 0 END) "
        "FROM usage GROUP BY day ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    head = f"Model-usage ledger ({db_path(data_dir)}):"
    if not rows:
        c.close()
        return head + "\n  (empty — no rollup yet)"
    L = [head, f"  {'day':<12}{'calls':>9}{'free%':>8}"]
    for day, total, free in rows:
        L.append(f"  {day:<12}{total:>9}{(100 * free // total if total else 0):>7}%")
    tot = c.execute("SELECT SUM(calls), SUM(CASE WHEN is_free THEN calls ELSE 0 END) "
                    "FROM usage").fetchone()
    if tot and tot[0]:
        L.append(f"  {'ALL TIME':<12}{tot[0]:>9}{(100 * tot[1] // tot[0]):>7}%   (cost offload)")
    L.append("  top models:")
    for model, calls, isf in c.execute(
            "SELECT model, SUM(calls), MAX(is_free) FROM usage "
            "GROUP BY model ORDER BY 2 DESC LIMIT 6"):
        L.append(f"    {calls:>8}  [{'free' if isf else 'PAID'}]  {model}")
    c.close()
    return "\n".join(L)


def main(argv) -> int:
    if argv and argv[0] == "report":
        data_dir = argv[1] if len(argv) > 1 else DEFAULT_DATA_DIR
        days = int(argv[2]) if len(argv) > 2 else 14
        print(report(data_dir, days))
        return 0
    data_dir = argv[0] if argv else DEFAULT_DATA_DIR
    logs, calls = rollup(data_dir)
    print(f"usage-rollup: +{logs} log(s), +{calls} call(s) -> {db_path(data_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
