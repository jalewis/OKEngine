"""`framework lanes` — the per-lane health view (#483).

Every number this command prints was reconstructed by hand, one `docker exec … grep` at a
time, during the #477/#478 investigation — and the most important one (84% of completion
receipts failing) nobody had at all until someone went looking. Two failure classes
accumulated for weeks in plain sight.

The load-bearing behaviours: write counts come from the RECEIPT, never a log grep (a
refused write is also a "completed" tool call); and the standing thresholds actually fire.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "framework_lanes.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="framework_lanes absent")


def _load():
    spec = importlib.util.spec_from_file_location("framework_lanes", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_lanes"] = m
    spec.loader.exec_module(m)
    return m


def _pack(tmp_path: Path, lane_id="aaa", lane_name="thing-drain"):
    d = tmp_path / "pack" / ".hermes-data" / "cron-plus"
    (d / "receipts" / lane_id).mkdir(parents=True)
    (d / "jobs.json").write_text(json.dumps([{"id": lane_id, "name": lane_name}]))
    return tmp_path / "pack", d / "receipts" / lane_id


def _receipt(dirp: Path, stamp: str, *, valid: bool, accepted_writes=0, error=None):
    items = [{"key": "k1", "disposition": "accepted",
              "writes": [{"path": f"wiki/e/{i}.md"} for i in range(accepted_writes)]}]
    (dirp / f"{stamp}.json").write_text(json.dumps({
        "valid": valid, "errors": [error] if error else [],
        "receipt": {"items": items}}))


def test_rolls_up_runs_valid_and_writes_per_lane(tmp_path):
    pack, rd = _pack(tmp_path)
    _receipt(rd, "2026-07-26_01", valid=True, accepted_writes=3)
    _receipt(rd, "2026-07-26_02", valid=False, error="missing okengine-receipt JSON block")
    lanes = _load().collect(pack)

    assert lanes["thing-drain"]["runs"] == 2
    assert lanes["thing-drain"]["valid"] == 1
    assert lanes["thing-drain"]["writes"] == 3
    assert lanes["thing-drain"]["errors"]["missing okengine-receipt JSON block"] == 1


def test_write_count_comes_from_the_receipt_not_a_log_grep(tmp_path):
    """A REFUSED write is also a "completed" tool call — grepping logs overstates success.

    Only writes on ACCEPTED items count. A deferred item carries no writes and must not
    inflate the number.
    """
    pack, rd = _pack(tmp_path)
    (rd / "2026-07-26_01.json").write_text(json.dumps({
        "valid": True, "errors": [],
        "receipt": {"items": [
            {"key": "k1", "disposition": "accepted", "writes": [{"path": "wiki/e/a.md"}]},
            {"key": "k2", "disposition": "deferred", "reason": "no write recorded"},
            {"key": "k3", "disposition": "skipped", "writes": [{"path": "wiki/e/nope.md"}]},
        ]}}))
    assert _load().collect(pack)["thing-drain"]["writes"] == 1


def test_threshold_fires_on_a_low_valid_rate(tmp_path):
    pack, rd = _pack(tmp_path)
    for i in range(4):
        _receipt(rd, f"2026-07-26_0{i}", valid=False, error="boom")
    m = _load()
    breaches = m.check(m.collect(pack))
    assert any("receipt-valid rate" in b for b in breaches), breaches


def test_threshold_fires_on_a_zero_write_streak(tmp_path):
    pack, rd = _pack(tmp_path)
    for i in range(5):
        _receipt(rd, f"2026-07-26_0{i}", valid=True, accepted_writes=0)
    m = _load()
    breaches = m.check(m.collect(pack))
    assert any("zero writes" in b for b in breaches), breaches


def test_healthy_lane_trips_nothing(tmp_path):
    pack, rd = _pack(tmp_path)
    for i in range(5):
        _receipt(rd, f"2026-07-26_0{i}", valid=True, accepted_writes=2)
    m = _load()
    assert m.check(m.collect(pack)) == []


def test_log_health_surfaces_provider_termination_context_and_compression(tmp_path):
    pack, rd = _pack(tmp_path)
    _receipt(rd, "2026-07-26_01", valid=True, accepted_writes=1)
    logs = pack / ".hermes-data" / "logs" / "cron-plus"
    logs.mkdir(parents=True)
    (logs / "thing-drain-20260726-010000.log").write_text(
        "INFO agent_init provider=local base_url=http://qwen/v1 model=qwen-coder\n"
        "INFO API call #1: model=qwen-coder provider=local in=54000 out=1200\n"
        "INFO context compression done: session=x messages=25->25\n"
        "INFO Turn ended: reason=text_response(finish_reason=stop) model=qwen-coder "
        "api_calls=1/90 budget=1/90\n"
    )
    row = _load().collect(pack)["thing-drain"]
    log = row["logs"][0]
    assert (log["model"], log["provider"], log["endpoint"]) == (
        "qwen-coder", "local", "http://qwen/v1")
    assert log["termination_reason"] == "text_response(finish_reason=stop)"
    assert log["peak_context"] == 54000 and log["peak_output"] == 1200
    assert log["compressions"] == 1 and log["compression_noops"] == 1
    breaches = _load().check({"thing-drain": row})
    assert any("compression no-op" in value for value in breaches)
    assert any("peak context" in value for value in breaches)


def test_extension_lane_name_maps_to_filesystem_safe_log_name(tmp_path):
    pack, rd = _pack(
        tmp_path, lane_id="ext",
        lane_name="okengine.predictions:prediction-structural-backfill")
    _receipt(rd, "2026-07-26_01", valid=True, accepted_writes=1)
    logs = pack / ".hermes-data" / "logs" / "cron-plus"
    logs.mkdir(parents=True)
    (logs / "okengine.predictions_prediction-structural-backfill-20260726-010000.log").write_text(
        "INFO API call #1: model=qwen-coder provider=local in=100 out=20\n")
    row = _load().collect(pack)["okengine.predictions:prediction-structural-backfill"]
    assert len(row["logs"]) == 1
    assert row["logs"][0]["model"] == "qwen-coder"


def test_completion_ratio_threshold_uses_receipt_counts(tmp_path):
    pack, rd = _pack(tmp_path)
    (rd / "2026-07-26_01.json").write_text(json.dumps({
        "valid": True, "errors": [], "receipt": {"items": []},
        "counts": {"selected": 10, "accepted": 1, "deferred": 9},
    }))
    breaches = _load().check(_load().collect(pack))
    assert any("completion ratio 10%" in value for value in breaches)


def test_check_exit_code_is_nonzero_on_breach(tmp_path, capsys):
    pack, rd = _pack(tmp_path)
    for i in range(4):
        _receipt(rd, f"2026-07-26_0{i}", valid=False, error="boom")
    assert _load().main([str(pack), "--check"]) == 1


def test_missing_deployment_is_an_error_not_an_empty_pass(tmp_path):
    assert _load().main([str(tmp_path / "nope")]) == 2


def test_one_invocation_aggregates_multiple_deployments(tmp_path, capsys):
    first, first_receipts = _pack(tmp_path / "one")
    second, second_receipts = _pack(tmp_path / "two", lane_name="other-drain")
    _receipt(first_receipts, "2026-07-26_01", valid=True, accepted_writes=1)
    _receipt(second_receipts, "2026-07-26_01", valid=True, accepted_writes=1)

    assert _load().main([str(first), str(second), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"pack/thing-drain", "pack/other-drain"}


def test_run_mode_lane_is_visible_from_runner_owned_records(tmp_path):
    pack, _ = _pack(tmp_path)
    jobs = pack / ".hermes-data" / "cron-plus" / "jobs.json"
    payload = json.loads(jobs.read_text())
    items = payload["jobs"] if isinstance(payload, dict) else payload
    items[0]["output_contract"] = {"completion": "run"}
    jobs.write_text(json.dumps(payload))
    runs = pack / ".hermes-data" / "cron-plus" / "runs" / "aaa"
    runs.mkdir(parents=True)
    (runs / "2026-07-28_12-00-00.json").write_text(json.dumps({
        "api": 1, "job_id": "aaa", "lane": "thing-drain",
        "completion": "run", "status": "succeeded",
    }))

    row = _load().collect(pack)["thing-drain"]

    assert row["executions"] == 1
    assert row["execution_successes"] == 1
    assert row["runs"] == 0


def test_latest_no_work_execution_does_not_inherit_an_older_failed_receipt(tmp_path):
    """#498: execution and receipt limits describe the same run, not two timelines."""
    pack, receipts = _pack(tmp_path)
    old_receipt = receipts / "2026-07-28_11-00-00.json"
    old_receipt.write_text(json.dumps({
        "valid": False, "errors": ["missing okengine-receipt JSON block"],
        "receipt": {},
    }))
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    os.utime(old_receipt, (old.timestamp(), old.timestamp()))

    now = datetime.now(timezone.utc)
    runs = pack / ".hermes-data" / "cron-plus" / "runs" / "aaa"
    runs.mkdir(parents=True)
    (runs / "latest.json").write_text(json.dumps({
        "api": 1, "job_id": "aaa", "lane": "thing-drain",
        "completion": "per-selected-item", "status": "succeeded",
        "started_at": now.isoformat(),
        "ended_at": (now + timedelta(seconds=2)).isoformat(),
    }))

    row = _load().collect(pack, limit=1)["thing-drain"]

    assert row["executions"] == 1
    assert row["execution_successes"] == 1
    assert row["runs"] == 0
    assert not row["errors"]


def test_failed_runner_owned_record_trips_threshold(tmp_path):
    pack, _ = _pack(tmp_path)
    runs = pack / ".hermes-data" / "cron-plus" / "runs" / "aaa"
    runs.mkdir(parents=True)
    (runs / "2026-07-28_12-00-00.json").write_text(json.dumps({
        "api": 1, "job_id": "aaa", "lane": "thing-drain",
        "completion": "run", "status": "failed", "error": "startup failed",
    }))

    breaches = _load().check(_load().collect(pack))

    assert any("failed runner-owned execution" in value for value in breaches)


def test_fresh_running_marker_is_visible_but_not_a_failure(tmp_path):
    from datetime import datetime, timezone

    pack, _ = _pack(tmp_path)
    runs = pack / ".hermes-data" / "cron-plus" / "runs" / "aaa"
    runs.mkdir(parents=True)
    (runs / "active.json").write_text(json.dumps({
        "api": 1, "job_id": "aaa", "lane": "thing-drain",
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
    }))

    row = _load().collect(pack)["thing-drain"]

    assert row["execution_running"] == 1
    assert not any("stale/interrupted" in value for value in _load().check({"thing-drain": row}))


def test_registered_as_a_framework_subcommand():
    text = (REPO / "scripts" / "framework.py").read_text()
    assert '"lanes"' in text, "framework lanes is not registered in the CLI dispatch"


def test_lane_io_and_log_failure_edges(tmp_path, monkeypatch):
    m = _load()
    pack = tmp_path / "pack"
    assert m._job_names(pack) == {}
    assert m._jobs(pack) == {}
    jobs = pack / ".hermes-data" / "cron-plus" / "jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text("bad json")
    assert m._job_names(pack) == {}
    assert m._jobs(pack) == {}
    jobs.write_text(json.dumps([{"id": "one", "name": "lane"}]))
    assert m._job_names(pack) == {"one": "lane"}

    log = tmp_path / "run.log"
    assert m.parse_log(log)["termination_reason"] == "unknown"
    log.write_text("agent run failed: RuntimeError: exploded\n")
    assert m.parse_log(log)["termination_reason"] == "RuntimeError: exploded"


def test_collect_filters_and_tolerates_corrupt_run_and_receipt_records(tmp_path):
    m = _load()
    pack, receipts = _pack(tmp_path)
    other = receipts.parent / "other"
    other.mkdir()
    (other / "bad.json").write_text("bad")
    (receipts / "bad.json").write_text("bad")
    runs = pack / ".hermes-data" / "cron-plus" / "runs"
    (runs / "other").mkdir(parents=True)
    (runs / "other" / "skip.json").write_text("bad")
    lane_runs = runs / "aaa"
    lane_runs.mkdir()
    (lane_runs / "bad.json").write_text("bad")
    (lane_runs / "running.json").write_text(json.dumps({
        "status": "running", "started_at": "invalid",
    }))
    (lane_runs / "ended.json").write_text(json.dumps({
        "status": "succeeded", "started_at": "invalid", "ended_at": "invalid",
    }))
    row = m.collect(pack, lane="thing-drain", limit=5)["thing-drain"]
    assert row["executions"] == 2
    assert row["stale_running"] == 1


def test_format_and_remaining_health_thresholds(tmp_path):
    m = _load()
    assert m._fmt({}).startswith("no receipts found")
    healthy = {
        "runs": 1, "valid": 1, "writes": 1, "errors": m.Counter(), "recent": [],
        "executions": 1, "execution_successes": 1, "execution_failures": 0,
        "execution_running": 0, "stale_running": 0,
        "logs": [{"compression_noops": 0, "peak_context": 1, "log": "run.log"}],
    }
    assert "TOTAL" in m._fmt({"lane": healthy})
    assert m.check({"lane": healthy}) == []
    stale = {**healthy, "stale_running": 1}
    assert any("stale/interrupted" in value for value in m.check({"lane": stale}))

    pack, receipts = _pack(tmp_path)
    _receipt(receipts, "one", valid=True, accepted_writes=1)
    assert m.main([str(pack), "--check"]) == 0
