"""Runtime freshness policy regressions for cross-lane ``after:`` (#129)."""
import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "patches" / "cron-plus" / "after_ordering.py"


def _mod():
    spec = importlib.util.spec_from_file_location("after_ordering", POLICY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _up(token="2026-07-16T12:00:00+00:00", success=True):
    return {"name": "up", "last_run_success": success, "last_completed_at": token}


def test_no_after_edge_is_unrestricted():
    ready, claim, reason = _mod().after_ready({"name": "free"}, {})
    assert ready and claim == {} and reason is None


def test_downstream_waits_for_successful_upstream_completion():
    m = _mod()
    down = {"name": "down", "after": ["up"]}
    ready, _, reason = m.after_ready(down, {"up": _up(success=None)})
    assert not ready and "no successful completion" in reason

    ready, claim, reason = m.after_ready(down, {"up": _up()})
    assert ready and claim == {"up": "2026-07-16T12:00:00+00:00"} and reason is None


def test_success_consumes_exact_input_and_requires_a_newer_completion():
    m = _mod()
    down = {"name": "down", "after": ["up"], "last_run_success": True}
    jobs = {"up": _up()}
    ready, claim, _ = m.after_ready(down, jobs)
    assert ready

    m.begin_claim(down, claim)
    assert down["last_run_success"] is None       # in progress is not prior success
    m.record_outcome(down, True, "2026-07-16T12:05:00+00:00")
    assert down["after_consumed"] == claim
    assert "after_claim" not in down

    ready, _, reason = m.after_ready(down, jobs)
    assert not ready and "no fresh completion" in reason
    jobs["up"] = _up("2026-07-16T13:00:00+00:00")
    assert m.after_ready(down, jobs)[0]


def test_failed_downstream_does_not_consume_freshness_and_can_retry():
    m = _mod()
    down = {
        "name": "down", "after": ["up"],
        "after_consumed": {"up": "2026-07-16T11:00:00+00:00"},
    }
    jobs = {"up": _up()}
    ready, claim, _ = m.after_ready(down, jobs)
    assert ready
    m.begin_claim(down, claim)
    m.record_outcome(down, False, "2026-07-16T12:05:00+00:00")
    assert down["after_consumed"] == {"up": "2026-07-16T11:00:00+00:00"}
    assert m.after_ready(down, jobs)[0]


def test_multiple_dependencies_are_all_of_and_runtime_errors_fail_closed():
    m = _mod()
    down = {"name": "down", "after": ["a", "b"]}
    ready, _, reason = m.after_ready(down, {"a": _up()})
    assert not ready and "'b' is absent" in reason

    by_name = {"a": _up(), "b": {**_up(), "last_run_success": False}}
    assert not m.after_ready(down, by_name)[0]
    by_name["b"] = _up("2026-07-16T12:01:00+00:00")
    ready, claim, _ = m.after_ready(down, by_name)
    assert ready and set(claim) == {"a", "b"}

    assert not m.after_ready({"after": "a"}, by_name)[0]
    assert not m.after_ready({"after": [""]}, by_name)[0]
    assert not m.after_ready({"after": ["a"], "after_consumed": []}, by_name)[0]


def test_legacy_success_state_upgrades_via_last_run_at_fallback():
    m = _mod()
    legacy = {"name": "up", "last_run_success": True,
              "last_run_at": "2026-07-16T12:00:00+00:00"}
    ready, claim, _ = m.after_ready({"after": ["up"]}, {"up": legacy})
    assert ready and claim["up"] == legacy["last_run_at"]


def test_carried_patch_wires_policy_at_atomic_claim_and_completion_boundaries():
    text = (REPO / "patches" / "cron-plus" / "after-ordering.patch").read_text()
    assert "ready, after_claim, reason = after_ready(job, by_name)" in text
    assert "begin_claim(job, after_claim)" in text
    assert "record_outcome(job, success" in text
    assert text.index("after_ready(job, by_name)") < text.index("compute_next_run(schedule")
