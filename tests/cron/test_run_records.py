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
