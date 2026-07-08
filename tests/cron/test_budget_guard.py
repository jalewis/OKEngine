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

    def _fake_cronplus(action, jid):
        # mirror real cron-plus: pause/resume flips the enabled flag in jobs.json, so the reconcile
        # (invariant-audit #13) sees the true live state on the next tick.
        calls.append((action, jid))
        d = json.loads(jobs.read_text())
        for j in d["jobs"]:
            if j.get("id") == jid:
                j["enabled"] = (action == "resume")
        jobs.write_text(json.dumps(d))
        return True

    monkeypatch.setattr(m, "_cronplus", _fake_cronplus)
    assert m.main([]) == 0 and ("pause", "a") in calls          # trips
    calls.clear()
    assert m.main([]) == 0 and calls == []                      # manual: stays paused, no auto-resume
    assert m.resume("manual") == 1 and calls == [("resume", "a")]   # operator recovery
    assert json.loads((tmp_path / "budget-guard-state.json").read_text())["paused"] is False


def test_cost_bearing_ids_skips_already_disabled():  # invariant-audit #19
    """An already-disabled job (operator maintenance pause) must NOT be captured for pausing —
    else auto-resume flips it back to enabled, silently reverting the operator's deliberate pause."""
    m = _load()
    jobs = [
        {"id": "a", "name": "raw-backfill", "enabled": False},              # operator-disabled -> skip
        {"id": "b", "name": "brief", "enabled": True},
        {"id": "c", "name": "drain"},                                        # enabled defaults True
        {"id": "d", "name": "cleanup", "no_agent": True, "enabled": True},   # free -> skip
    ]
    names = [n for _, n in m.cost_bearing_ids(jobs)]
    assert "raw-backfill" not in names                                       # not re-enabled on resume
    assert "brief" in names and "drain" in names
    assert "cleanup" not in names


def test_usd_budget_without_price_warns_inert(tmp_path, monkeypatch, capsys):  # invariant-audit #18
    """A USD cap needs OKENGINE_BUDGET_PRICE_PER_MTOK to convert tokens->USD. Without it the USD
    term is always False and the cap SILENTLY never trips (fail-open). The guard must WARN so the
    operator knows they aren't actually capped."""
    m = _load()
    db = tmp_path / "state.db"
    _make_db(db, [])
    monkeypatch.setenv("OKENGINE_STATE_DB", str(db))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OKENGINE_BUDGET_USD", "10")
    monkeypatch.delenv("OKENGINE_BUDGET_PRICE_PER_MTOK", raising=False)
    monkeypatch.delenv("OKENGINE_BUDGET_TOKENS", raising=False)
    assert m.main([]) == 0
    assert "USD cap is INERT" in capsys.readouterr().err


def test_trip_does_not_claim_paused_when_pause_fails(tmp_path, monkeypatch):
    """okengine invariant-audit #3: if cron-plus pause fails for all cost-bearing crons, the guard
    must NOT persist paused=True — that makes decide() no-op forever while crons keep spending past
    the cap (fail-open). paused stays False so the next tick re-attempts."""
    m = _load()
    db = tmp_path / "state.db"; _make_db(db, [(1_000_000.0 - 10, 2_000_000, 0)])
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"id": "a", "name": "raw-backfill"}]}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OKENGINE_STATE_DB", str(db))
    monkeypatch.setenv("OKENGINE_CRON_PLUS_JOBS", str(jobs))
    monkeypatch.setenv("OKENGINE_BUDGET_TOKENS", "1000000")
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 1_000_000.0)}))
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: False)   # every pause FAILS
    assert m.main([]) == 0
    state = json.loads((tmp_path / "budget-guard-state.json").read_text())
    assert state["paused"] is False                 # NOT tripped-on-paper — retry next tick
    assert state["paused_ids"] == []
    # decide() therefore re-attempts the pause rather than no-op'ing
    assert m.decide(over_budget=True, currently_paused=False, resume_policy="manual") == "pause"


def test_malformed_budget_fails_closed(tmp_path, monkeypatch):  # invariant-audit #22
    """A SET-but-malformed budget env (`$50`, `1,000,000`) must NOT crash the guard or silently run
    uncapped (fail-open). It must fail CLOSED: pause the cost-bearing crons so the deployment isn't
    left spending while the operator believes a cap is set."""
    m = _load()
    db = tmp_path / "state.db"
    _make_db(db, [(1_000_000.0 - 10, 5, 5)])          # trivial usage, well under any real cap
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"id": "a", "name": "raw-backfill"}]}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OKENGINE_STATE_DB", str(db))
    monkeypatch.setenv("OKENGINE_CRON_PLUS_JOBS", str(jobs))
    monkeypatch.setenv("OKENGINE_BUDGET_TOKENS", "1,000,000")   # thousands separators -> not int()
    monkeypatch.delenv("OKENGINE_BUDGET_USD", raising=False)
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 1_000_000.0)}))
    calls = []
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: calls.append((action, jid)) or True)
    assert m.main([]) == 0                              # does not crash
    assert ("pause", "a") in calls                     # fail-closed: paused, not left uncapped
    state = json.loads((tmp_path / "budget-guard-state.json").read_text())
    assert state["paused"] is True


def test_reconcile_repauses_after_jobs_redeploy(tmp_path, monkeypatch):  # invariant-audit #13
    """The guard trusts its own state file. A wholesale jobs.json redeploy re-enables every
    cost-bearing cron while state still says paused -> decide() no-ops and the cap is silently
    defeated. The next tick must reconcile against the LIVE enabled-status and re-pause the crons."""
    m = _load()
    db = tmp_path / "state.db"
    _make_db(db, [(1_000_000.0 - 10, 2_000_000, 0)])   # still over budget
    # state says we paused "a"; a redeploy has re-enabled it in the live jobs.json
    _seed_paused(tmp_path, ["a"])
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"id": "a", "name": "raw-backfill", "enabled": True}]}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("OKENGINE_STATE_DB", str(db))
    monkeypatch.setenv("OKENGINE_CRON_PLUS_JOBS", str(jobs))
    monkeypatch.setenv("OKENGINE_BUDGET_TOKENS", "1000000")
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 1_000_000.0)}))
    calls = []
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: calls.append((action, jid)) or True)
    assert m.main([]) == 0
    assert calls == [("pause", "a")]                   # drifted cron re-paused (kill-switch holds)

    # and when the recorded cron is genuinely still disabled, reconcile is a no-op (no churn)
    calls.clear()
    jobs.write_text(json.dumps({"jobs": [{"id": "a", "name": "raw-backfill", "enabled": False}]}))
    assert m.main([]) == 0
    assert calls == []


def test_resume_keeps_paused_when_some_resume_fails(tmp_path, monkeypatch):
    """okengine invariant-audit #14: a partial resume (cron-plus fails for some ids) must NOT clear
    paused — that strands those crons disabled while reporting 'not paused'. Keep paused with only
    the still-disabled ids so the next tick re-resumes them."""
    m = _load()
    _seed_paused(tmp_path, ["a", "c"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(m, "time", type("T", (), {"time": staticmethod(lambda: 2_000_000.0)}))
    monkeypatch.setattr(m, "_cronplus", lambda action, jid: jid == "a")   # "c" resume FAILS
    assert m.resume("auto") == 1                     # one resumed
    state = json.loads((tmp_path / "budget-guard-state.json").read_text())
    assert state["paused"] is True                   # still paused (not falsely cleared)
    assert state["paused_ids"] == ["c"]              # only the still-disabled one retained
