"""Regression: fleet-health must WAKE on sustained red, not merely render it.

The actor assessment lane broke on a stale review key and stayed red for six hours — about
twenty-four runs of this job at its 15-minute cadence. Every one of them drew the row, printed the
red line, and emitted `wakeAgent: False`. Nobody was told, because a dashboard is not a watchdog.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "fleet_health.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="fleet_health absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("fleet_health", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_health"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(mod, state, lane_sets, iso="2026-08-08T00:00:00Z"):
    return mod.escalate(state, lane_sets, iso)


def test_a_single_red_run_does_not_wake_anyone():
    """Hysteresis: waking on the first blip trains the reader to ignore the channel."""
    mod = _load()
    state, wake = _run(mod, {}, {"errored": ["assessment-pipeline"]})
    assert wake == []
    assert state["lanes"]["assessment-pipeline"]["consecutive"] == 1


def test_sustained_red_wakes_at_the_threshold():
    mod = _load()
    state, wake = {}, []
    for _ in range(mod.ESCALATE_AFTER):
        state, wake = _run(mod, state, {"errored": ["assessment-pipeline"]})
    assert wake == ["assessment-pipeline"], "a lane red across the threshold must wake the agent"


def test_sustained_invalid_schedule_wakes_instead_of_remaining_never_run():
    mod = _load()
    state, wake = {}, []
    for _ in range(mod.ESCALATE_AFTER):
        state, wake = _run(mod, state, {"invalid-schedule": ["dead-lane"]})
    assert wake == ["dead-lane"]
    assert state["lanes"]["dead-lane"]["status"] == "invalid-schedule"


def test_it_does_not_wake_again_every_single_run():
    """Loud enough not to be forgotten, quiet enough to stay worth reading."""
    mod = _load()
    state, wakes = {}, 0
    for _ in range(mod.ESCALATE_AFTER + mod.REWAKE_EVERY):
        state, wake = _run(mod, state, {"errored": ["assessment-pipeline"]})
        wakes += bool(wake)
    assert wakes == 2, "one wake at the threshold, one re-wake — not one per run"


def test_recovery_clears_the_counter():
    mod = _load()
    state, _ = _run(mod, {}, {"errored": ["assessment-pipeline"]})
    state, wake = _run(mod, state, {"ok": ["assessment-pipeline"]})
    assert wake == [] and state["lanes"] == {}


def test_changing_from_one_red_status_to_another_does_not_reset_the_counter():
    """errored -> timed-out is still broken; the clock must not restart on a relabel."""
    mod = _load()
    state, _ = _run(mod, {}, {"errored": ["lane"]})
    state, _ = _run(mod, state, {"timed-out": ["lane"]})
    assert state["lanes"]["lane"]["consecutive"] == 2
    assert state["lanes"]["lane"]["red_since"] == "2026-08-08T00:00:00Z"


def test_load_and_absence_statuses_never_escalate():
    """`saturated`/`stale` are load, and `never-run` is usually a weekly lane whose day has not
    come round — none of them mean a lane is broken."""
    mod = _load()
    for _ in range(mod.ESCALATE_AFTER + 1):
        state, wake = _run(mod, {}, {"saturated": ["a"], "stale": ["b"], "never-run": ["c"]})
        assert wake == [] and state["lanes"] == {}


def test_missing_history_is_a_first_sighting_not_an_all_clear():
    """An unreadable state file must not be read as 'nothing was wrong before'."""
    mod = _load()
    assert mod._escalation_state(Path("/nonexistent/fleet.json")) == {}


def test_the_monitor_failing_on_its_own_output_always_wakes():
    """The one fault no other lane can report — every consumer keeps reading the frozen copy."""
    src = MOD.read_text(encoding="utf-8")
    marker = "cannot write dashboard/sidecar"
    tail = src[src.index(marker):src.index(marker) + 900]
    assert '{"wakeAgent": True}' in tail.replace("'", '"') or 'wakeAgent": True' in tail, tail


def test_a_corrupt_history_file_is_a_first_sighting_not_an_all_clear(tmp_path):
    """State that parses but is not a mapping has no per-lane counters in it. Reading it as an empty
    history restarts the count; reading it as anything else would silently drop lanes that were
    already red — the escalation would then never fire for exactly the lanes that need it."""
    mod = _load()
    path = tmp_path / ".fleet-escalation.json"
    path.write_text(json.dumps(["lanes"]), encoding="utf-8")
    assert mod._escalation_state(path) == {}
    path.write_text("{not json", encoding="utf-8")
    assert mod._escalation_state(path) == {}
    assert mod._escalation_state(tmp_path / "absent.json") == {}


# ---------------------------------------------------------------------------
# The escalation as `main()` actually runs it. `escalate()` is pure and pinned
# above; these cover the wiring around it — persisting the history, and the one
# line an operator is meant to act on.
# ---------------------------------------------------------------------------

import os
import time


def _errored_fleet(tmp, monkeypatch):
    """A single lane that has been failing, and the env `main()` reads it through."""
    (tmp / "wiki").mkdir(parents=True, exist_ok=True)
    jobs = tmp / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [
        {"name": "broken", "enabled": True, "schedule": {"expr": "0 0 * * *"}}]}), encoding="utf-8")
    logs = tmp / "logs"
    logs.mkdir(exist_ok=True)
    log = logs / "broken-20260809-120000.log"
    log.write_text("Traceback (most recent call last):\nboom\n"
                   "ERROR cron-plus.runner: agent run failed: Script exited\n", encoding="utf-8")
    recent = time.time() - 60
    os.utime(log, (recent, recent))
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    monkeypatch.setenv("CRON_JOBS", str(jobs))
    monkeypatch.setenv("CRON_LOGS", str(logs))


def test_a_lane_red_across_consecutive_runs_finally_wakes_an_agent(tmp_path, monkeypatch, capsys):
    """End to end through `main()`: the counter has to SURVIVE between runs, which means the state
    file has to be written and read back. A pure-function test cannot see that — an escalate() that
    works perfectly against a history nothing persists never fires."""
    _errored_fleet(tmp_path, monkeypatch)

    threshold = _load().ESCALATE_AFTER
    for run in range(1, threshold):
        mod = _load()
        assert mod.main() == 0
        assert '{"wakeAgent": false}' in capsys.readouterr().out, f"run {run} must not wake yet"

    mod = _load()
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "fleet-health: ESCALATING" in out
    assert "broken (errored," in out and "consecutive runs since" in out
    assert '{"wakeAgent": true}' in out
    state = json.loads((tmp_path / "wiki" / "dashboards" / ".fleet-escalation.json").read_text())
    assert state["lanes"]["broken"]["consecutive"] == mod.ESCALATE_AFTER


def test_an_unwritable_history_degrades_to_this_run_only_and_says_so(tmp_path, monkeypatch, capsys):
    """Losing the history means losing the ability to say "still". The run must still report what it
    can see right now — going silent because a side-file could not be written would turn a disk
    problem into an all-clear."""
    _errored_fleet(tmp_path, monkeypatch)
    mod = _load()
    real_write = Path.write_text

    def refuse(self, *a, **kw):
        if ".fleet-escalation" in self.name:
            raise OSError(28, "ENOSPC")
        return real_write(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", refuse)
    assert mod.main() == 0
    captured = capsys.readouterr()
    assert "cannot persist escalation state" in captured.err
    assert "consecutive-run tracking is unavailable" in captured.err
    assert (tmp_path / "wiki" / "dashboards" / "fleet-health.md").is_file(), (
        "the dashboard is still written")
