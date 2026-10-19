import importlib.util
import json
import time
from datetime import datetime, timezone
from pathlib import Path


MODULE = Path(__file__).parents[2] / "patches" / "cron-plus" / "run_records.py"
SPEC = importlib.util.spec_from_file_location("run_records", MODULE)
run_records = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_records)


def test_writes_runner_owned_record_for_run_mode(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    job = {
        "id": "lane-1",
        "name": "orphans-drain",
        "output_contract": {"completion": "run",
                            "required_write_path": "briefings/daily-{date}.md"},
        "_okengine_executed_tool_calls": 2,
        "_okengine_executed_writes": [{"path": "entities/a.md"}],
    }
    current = {"last_run_success": True, "last_error": None}

    path = run_records.write_run_record(tmp_path, job, started, current)
    record = json.loads(path.read_text())

    assert record["status"] == "succeeded"
    assert record["completion"] == "run"
    assert record["executed_tool_call_turns"] == 2
    assert record["writes"] == [{"path": "entities/a.md"}]
    assert record["expected_artifacts"] == ["briefings/daily-2026-07-28.md"]


def test_expected_artifact_uses_deployment_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        started = datetime(2026, 9, 3, 3, tzinfo=timezone.utc)
        job = {"id": "lane-1", "name": "daily", "output_contract": {
            "completion": "run", "required_write_path": "briefings/{date}.md"}}
        record = json.loads(run_records.begin_run_record(tmp_path, job, started).read_text())
        assert record["expected_artifacts"] == ["briefings/2026-09-02.md"]
    finally:
        monkeypatch.undo()
        time.tzset()


def test_begin_marker_makes_interrupted_run_visible(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)

    path = run_records.begin_run_record(
        tmp_path, {"id": "lane-1", "name": "orphans-drain"}, started
    )
    record = json.loads(path.read_text())

    assert record["status"] == "running"
    assert record["started_at"] == "2026-07-28T12:00:00+00:00"
    assert record["ended_at"] is None


def test_deterministic_record_attributes_artifacts_without_model_spend(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    artifact = {
        "path": "/opt/vault/wiki/lint.md", "operation": "create",
        "count": 3, "sha256": "sha256:abc",
    }
    job = {
        "id": "audit", "name": "wiki-health-audit", "no_agent": True,
        "model": "should-not-be-attributed", "provider": "should-not-be-attributed",
        "_okengine_artifacts": [artifact],
    }

    path = run_records.write_run_record(
        tmp_path, job, started, {"last_run_success": True})
    record = json.loads(path.read_text())

    assert record["artifacts"] == [artifact]
    assert record["writes"] == []
    assert record["model"] is None
    assert record["provider"] is None


def test_truthy_non_boolean_no_agent_does_not_hide_model_attribution(tmp_path):
    started = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    path = run_records.begin_run_record(
        tmp_path,
        {"id": "lane", "no_agent": 1, "model": "configured", "provider": "local"},
        started,
    )
    record = json.loads(path.read_text())

    assert record["model"] == "configured"
    assert record["provider"] == "local"


# --- Nothing ever revisited a record left `running` by a hard-killed worker. The gateway
# healthcheck fails on any `running` record older than an hour, so one killed worker made a
# deployment permanently unhealthy: okcti (policy-audit) and okpack-ai-research (entity-backfill,
# raw-backfill) both sat that way for a day, on the same afternoon, until the JSON was hand-edited.


def _running(dirpath: Path, name: str, pid: int, lane: str = "lane-1") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(json.dumps({
        "api": 1, "job_id": "lane-1", "lane": lane, "status": "running",
        "started_at": "2026-09-20T18:46:13+00:00", "ended_at": None, "pid": pid,
    }), encoding="utf-8")
    return p


def _dead_pid() -> int:
    """A pid that is certainly not running: allocate one and reap it."""
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_a_record_whose_worker_died_is_closed_out(tmp_path):
    runs = tmp_path / "runs" / "lane-1"
    stale = _running(runs, "2026-09-20_18-46-13-218512.json", _dead_pid())
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 1
    after = json.loads(stale.read_text())
    assert after["status"] == "failed"
    assert after["ended_at"]
    assert "orphaned" in after["error"] and "never closed out" in after["error"]


def test_a_live_worker_is_left_alone(tmp_path):
    """Closing out a run that is still happening would be a false failure on real work."""
    import os
    runs = tmp_path / "runs" / "lane-1"
    live = _running(runs, "2026-09-21_10-00-00-000000.json", os.getpid())
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 0
    assert json.loads(live.read_text())["status"] == "running"


def test_a_finished_record_is_untouched(tmp_path):
    runs = tmp_path / "runs" / "lane-1"
    runs.mkdir(parents=True)
    done = runs / "done.json"
    done.write_text(json.dumps({"status": "succeeded", "pid": _dead_pid()}), encoding="utf-8")
    before = done.read_text()
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 0
    assert done.read_text() == before


def test_an_unreadable_record_is_not_ours_to_rewrite(tmp_path):
    runs = tmp_path / "runs" / "lane-1"
    runs.mkdir(parents=True)
    broken = runs / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 0
    assert broken.read_text() == "{ not json"


def test_an_uninterpretable_pid_counts_as_alive(tmp_path):
    """Fail safe: a noisy healthcheck beats closing out work that may still be running."""
    runs = tmp_path / "runs" / "lane-1"
    for name, pid in (("a.json", None), ("b.json", "not-an-int"), ("c.json", -1), ("d.json", 0)):
        _running(runs, name, pid)
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 0


def test_starting_a_new_run_reconciles_the_lanes_orphans(tmp_path):
    """The reconciliation runs where the knowledge and the write permission already are."""
    runs = tmp_path / "runs" / "lane-1"
    stale = _running(runs, "2026-09-20_18-46-13-218512.json", _dead_pid())
    path = run_records.begin_run_record(
        tmp_path, {"id": "lane-1", "name": "entity-backfill"},
        datetime(2026, 9, 21, 12, tzinfo=timezone.utc))
    assert json.loads(stale.read_text())["status"] == "failed"
    assert json.loads(path.read_text())["status"] == "running"   # the new run is untouched


def test_reconciling_a_lane_with_no_directory_is_a_no_op(tmp_path):
    assert run_records.reconcile_orphaned_runs(tmp_path, "never-ran") == 0


# okengine#1010: a runner killed from outside (here, by the gateway's MCP connect cleanup) left its
# record `running` until the SAME lane started again — a day for a daily lane, failing the gateway
# healthcheck all that time. The scheduler tick now sweeps every lane.


def _status(path: Path) -> str:
    return json.loads(path.read_text())["status"]


def test_a_tick_sweep_closes_every_lanes_orphan_without_waiting_for_the_lane(tmp_path):
    import os
    runs = tmp_path / "runs"
    daily = _running(runs / "policy-audit", "2026-10-08_09-15-21-000001.json", _dead_pid())
    quarter_hourly = _running(runs / "fleet-health", "2026-10-08_09-15-21-000002.json", _dead_pid())
    live = _running(runs / "entity-link", "2026-10-08_09-15-21-000003.json", os.getpid())

    assert run_records.reconcile_all_orphaned_runs(tmp_path) == 2

    assert _status(daily) == "failed"
    assert _status(quarter_hourly) == "failed"
    assert _status(live) == "running", "a live worker's record must never be closed out"


def test_the_sweep_reads_only_each_lanes_newest_record(tmp_path):
    """Older orphans are the lane-start path's job, so a tick's cost is one read per lane."""
    runs = tmp_path / "runs" / "lane-1"
    older = _running(runs, "2026-10-07_09-00-00-000000.json", _dead_pid())
    newest = _running(runs, "2026-10-08_09-00-00-000000.json", _dead_pid())
    (runs / "2026-10-08_10-00-00-000000.json.tmp").write_text("{}", encoding="utf-8")

    assert run_records.reconcile_all_orphaned_runs(tmp_path) == 1

    assert _status(newest) == "failed"
    assert _status(older) == "running"


def test_the_sweep_skips_a_lane_whose_newest_record_is_closed(tmp_path):
    runs = tmp_path / "runs" / "lane-1"
    _running(runs, "2026-10-07_09-00-00-000000.json", _dead_pid())
    done = runs / "2026-10-08_09-00-00-000000.json"
    done.write_text(json.dumps({"status": "succeeded", "pid": _dead_pid()}), encoding="utf-8")
    before = done.read_text()

    assert run_records.reconcile_all_orphaned_runs(tmp_path) == 0
    assert done.read_text() == before


def test_the_sweep_ignores_a_missing_root_and_non_lane_entries(tmp_path):
    assert run_records.reconcile_all_orphaned_runs(tmp_path) == 0
    runs = tmp_path / "runs"
    (runs / "empty-lane").mkdir(parents=True)
    (runs / "stray.json").write_text(json.dumps({"status": "running", "pid": _dead_pid()}),
                                     encoding="utf-8")

    assert run_records.reconcile_all_orphaned_runs(tmp_path) == 0
    assert _status(runs / "stray.json") == "running"


def test_a_run_that_finishes_during_the_liveness_probe_keeps_its_own_outcome(tmp_path):
    """The tick races the runner. Re-read after the worker is gone, so a run that closed its
    own record between the first read and the probe is never overwritten as failed."""
    from unittest.mock import create_autospec
    path = _running(tmp_path / "runs" / "lane-1", "2026-10-08_09-00-00-000000.json", 4242)

    def finish_then_exit(pid):
        path.write_text(json.dumps({"status": "succeeded", "pid": pid}), encoding="utf-8")
        return False

    probe = create_autospec(run_records._worker_alive, side_effect=finish_then_exit)
    original = run_records._worker_alive
    run_records._worker_alive = probe
    try:
        assert run_records.reconcile_all_orphaned_runs(tmp_path) == 0
        assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 0
    finally:
        run_records._worker_alive = original

    assert _status(path) == "succeeded"
    probe.assert_called_once_with(4242)
