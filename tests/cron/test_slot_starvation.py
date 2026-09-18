"""slot_starvation (okengine#614): the shared scan behind both fleet monitors.

The scan lived only in the operator-invoked fleet_status.py, so the class it detects went
unwatched for six weeks. It now has two consumers with different cadences; these tests pin the
library itself, and the consumers pin their own wiring.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "slot_starvation.py"

KILLED = "TimeoutError: cron-plus run exceeded hard timeout of 600s"
DENIED = ("TimeoutError: model slot unavailable after 300.0s "
          "(identity=custom|http://gpu:11436/v1|local-model:30b, limit=4)")


def _mod():
    spec = importlib.util.spec_from_file_location("slot_starvation", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["slot_starvation"] = m
    spec.loader.exec_module(m)
    return m


def _runs(tmp, records):
    for job_id, recs in records.items():
        d = tmp / "cron-plus" / "runs" / job_id
        d.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(recs):
            (d / f"r{i}.json").write_text(json.dumps(r), encoding="utf-8")


def test_zero_turn_kill_on_an_agent_lane_is_starvation(tmp_path):
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "raw-backfill", "status": "failed", "error": KILLED,
         "executed_tool_call_turns": 0},
        {"lane": "raw-backfill", "status": "succeeded", "executed_tool_call_turns": 2},
    ]})
    r = m.scan(str(tmp_path), 0, {"raw-backfill"})
    assert (r["stalled"], r["killed"], r["runs"], r["measurable"]) == (1, 1, 2, True)
    assert r["stalled_lanes"] == {"raw-backfill": 1}
    assert r["starved"] == 0, "a hard-timeout kill is NOT a denied slot"


def test_a_kill_that_executed_work_is_an_overrun_not_starvation(tmp_path):
    """The discriminator is turns. Without it every genuine overrun is misfiled as capacity --
    the same error as the original bug, pointing the other way."""
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "regrade", "status": "failed", "error": KILLED,
                            "executed_tool_call_turns": 6}]})
    r = m.scan(str(tmp_path), 0, {"regrade"})
    assert (r["stalled"], r["starved"], r["killed"]) == (0, 0, 1)


def test_a_no_agent_lane_cannot_starve(tmp_path):
    """A no_agent lane holds no model slot, so zero turns proves nothing about capacity there."""
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "reshelve", "status": "failed", "error": KILLED,
                            "executed_tool_call_turns": 0}]})
    r = m.scan(str(tmp_path), 0, set())          # not an agent lane
    assert (r["stalled"], r["starved"], r["killed"]) == (0, 0, 1)


def test_a_non_timeout_failure_is_not_a_kill(tmp_path):
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "x", "status": "failed",
                            "error": "ValueError: bad frontmatter",
                            "executed_tool_call_turns": 0}]})
    r = m.scan(str(tmp_path), 0, {"x"})
    assert (r["stalled"], r["starved"], r["killed"], r["runs"]) == (0, 0, 0, 1)


def test_absent_run_dir_is_unmeasurable_not_clean(tmp_path):
    """Absence is UNKNOWN. Returning a clean zero for a directory never read is how a detector
    becomes decoration."""
    m = _mod()
    r = m.scan(str(tmp_path), 0, {"x"})
    assert r["measurable"] is False and r["starved"] == 0 and r["stalled"] == 0


def test_records_older_than_the_cutoff_are_excluded(tmp_path):
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "x", "status": "failed", "error": KILLED,
                            "executed_tool_call_turns": 0}]})
    stale = tmp_path / "cron-plus" / "runs" / "a" / "r0.json"
    os.utime(stale, (1_000_000, 1_000_000))
    import time
    r = m.scan(str(tmp_path), time.time() - 3600, {"x"})
    assert r["measurable"] is False              # nothing in-window at all
    assert r["starved"] == 0 and r["stalled"] == 0


def test_corrupt_records_are_skipped_without_losing_the_good_ones(tmp_path):
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "x", "status": "failed", "error": KILLED,
                            "executed_tool_call_turns": 0}]})
    (tmp_path / "cron-plus" / "runs" / "a" / "bad.json").write_text("{nope", encoding="utf-8")
    r = m.scan(str(tmp_path), 0, {"x"})
    assert r["stalled"] == 1


def test_lane_falls_back_to_the_run_directory_name(tmp_path):
    """Older records predate the `lane` field; the directory is the job id and still identifies
    the lane well enough to report."""
    m = _mod()
    _runs(tmp_path, {"job-abc": [{"status": "failed", "error": KILLED,
                                  "executed_tool_call_turns": 0}]})
    r = m.scan(str(tmp_path), 0, {"job-abc"})
    assert r["stalled_lanes"] == {"job-abc": 1}


def test_agent_lanes_of_excludes_no_agent_and_nameless(tmp_path):
    m = _mod()
    jobs = [{"name": "a"}, {"name": "b", "no_agent": True}, {"no_agent": False}, "junk"]
    assert m.agent_lanes_of(jobs) == {"a"}


def test_counts_accumulate_per_lane_across_runs(tmp_path):
    """The per-lane count IS the product -- "raw-backfill x122" is what names the culprit. A
    counter only ever exercised at 1 could be broken for every value above it and still pass.
    """
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "raw-backfill", "status": "failed", "error": KILLED,
         "executed_tool_call_turns": 0} for _ in range(3)
    ] + [
        {"lane": "entity-backfill", "status": "failed", "error": KILLED,
         "executed_tool_call_turns": 0},
    ]})
    r = m.scan(str(tmp_path), 0, {"raw-backfill", "entity-backfill"})
    assert r["stalled_lanes"] == {"raw-backfill": 3, "entity-backfill": 1}
    assert r["stalled"] == 4


def test_a_stale_record_does_not_abort_the_rest_of_the_scan(tmp_path):
    """Skipping an out-of-window record must CONTINUE, not stop. Aborting on the first stale file
    would silently truncate the count while still reporting a confident-looking number -- the
    detector-that-cannot-fail shape this whole class is about.
    """
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "old", "status": "failed", "error": KILLED, "executed_tool_call_turns": 0},
        {"lane": "fresh", "status": "failed", "error": KILLED, "executed_tool_call_turns": 0},
    ]})
    os.utime(tmp_path / "cron-plus" / "runs" / "a" / "r0.json", (1_000_000, 1_000_000))
    import time
    r = m.scan(str(tmp_path), time.time() - 3600, {"old", "fresh"})
    assert r["stalled_lanes"] == {"fresh": 1}, "the stale record must be skipped, not end the scan"


def test_a_non_timeout_failure_does_not_abort_the_rest_of_the_scan(tmp_path):
    """Same contract for the other skip: one unrelated failure must not hide every later kill."""
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "other", "status": "failed", "error": "ValueError: nope",
         "executed_tool_call_turns": 0},
        {"lane": "starved", "status": "failed", "error": KILLED,
         "executed_tool_call_turns": 0},
    ]})
    r = m.scan(str(tmp_path), 0, {"other", "starved"})
    assert r["stalled_lanes"] == {"starved": 1}
    assert r["killed"] == 1 and r["runs"] == 2


def test_a_truthy_non_true_no_agent_is_still_an_agent_lane(tmp_path):
    """`no_agent: 1` is not `no_agent: True`. The runner tests `is True`, so anything else DOES
    hold a model slot and must stay eligible -- resolving it as truthy here would wrongly exempt
    a lane that can genuinely starve.
    """
    m = _mod()
    assert m.agent_lanes_of([{"name": "a", "no_agent": 1}]) == {"a"}
    assert m.agent_lanes_of([{"name": "a", "no_agent": True}]) == set()


def test_scan_order_is_deterministic(tmp_path):
    """The scan must not depend on filesystem iteration order.

    Without this, a bug that STOPS the scan early rather than skipping one record shows up or
    hides depending on which file the directory happened to yield first -- which is exactly how
    the two continue/break mutants survived a test written to catch them.
    """
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "old", "status": "failed", "error": KILLED, "executed_tool_call_turns": 0},
        {"lane": "fresh", "status": "failed", "error": KILLED, "executed_tool_call_turns": 0},
    ]})
    import glob as _g
    listing = sorted(_g.glob(str(tmp_path / "cron-plus" / "runs" / "*" / "*.json")))
    assert [p.rsplit("/", 1)[-1] for p in listing] == ["r0.json", "r1.json"]
    # r0 (the one aged out below) is therefore visited FIRST, so a scan that breaks instead of
    # continuing loses r1 and the assertion in the stale-record test becomes decisive.
    os.utime(tmp_path / "cron-plus" / "runs" / "a" / "r0.json", (1_000_000, 1_000_000))
    import time
    r = m.scan(str(tmp_path), time.time() - 3600, {"old", "fresh"})
    assert r["stalled_lanes"] == {"fresh": 1}


def test_a_healthy_run_before_a_starved_one_does_not_end_the_scan(tmp_path):
    """The third early-stop path: skipping a non-failed record must CONTINUE.

    This is the ordering that dominates a healthy fleet -- successes vastly outnumber kills, so
    a scan that stopped at the first success would report near-zero starvation forever while
    looking perfectly well-behaved. Ordered deliberately: r0 (succeeded) is visited first.
    """
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "raw-backfill", "status": "succeeded", "executed_tool_call_turns": 3},
        {"lane": "raw-backfill", "status": "failed", "error": KILLED,
         "executed_tool_call_turns": 0},
    ]})
    r = m.scan(str(tmp_path), 0, {"raw-backfill"})
    assert r["stalled"] == 1, "the success must be skipped, not end the scan"
    assert r["runs"] == 2


# --- starvation vs stall: the distinction the detector exists to make ---------

def test_a_denied_slot_is_starvation_not_a_stall(tmp_path):
    """`model_slot` names this failure explicitly. It is the ONLY reliable starvation signal:
    once the slot wait is capped below the run's deadline, a starved run can no longer reach
    the hard timeout, so it always reports itself this way."""
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "raw-backfill", "status": "failed", "error": DENIED,
                            "executed_tool_call_turns": 0}]})
    r = m.scan(str(tmp_path), 0, {"raw-backfill"})
    assert (r["starved"], r["stalled"], r["killed"]) == (1, 0, 0)
    assert r["lanes"] == {"raw-backfill": 1} and r["stalled_lanes"] == {}


def test_a_hard_timeout_with_no_turns_is_a_stall_not_starvation(tmp_path):
    """The run HELD a slot and returned nothing usable. Reporting it as starvation tells the
    operator to raise concurrency, which cannot help — measured live at 0 starvation against
    15 stalls on one deployment under exactly that heading."""
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "broken-wikilinks-drain", "status": "failed", "error": KILLED,
                            "executed_tool_call_turns": 0}]})
    r = m.scan(str(tmp_path), 0, {"broken-wikilinks-drain"})
    assert (r["starved"], r["stalled"]) == (0, 1)


def test_the_two_causes_are_counted_separately_never_summed(tmp_path):
    """They need opposite remedies, so a combined figure points at whichever dominates."""
    m = _mod()
    _runs(tmp_path, {"a": [
        {"lane": "l1", "status": "failed", "error": DENIED, "executed_tool_call_turns": 0},
        {"lane": "l2", "status": "failed", "error": KILLED, "executed_tool_call_turns": 0},
        {"lane": "l3", "status": "failed", "error": KILLED, "executed_tool_call_turns": 4},
    ]})
    r = m.scan(str(tmp_path), 0, {"l1", "l2", "l3"})
    assert r["starved"] == 1 and r["stalled"] == 1 and r["killed"] == 2
    assert r["lanes"] == {"l1": 1} and r["stalled_lanes"] == {"l2": 1}


def test_a_denied_slot_counts_even_for_a_lane_not_listed_as_an_agent_lane(tmp_path):
    """The agent-lane guard exists because zero turns proves nothing on a no_agent lane. A denied
    slot needs no such guard: a lane that never takes a slot cannot be refused one, so the error
    itself is proof enough."""
    m = _mod()
    _runs(tmp_path, {"a": [{"lane": "odd", "status": "failed", "error": DENIED,
                            "executed_tool_call_turns": 0}]})
    r = m.scan(str(tmp_path), 0, set())
    assert r["starved"] == 1


def test_timeout_pressure_flags_success_p99_near_ceiling(tmp_path):
    m = _mod()
    _runs(tmp_path, {"lane": [
        {"lane": "regrade", "status": "succeeded", "duration_seconds": seconds}
        for seconds in ([500] * 19 + [890])
    ]})
    pressure = m.timeout_pressure(str(tmp_path), 0, [{"name": "regrade", "timeout": 900}])
    assert pressure["regrade"]["p99"] == 890
    assert pressure["regrade"]["ratio"] > .98


def test_timeout_pressure_requires_enough_evidence(tmp_path):
    m = _mod()
    _runs(tmp_path, {"lane": [
        {"lane": "regrade", "status": "succeeded", "duration_seconds": 899}
        for _ in range(19)
    ]})
    assert m.timeout_pressure(str(tmp_path), 0, [{"name": "regrade", "timeout": 900}]) == {}


def test_timeout_pressure_timestamp_fallback_and_skip_edges(tmp_path):
    m = _mod()
    records = [
        {"lane": "calm", "status": "succeeded", "duration_seconds": 100}
        for _ in range(20)
    ] + [
        {"lane": "calm", "status": "failed", "duration_seconds": 899},
        {"lane": "other", "status": "succeeded", "duration_seconds": 899},
        {"lane": "calm", "status": "succeeded", "started_at": "bad"},
        {"lane": "calm", "status": "succeeded", "started_at": "2026-08-26T00:00:00Z",
         "finished_at": "2026-08-26T00:01:40Z"},
    ]
    _runs(tmp_path, {"lane": records})
    bad = tmp_path / "cron-plus/runs/lane/bad.json"
    bad.write_text("{bad")
    assert m.timeout_pressure(str(tmp_path), 0, [{"name": "calm", "timeout": 900}]) == {}
    os.utime(tmp_path / "cron-plus/runs/lane/r0.json", (1, 1))
    assert m.timeout_pressure(str(tmp_path), 2, [{"name": "calm", "timeout": 900}]) == {}
