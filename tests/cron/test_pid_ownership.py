import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).parents[2] / "patches" / "cron-plus" / "pid_ownership.py"
spec = importlib.util.spec_from_file_location("pid_ownership", MODULE)
ownership = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ownership)


def test_owner_removes_its_pid_record(tmp_path):
    path = tmp_path / "job.pid"
    path.write_text(json.dumps({"pid": 101, "started_at": "fixture"}))

    assert ownership.remove_if_owned(path, 101)
    assert not path.exists()


def test_older_runner_preserves_newer_runners_record(tmp_path):
    path = tmp_path / "job.pid"
    path.write_text(json.dumps({"pid": 202, "started_at": "newer"}))

    assert not ownership.remove_if_owned(path, 101)
    assert ownership.recorded_pid(path) == 202


def test_legacy_plain_pid_records_remain_supported(tmp_path):
    path = tmp_path / "job.pid"
    path.write_text("303")

    assert ownership.recorded_pid(path) == 303
    assert ownership.remove_if_owned(path, 303)


def test_missing_or_malformed_record_is_never_unlinked_as_owned(tmp_path):
    missing = tmp_path / "missing.pid"
    assert not ownership.remove_if_owned(missing, 404)
    malformed = tmp_path / "malformed.pid"
    malformed.write_text("not-a-pid")
    assert not ownership.remove_if_owned(malformed, 404)
    assert malformed.exists()
