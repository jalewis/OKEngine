"""Behavioral contracts of the cron-plus runner records that the original suite left unpinned.

Each test names the observable contract it defends. They run the real module against real
temporary run directories; the only substitution is `os.kill`, autospecced, where the contract is
about which signal the liveness probe sends (and never sending one to a process group).
"""
import errno
import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

MODULE = Path(__file__).parents[2] / "patches" / "cron-plus" / "run_records.py"
SPEC = importlib.util.spec_from_file_location("run_records_mutation", MODULE)
run_records = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_records)

STARTED = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _record_file(directory: Path, name: str, **fields) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    body = {"api": 1, "job_id": "lane-1", "status": "running", "ended_at": None}
    body.update(fields)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _kill_probe(**kwargs):
    return mock.patch.object(run_records.os, "kill", autospec=True, **kwargs)


# --- the liveness probe -------------------------------------------------------------------------

@pytest.mark.parametrize("pid", [0, -1, -7])
def test_a_non_positive_pid_is_never_signalled(pid):
    """os.kill(0, …) and os.kill(-1, …) address a process GROUP / every process: never probe them."""
    with _kill_probe() as kill:
        assert run_records._worker_alive(pid) is True
    kill.assert_not_called()


def test_the_runners_own_pid_is_alive_without_a_probe():
    with _kill_probe() as kill:
        assert run_records._worker_alive(os.getpid()) is True
    kill.assert_not_called()


def test_pid_one_is_probed_with_the_null_signal():
    """pid 1 is a real process id, not a sentinel: it is probed, and only with signal 0."""
    with _kill_probe(side_effect=ProcessLookupError(errno.ESRCH, "gone")) as kill:
        assert run_records._worker_alive(1) is False
    kill.assert_called_once_with(1, 0)


def test_a_vanished_worker_is_dead_and_probed_with_signal_zero():
    with _kill_probe(side_effect=ProcessLookupError(errno.ESRCH, "gone")) as kill:
        assert run_records._worker_alive(424242) is False
    kill.assert_called_once_with(424242, 0)


def test_a_worker_owned_by_another_user_is_alive():
    with _kill_probe(side_effect=PermissionError(errno.EPERM, "not yours")) as kill:
        assert run_records._worker_alive(424242) is True
    kill.assert_called_once_with(424242, 0)


def test_an_uninterpretable_probe_error_counts_as_alive():
    with _kill_probe(side_effect=OSError(errno.EIO, "odd")) as kill:
        assert run_records._worker_alive(424242) is True
    kill.assert_called_once_with(424242, 0)


def test_a_successful_probe_means_alive():
    with _kill_probe(return_value=None) as kill:
        assert run_records._worker_alive(424242) is True
    kill.assert_called_once_with(424242, 0)


def test_a_dead_worker_with_a_lower_pid_than_the_runner_is_reconciled(tmp_path):
    """Liveness is identity with the runner's pid, not an ordering against it."""
    mine = os.getpid()
    lower = next(p for p in range(mine - 1, 1, -1) if not Path(f"/proc/{p}").exists())
    stale = _record_file(tmp_path / "runs" / "lane-1", "a.json", pid=lower)
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 1
    assert json.loads(stale.read_text())["status"] == "failed"


# --- reconciliation keeps going past records it must skip ----------------------------------------

def test_every_skippable_record_is_skipped_without_abandoning_later_orphans(tmp_path):
    runs = tmp_path / "runs" / "lane-1"
    runs.mkdir(parents=True)
    (runs / "a.json").write_text("{ not json", encoding="utf-8")                 # unreadable
    _record_file(runs, "b.json", status="succeeded", pid=_dead_pid())           # finished
    (runs / "c.json").write_text(json.dumps(["running"]), encoding="utf-8")     # not a record
    _record_file(runs, "d.json", pid=os.getpid())                               # live worker
    dead = _dead_pid()
    orphan = _record_file(runs, "z.json", pid=dead)

    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 1
    after = json.loads(orphan.read_text())
    assert after["status"] == "failed"
    assert f"worker pid {dead} is gone" in after["error"]
    assert json.loads((runs / "d.json").read_text())["status"] == "running"


def test_a_failed_record_with_a_dead_worker_is_left_as_written(tmp_path):
    """Only `running` is reconciled; a closed `failed` record keeps its own end time and error."""
    runs = tmp_path / "runs" / "lane-1"
    failed = _record_file(runs, "a.json", status="failed", pid=_dead_pid(),
                          ended_at="2026-09-01T00:00:00+00:00", error="model timeout")
    before = failed.read_text()
    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 0
    assert failed.read_text() == before


def test_an_unwritable_orphan_is_skipped_and_the_next_one_still_closes(tmp_path):
    runs = tmp_path / "runs" / "lane-1"
    blocked = _record_file(runs, "a.json", pid=_dead_pid())
    (runs / "a.json.tmp").mkdir()          # the atomic temp path is occupied: the write must fail
    closable = _record_file(runs, "b.json", pid=_dead_pid())

    assert run_records.reconcile_orphaned_runs(tmp_path, "lane-1") == 1
    assert json.loads(blocked.read_text())["status"] == "running"
    assert json.loads(closable.read_text())["status"] == "failed"


# --- record shape --------------------------------------------------------------------------------

def test_a_new_marker_carries_the_full_initial_shape(tmp_path):
    job = {"id": "lane-1", "name": "orphans-drain", "output_contract_digest": "sha256:feed",
           "output_contract": {"completion": "run"}, "model": "m", "provider": "p"}
    path = run_records.begin_run_record(tmp_path, job, STARTED)
    record = json.loads(path.read_text())
    assert path.parent == tmp_path / "runs" / "lane-1"
    assert path.name == "2026-07-28_12-00-00-000000.json"
    assert record == {
        "api": 1, "job_id": "lane-1", "lane": "orphans-drain", "completion": "run",
        "contract_digest": "sha256:feed", "started_at": "2026-07-28T12:00:00+00:00",
        "ended_at": None, "status": "running", "error": None, "delivery_error": None,
        "model": "m", "provider": "p", "executed_tool_call_turns": 0, "writes": [],
        "artifacts": [], "expected_artifacts": [], "pid": os.getpid(),
    }


def test_the_record_file_is_two_space_indented_json_with_a_trailing_newline(tmp_path):
    path = run_records.begin_run_record(tmp_path, {"id": "lane-1"}, STARTED)
    text = path.read_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"
    assert not path.with_suffix(".json.tmp").exists()


def test_lane_falls_back_to_the_job_id_when_unnamed(tmp_path):
    record = json.loads(run_records.begin_run_record(
        tmp_path, {"id": "lane-1"}, STARTED).read_text())
    assert record["lane"] == "lane-1"
    assert record["job_id"] == "lane-1"


def test_a_job_without_an_id_records_empty_identity(tmp_path):
    record = json.loads(run_records.begin_run_record(tmp_path, {}, STARTED).read_text())
    assert record["job_id"] == ""
    assert record["lane"] == ""


@pytest.mark.parametrize("required", ["", 7, None, ["briefings/x.md"]])
def test_only_a_non_empty_string_path_is_an_expected_artifact(tmp_path, required):
    job = {"id": "lane-1", "output_contract": {"completion": "run",
                                               "required_write_path": required}}
    record = json.loads(run_records.begin_run_record(tmp_path, job, STARTED).read_text())
    assert record["expected_artifacts"] == []


# --- finishing ----------------------------------------------------------------------------------

@pytest.mark.parametrize("success", [False, None, 1, "true"])
def test_only_a_boolean_true_success_is_succeeded(tmp_path, success):
    path = run_records.begin_run_record(tmp_path, {"id": "lane-1"}, STARTED)
    record = json.loads(run_records.finish_run_record(
        path, {"id": "lane-1"}, {"last_run_success": success}).read_text())
    assert record["status"] == "failed"


def test_a_finish_without_state_is_failed_with_no_spend(tmp_path):
    path = run_records.begin_run_record(tmp_path, {"id": "lane-1"}, STARTED)
    record = json.loads(run_records.finish_run_record(path, {"id": "lane-1"}, None).read_text())
    assert record["status"] == "failed"
    assert record["executed_tool_call_turns"] == 0
    assert record["error"] is None and record["delivery_error"] is None
    assert record["ended_at"]


def test_finish_copies_runner_errors_and_executed_work(tmp_path):
    job = {"id": "lane-1", "_okengine_executed_tool_calls": "3",
           "_okengine_executed_writes": ({"path": "a.md"},), "_okengine_artifacts": None}
    path = run_records.begin_run_record(tmp_path, job, STARTED)
    record = json.loads(run_records.finish_run_record(path, job, {
        "last_run_success": True, "last_error": "e", "last_delivery_error": "d"}).read_text())
    assert record["status"] == "succeeded"
    assert (record["error"], record["delivery_error"]) == ("e", "d")
    assert record["executed_tool_call_turns"] == 3
    assert record["writes"] == [{"path": "a.md"}]
    assert record["artifacts"] == []


def test_finishing_a_missing_marker_writes_a_complete_record(tmp_path):
    path = tmp_path / "runs" / "lane-1" / "lost.json"
    job = {"id": "lane-1", "name": "drain"}
    record = json.loads(run_records.finish_run_record(
        path, job, {"last_run_success": True}).read_text())
    assert record["status"] == "succeeded"
    assert record["lane"] == "drain" and record["api"] == 1
    assert record["started_at"]


def test_finishing_a_corrupt_marker_writes_a_complete_record(tmp_path):
    path = tmp_path / "runs" / "lane-1" / "torn.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status": "runn', encoding="utf-8")
    record = json.loads(run_records.finish_run_record(
        path, {"id": "lane-1"}, {"last_run_success": False, "last_error": "x"}).read_text())
    assert record["status"] == "failed"
    assert record["error"] == "x"
    assert record["job_id"] == "lane-1"
