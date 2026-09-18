"""A killed worker's session row must be closed HONESTLY (okengine#514).

Hermes closes a `sessions` row on the normal path only — `cron_complete`, or `agent_close`
via carried patch 05. Neither runs if the worker is killed (watchdog, force-recreate mid-run,
OOM), so the row keeps `ended_at IS NULL` forever and there is no automatic cleanup. Measured
1,701 orphans across five deployments (9.3% of 18,210), still accruing.

The reaper's value is entirely in *how* it closes them, so that is what these tests pin:

* an age gate, so a live job is never closed underneath itself;
* `ended_at` from the session's last real activity, NEVER `now` — stamping `now` invents a
  duration from the kill to whenever the reaper ran (weeks, for the old rows) and feeds that
  fabricated number into every duration metric;
* a distinct `end_reason`, so reaped rows never masquerade as real completions.

Also pins the numeric-cutoff trap: `started_at` is a REAL unix epoch, so comparing it against
a `datetime()` STRING makes the predicate trivially true for every row. That exact mistake
produced an "all orphans are >30 days old" reading during triage, which was false.
"""
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "reap_stale_sessions.py"


def _load():
    spec = importlib.util.spec_from_file_location("reap_stale_sessions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        """
    )
    return conn


def _session(conn, sid, started_at, ended_at=None, reason=None, messages=()):
    conn.execute("INSERT INTO sessions (id, started_at, ended_at, end_reason) VALUES (?,?,?,?)",
                 (sid, started_at, ended_at, reason))
    for stamp in messages:
        conn.execute("INSERT INTO messages (session_id, timestamp) VALUES (?,?)", (sid, stamp))
    conn.commit()


def _query(path: Path, statement: str):
    """Return materialized rows without leaking the short-lived assertion connection."""
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(statement).fetchall()
    finally:
        conn.close()


def test_ended_at_comes_from_last_activity_never_now(tmp_path):
    """The core property: a reaped row's duration must be the real one, not time-since-kill."""
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    now = time.time()
    started = now - 30 * 86400          # killed 30 days ago
    last_msg = started + 42             # its last message, 42s in
    _session(conn, "killed", started, messages=(started + 10, last_msg))
    conn.close()

    assert m.main(["--db", str(db)]) == 0

    row = _query(db, "SELECT ended_at, end_reason FROM sessions WHERE id='killed'")[0]
    assert row[0] == pytest.approx(last_msg), (
        "ended_at must be the last message timestamp; stamping `now` would record a "
        "30-day session that never happened")
    assert row[1] == "stale_reaper"
    assert row[0] - started == pytest.approx(42, abs=1), "duration must be the real 42s"


def test_a_session_with_no_messages_closes_at_its_own_start(tmp_path):
    """Zero-length is honest; a fabricated span is not."""
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    started = time.time() - 10 * 86400
    _session(conn, "empty", started)
    conn.close()

    assert m.main(["--db", str(db)]) == 0
    ended = _query(db, "SELECT ended_at FROM sessions WHERE id='empty'")[0][0]
    assert ended == pytest.approx(started), "with no messages, close at started_at"


def test_age_gate_protects_a_running_session(tmp_path):
    """A live job must never be closed underneath itself."""
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    now = time.time()
    _session(conn, "running", now - 60, messages=(now - 30,))       # 1 minute old
    _session(conn, "stale", now - 12 * 3600, messages=(now - 12 * 3600 + 5,))
    conn.close()

    assert m.main(["--db", str(db), "--age-hours", "6"]) == 0
    rows = dict(_query(db, "SELECT id, ended_at FROM sessions"))
    assert rows["running"] is None, "a session inside the age gate must be left open"
    assert rows["stale"] is not None, "a session older than the gate must be closed"


def test_already_closed_rows_are_never_rewritten(tmp_path):
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    started = time.time() - 20 * 86400
    _session(conn, "done", started, ended_at=started + 5, reason="cron_complete")
    conn.close()

    assert m.main(["--db", str(db)]) == 0
    row = _query(db, "SELECT ended_at, end_reason FROM sessions WHERE id='done'")[0]
    assert row == (pytest.approx(started + 5), "cron_complete"), (
        "a real completion must keep its own stamp and reason")


def test_reaped_rows_stay_distinguishable(tmp_path):
    """`stale_reaper` must not be confusable with a real completion."""
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    old = time.time() - 20 * 86400
    _session(conn, "real", old, ended_at=old + 3, reason="cron_complete")
    _session(conn, "orphan", old, messages=(old + 7,))
    conn.close()

    assert m.main(["--db", str(db)]) == 0
    mix = dict(_query(db, "SELECT end_reason, COUNT(*) FROM sessions GROUP BY end_reason"))
    assert mix == {"cron_complete": 1, "stale_reaper": 1}


def test_dry_run_changes_nothing(tmp_path, capsys):
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    old = time.time() - 20 * 86400
    _session(conn, "orphan", old, messages=(old + 1,))
    conn.close()

    assert m.main(["--db", str(db), "--dry-run"]) == 0
    assert "WOULD CLOSE" in capsys.readouterr().out
    assert _query(db, "SELECT ended_at FROM sessions WHERE id='orphan'")[0][0] is None


def test_limit_caps_rows_per_run(tmp_path):
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    old = time.time() - 20 * 86400
    for i in range(5):
        _session(conn, f"orphan{i}", old + i, messages=(old + i + 1,))
    conn.close()

    assert m.main(["--db", str(db), "--limit", "2"]) == 0
    closed = _query(db, "SELECT COUNT(*) FROM sessions WHERE ended_at IS NOT NULL")[0][0]
    assert closed == 2


def test_missing_db_and_missing_schema_are_clean_no_ops(tmp_path, capsys):
    """A fresh deploy has no state; this lane must not fail it."""
    m = _load()
    assert m.main(["--db", str(tmp_path / "absent.db")]) == 0
    assert '"wakeAgent": false' in capsys.readouterr().out

    bare = tmp_path / "bare.db"
    conn = sqlite3.connect(str(bare))
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    assert m.main(["--db", str(bare)]) == 0
    assert '"reaped": 0' in capsys.readouterr().out


def test_emits_a_wake_false_receipt_line(tmp_path, capsys):
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    old = time.time() - 20 * 86400
    _session(conn, "orphan", old, messages=(old + 1,))
    conn.close()

    assert m.main(["--db", str(db)]) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["wakeAgent"] is False
    assert payload["reaped"] == 1
    assert payload["orphans_remaining"] == 0


def test_cutoff_is_numeric_not_a_datetime_string(tmp_path):
    """The triage trap, pinned.

    `started_at` is a REAL epoch. Comparing it against a `datetime('now', ...)` STRING makes
    the predicate trivially true — SQLite orders REAL before TEXT — so every row matches and
    the age gate silently disappears. This asserts a fresh session is NOT selected, which is
    exactly what a string cutoff would break.
    """
    m = _load()
    db = tmp_path / "state.db"
    conn = _db(db)
    now = time.time()
    _session(conn, "fresh", now - 5, messages=(now - 1,))
    stale = m.find_stale(conn, now - 6 * 3600)
    conn.close()
    assert stale == [], (
        "a 5-second-old session must not be selected by a 6h cutoff; if it is, the cutoff is "
        "being compared as a string")


def test_environment_parsers_and_default_paths(monkeypatch):
    m = _load()
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OKENGINE_STATE_DB", raising=False)
    assert m._hermes_home() == "/opt/data"
    assert m._state_db_path() == "/opt/data/state.db"
    monkeypatch.setenv("HERMES_HOME", "/srv/hermes")
    assert m._state_db_path() == "/srv/hermes/state.db"
    monkeypatch.setenv("OKENGINE_STATE_DB", "/tmp/custom.db")
    assert m._state_db_path() == "/tmp/custom.db"
    for raw in ("", "bad", "0", "-2"):
        monkeypatch.setenv("FLOAT", raw)
        assert m._env_float("FLOAT", 3.0) == 3.0
    monkeypatch.setenv("FLOAT", "2.5")
    assert m._env_float("FLOAT", 3.0) == 2.5
    for raw in ("", "bad"):
        monkeypatch.setenv("INT", raw)
        assert m._env_int("INT", 4) == 4
    monkeypatch.setenv("INT", "-2")
    assert m._env_int("INT", 4) == 0


def test_find_stale_ignores_non_numeric_activity_and_empty_reap():
    m = _load()

    class Rows:
        def fetchall(self):
            return [("bad", "yesterday"), (7, 12)]

    class Conn:
        def execute(self, sql, params):
            assert "LIMIT ?" in sql and params == [10.0, 2]
            return Rows()

    assert m.find_stale(Conn(), 10.0, 2) == [("7", 12.0)]
    assert m.reap(Conn(), []) == 0


def test_invalid_age_connect_error_and_large_dry_run(tmp_path, monkeypatch, capsys):
    m = _load()
    assert m.main(["--db", str(tmp_path / "x"), "--age-hours", "0"]) == 2
    db = tmp_path / "exists.db"
    db.touch()
    monkeypatch.setattr(m.sqlite3, "connect", lambda _p: (_ for _ in ()).throw(
        m.sqlite3.Error("denied")))
    assert m.main(["--db", str(db)]) == 1

    class Result:
        def fetchone(self):
            return (21,)

    class Conn:
        def execute(self, *_args):
            return Result()

        def close(self):
            pass

    monkeypatch.setattr(m.sqlite3, "connect", lambda _p: Conn())
    monkeypatch.setattr(m, "find_stale", lambda *_a: [(str(i), 1.0) for i in range(21)])
    assert m.main(["--db", str(db), "--dry-run"]) == 0
    assert "and 1 more" in capsys.readouterr().out
