"""Tests for scripts/cron/budget_guard.py — the engine spend cap (okengine#35)."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "budget_guard.py"


def _load():
    sys.modules.pop("budget_guard", None)
    spec = importlib.util.spec_from_file_location("budget_guard", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["budget_guard"] = m
    spec.loader.exec_module(m)
    return m


def _make_db(path, rows):
    """rows = [(started_at, input, output)]"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (started_at REAL, input_tokens INT, "
                "output_tokens INT, cache_read_tokens INT, cache_write_tokens INT, "
                "reasoning_tokens INT)")
    con.executemany("INSERT INTO sessions(started_at,input_tokens,output_tokens,"
                    "cache_read_tokens,cache_write_tokens,reasoning_tokens) "
                    "VALUES (?,?,?,0,0,0)", rows)
    con.commit(); con.close()


def test_window_seconds():
    m = _load()
    assert m.window_seconds("day") == 86400
    assert m.window_seconds("week") == 604800
    assert m.window_seconds("month") == 2592000
    assert m.window_seconds("bogus") == 86400   # default day


def test_tokens_in_window_sums_only_recent(tmp_path):
    m = _load()
    db = tmp_path / "state.db"
    now = 1_000_000.0
    _make_db(db, [
        (now - 100, 1000, 200),       # in window
        (now - 50, 500, 100),         # in window
        (now - 90000, 9999, 9999),    # older than 1 day -> excluded
    ])
    # day window: only the two recent rows -> 1000+200+500+100 = 1800
    assert m.tokens_in_window(db, m.window_seconds("day"), now) == 1800
    # week window: includes the old row too -> +19998
    assert m.tokens_in_window(db, m.window_seconds("week"), now) == 1800 + 19998


def test_tokens_in_window_missing_db_is_zero(tmp_path):
    m = _load()
    assert m.tokens_in_window(tmp_path / "nope.db", 86400, 1.0) == 0


def test_cost_bearing_ids_excludes_no_agent_and_self():
    m = _load()
    jobs = [
        {"id": "a", "name": "raw-backfill"},                       # agent -> included
        {"id": "b", "name": "reshelve", "no_agent": True},         # free -> excluded
        {"id": "c", "name": "feed-fetch", "no_agent": True},       # free -> excluded
        {"id": "d", "name": "budget-guard", "no_agent": True},     # self -> excluded
        {"id": "e", "name": "entity-backfill"},                    # agent -> included
        {"name": "no-id"},                                         # no id -> skipped
    ]
    got = m.cost_bearing_ids(jobs)
    assert got == [("a", "raw-backfill"), ("e", "entity-backfill")]


def test_decide_matrix():
    m = _load()
    assert m.decide(over_budget=True, currently_paused=False, resume_policy="auto") == "pause"
    assert m.decide(over_budget=True, currently_paused=True, resume_policy="auto") == "noop"
    assert m.decide(over_budget=False, currently_paused=True, resume_policy="auto") == "resume"
    assert m.decide(over_budget=False, currently_paused=True, resume_policy="manual") == "noop"
    assert m.decide(over_budget=False, currently_paused=False, resume_policy="auto") == "noop"


def test_estimated_usd():
    m = _load()
    assert m.estimated_usd(1_000_000, 2.0) == 2.0
    assert m.estimated_usd(500_000, 4.0) == 2.0
    assert m.estimated_usd(1_000_000, 0) == 0.0   # no price -> no estimate


def test_main_noop_when_no_budget(monkeypatch, capsys):
    m = _load()
    monkeypatch.delenv("OKENGINE_BUDGET_TOKENS", raising=False)
    monkeypatch.delenv("OKENGINE_BUDGET_USD", raising=False)
    assert m.main([]) == 0
    assert "disabled" in capsys.readouterr().out


def test_main_pauses_over_budget(tmp_path, monkeypatch):
    m = _load()
    # state db over the token budget
    db = tmp_path / "state.db"
    _make_db(db, [(1_000_000.0 - 10, 2_000_000, 0)])
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [
        {"id": "a", "name": "raw-backfill"},
        {"id": "b", "name": "reshelve", "no_agent": True},
    ]}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OKENGINE_STATE_DB", str(db))
    monkeypatch.setenv("OKENGINE_CRON_PLUS_JOBS", str(jobs))
    monkeypatch.setenv("OKENGINE_BUDGET_TOKENS", "1000000")
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 1_000_000.0)}))
    paused = []
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: paused.append((action, jid)) or True)
    assert m.main([]) == 0
    assert paused == [("pause", "a")]                       # only the agent job paused
    state = json.loads((tmp_path / "budget-guard-state.json").read_text())
    assert state["paused"] is True and state["paused_ids"] == ["a"]


def _seed_paused(tmp_path, ids):
    (tmp_path / "budget-guard-state.json").write_text(json.dumps(
        {"paused": True, "paused_ids": ids, "paused_names": ids,
         "tripped_at": 1.0, "reason": "over budget (test)"}))


def test_resume_reenables_and_clears_state(tmp_path, monkeypatch):
    """okengine#97: resume() re-enables the paused crons and clears the pause state."""
    m = _load()
    _seed_paused(tmp_path, ["a", "c"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 2_000_000.0)}))
    calls = []
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: calls.append((action, jid)) or True)
    assert m.resume("manual") == 2
    assert calls == [("resume", "a"), ("resume", "c")]
    state = json.loads((tmp_path / "budget-guard-state.json").read_text())
    assert state["paused"] is False and state["note"] == "manual-resume"


def test_resume_noop_when_not_paused(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # no state file -> not paused
    calls = []
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: calls.append((action, jid)) or True)
    assert m.resume("manual") == 0
    assert calls == []


def test_manual_mode_pauses_and_resume_is_the_recovery(tmp_path, monkeypatch):
    """End-to-end #97: in manual mode the guard pauses and NEVER auto-resumes (a second
    run stays paused); resume() is the only supported way back."""
    m = _load()
    db = tmp_path / "state.db"
    _make_db(db, [(1_000_000.0 - 10, 2_000_000, 0)])
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"id": "a", "name": "raw-backfill"}]}))
    for k, v in {"HERMES_HOME": str(tmp_path), "OKENGINE_STATE_DB": str(db),
                 "OKENGINE_CRON_PLUS_JOBS": str(jobs), "OKENGINE_BUDGET_TOKENS": "1000000",
                 "OKENGINE_BUDGET_RESUME": "manual"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 1_000_000.0)}))
    calls = []
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: calls.append((action, jid)) or True)
    assert m.main([]) == 0 and ("pause", "a") in calls          # trips
    calls.clear()
    assert m.main([]) == 0 and calls == []                      # manual: stays paused, no auto-resume
    assert m.resume("manual") == 1 and calls == [("resume", "a")]   # operator recovery
    assert json.loads((tmp_path / "budget-guard-state.json").read_text())["paused"] is False
