#!/usr/bin/env python3
"""reap-stale-sessions — close session rows a killed worker left open (okengine#514).

A `no_agent` cron (it never calls the LLM). Hermes closes a `sessions` row on the
normal path — `cron_complete` for a cron run, `agent_close` via carried patch
05-delegate-tool-session-end for a delegate — but NEITHER runs if the worker dies:
a watchdog kill, `docker compose restart`/force-recreate mid-run, or an OOM. The row
then keeps `ended_at IS NULL` forever, and Hermes has no automatic cleanup.

Measured on this fleet before the fix: 1,701 orphans across five deployments
(9.3% of 18,210 rows), still accruing — four appeared during one evening's gateway
recreates. Reported by a sibling team as their #512 item 3; they saw 107.

This is not only tidiness. An orphaned row is indistinguishable from a running one,
so anything counting "sessions that finished" against "sessions that started" is
reading an inflated denominator — the lane-health surfaces built in okengine#483 and
okengine#487 included.

Three rules, each of which exists because the obvious shortcut is wrong:

1. AGE GATE. Only rows older than the threshold are touched, so a genuinely running
   job is never closed underneath itself. Default 6h, which is far beyond any real
   lane (the longest observed run is minutes) and beyond the 900s quiesce the
   qualification matrix waits.

2. `ended_at` COMES FROM THE LAST MESSAGE, NEVER `now`. Stamping `now` would invent
   a duration stretching from the kill to whenever the reaper happened to run —
   weeks, for the old rows — and that fabricated number would then flow into every
   duration metric. A session's last real activity is its last message; with no
   messages at all, `started_at` is used, giving a zero-length session rather than
   a fictional one.

3. A DISTINCT `end_reason`. `stale_reaper` is never confused with a real
   `cron_complete`/`agent_close`, so the reaped population stays auditable and a
   future measurement can exclude it.

Env (all optional):
  OKENGINE_STATE_DB            path (default $HERMES_HOME/state.db or /opt/data/state.db)
  OKENGINE_SESSION_REAP_AGE_H  float hours (default 6) — minimum age to reap
  OKENGINE_SESSION_REAP_LIMIT  int (default 0 = no limit) — cap rows per run

Exit 0 on success, including "nothing to do". Emits a wakeAgent=false JSON line so
cron-plus records it as a deterministic run.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SELF_NAME = "reap-stale-sessions"
END_REASON = "stale_reaper"
DEFAULT_AGE_HOURS = 6.0


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME") or "/opt/data"


def _state_db_path() -> str:
    return os.environ.get("OKENGINE_STATE_DB") or str(Path(_hermes_home()) / "state.db")


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def find_stale(conn: sqlite3.Connection, cutoff: float, limit: int = 0) -> list[tuple[str, float]]:
    """Sessions with no `ended_at` that started before `cutoff`, with the timestamp to
    close each at: its last message, else its own `started_at`.

    `started_at`/`timestamp` are REAL unix epochs, so the comparison is numeric. A
    string cutoff would compare REAL against TEXT, which SQLite orders type-first and
    makes trivially true for every row — an inflated result that reads as a finding.
    """
    sql = """
        SELECT s.id,
               COALESCE(MAX(m.timestamp), s.started_at) AS last_activity
          FROM sessions s
          LEFT JOIN messages m ON m.session_id = s.id
         WHERE s.ended_at IS NULL
           AND s.started_at IS NOT NULL
           AND s.started_at < ?
         GROUP BY s.id
         ORDER BY s.started_at
    """
    params: list[object] = [cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out: list[tuple[str, float]] = []
    for session_id, last_activity in rows:
        stamp = last_activity if isinstance(last_activity, (int, float)) else None
        if stamp is None:
            continue
        out.append((str(session_id), float(stamp)))
    return out


def reap(conn: sqlite3.Connection, stale: list[tuple[str, float]]) -> int:
    """Close each row at its own last-activity stamp. One transaction."""
    if not stale:
        return 0
    conn.executemany(
        "UPDATE sessions SET ended_at = ?, end_reason = ? "
        " WHERE id = ? AND ended_at IS NULL",
        [(stamp, END_REASON, session_id) for session_id, stamp in stale],
    )
    conn.commit()
    return len(stale)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="state DB path (default: OKENGINE_STATE_DB)")
    parser.add_argument("--age-hours", type=float, default=None,
                        help="minimum session age to reap (default 6, or "
                             "OKENGINE_SESSION_REAP_AGE_H)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap rows closed this run (default unlimited)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be closed, change nothing")
    args = parser.parse_args(argv)

    db_path = Path(args.db or _state_db_path())
    age_hours = args.age_hours if args.age_hours is not None else _env_float(
        "OKENGINE_SESSION_REAP_AGE_H", DEFAULT_AGE_HOURS)
    if age_hours <= 0:
        print(f"{SELF_NAME}: --age-hours must be positive (got {age_hours})", file=sys.stderr)
        return 2
    limit = args.limit if args.limit is not None else _env_int("OKENGINE_SESSION_REAP_LIMIT", 0)

    now = time.time()
    cutoff = now - age_hours * 3600.0

    # A missing DB is a clean no-op: a fresh deploy has no state yet, and this lane
    # must not fail a deployment that simply has not run an agent.
    if not db_path.is_file():
        print(f"{SELF_NAME}: no state DB at {db_path} — nothing to do")
        print(json.dumps({"wakeAgent": False, "reaped": 0, "orphans_remaining": 0}))
        return 0

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        print(f"{SELF_NAME}: cannot open {db_path}: {exc}", file=sys.stderr)
        return 1

    try:
        try:
            stale = find_stale(conn, cutoff, limit)
        except sqlite3.Error as exc:
            # No sessions/messages table (a non-Hermes or pre-migration DB) is a
            # no-op, not a failure.
            print(f"{SELF_NAME}: state DB has no reapable session schema ({exc})")
            print(json.dumps({"wakeAgent": False, "reaped": 0, "orphans_remaining": 0}))
            return 0

        print(f"=== {SELF_NAME} ===")
        print(f"  db          : {db_path}")
        print(f"  age gate    : {age_hours:g}h (cutoff {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cutoff))})")
        print(f"  stale found : {len(stale)}")

        if args.dry_run:
            for session_id, stamp in stale[:20]:
                print(f"  WOULD CLOSE {session_id} at "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp))}")
            if len(stale) > 20:
                print(f"  … and {len(stale) - 20} more")
            reaped = 0
        else:
            reaped = reap(conn, stale)
            print(f"  reaped      : {reaped} (end_reason={END_REASON})")

        # Report what is LEFT so a persistent backlog is visible rather than implied
        # by absence. Rows younger than the age gate are expected and not a problem.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
        print(f"  open rows   : {remaining} (includes live sessions and any inside the age gate)")
    finally:
        conn.close()

    print(json.dumps({"wakeAgent": False, "reaped": reaped,
                      "orphans_remaining": int(remaining)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
