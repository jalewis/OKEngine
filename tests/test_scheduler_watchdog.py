import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scheduler_watchdog", ROOT / "scripts/scheduler_watchdog.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _deployment(tmp_path, age=0):
    cron = tmp_path / ".hermes-data/cron-plus"
    cron.mkdir(parents=True)
    tick = cron / ".tick.lock"
    tick.write_text("")
    stamp = time.time() - age
    os.utime(tick, (stamp, stamp))
    return tmp_path


def test_age_accepts_naive_utc_timestamp_and_clamps_future_time():
    assert MODULE._age_seconds("2030-01-01T00:00:00", 1_700_000_000) == 0
    one_hour = MODULE.datetime(2026, 1, 1, 1, tzinfo=MODULE.timezone.utc).timestamp()
    assert MODULE._age_seconds("2026-01-01T02:00:00+02:00", one_hour) == 3600


def test_fresh_tick_is_healthy_and_stale_tick_is_not(tmp_path):
    dep = _deployment(tmp_path, age=200)
    stale = MODULE.inspect(dep, time.time(), 180)
    assert stale["healthy"] is False and "stale" in stale["reasons"][0]
    os.utime(dep / ".hermes-data/cron-plus/.tick.lock", None)
    assert MODULE.inspect(dep, time.time(), 180)["healthy"] is True
    assert MODULE.inspect(
        deployment=dep, now=time.time(), max_tick_age=180,
    )["healthy"] is True


def test_stalled_sentinel_fails_even_with_fresh_tick(tmp_path):
    dep = _deployment(tmp_path)
    (dep / ".hermes-data/cron-plus/.scheduler-stalled").write_text("job store unreadable")
    check = MODULE.inspect(dep, time.time(), 180)
    assert check["stalled"] is True and check["healthy"] is False


def test_watchdog_persists_failure_evidence_independently_of_scheduler(tmp_path, capsys):
    dep = tmp_path / "dead"
    evidence = tmp_path / "monitor/evidence.json"
    assert MODULE.main([str(dep), "--evidence", str(evidence)]) == 1
    payload = json.loads(evidence.read_text())
    assert payload["healthy"] is False
    assert payload["watchdog"] == "scheduler-independent-host-process"
    assert payload["max_running_age_seconds"] == 3600
    assert payload["max_corpus_lock_age_seconds"] == 300
    assert json.loads(capsys.readouterr().out)["deployments"][0]["tick_age_seconds"] is None


def test_restart_is_never_implicit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(MODULE, "restart", lambda path: calls.append(path))
    assert MODULE.main([str(tmp_path), "--evidence", str(tmp_path / "result.json")]) == 1
    assert calls == []


def test_gateway_death_preserves_deterministic_and_agent_orphan_evidence(tmp_path):
    dep = _deployment(tmp_path, age=500)
    for lane, completion in (("audit", None), ("judgment", "per-selected-item")):
        target = dep / f".hermes-data/cron-plus/runs/{lane}/one.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({
            "status": "running", "lane": lane, "completion": completion,
            "started_at": "2026-08-26T12:00:00Z"}))
    check = MODULE.inspect(dep, time.time(), 180)
    assert check["healthy"] is False
    assert {(item["lane"], item["kind"]) for item in check["running_records"]} == {
        ("audit", "deterministic"), ("judgment", "agent")}
    assert all(item["stale"] for item in check["running_records"])


def test_malformed_and_completed_run_records_are_ignored(tmp_path):
    dep = _deployment(tmp_path)
    runs = dep / ".hermes-data/cron-plus/runs/lane"
    runs.mkdir(parents=True)
    (runs / "broken.json").write_text("{")
    (runs / "scalar.json").write_text("[]")
    (runs / "done.json").write_text('{"status":"complete"}')
    assert MODULE.inspect(dep, time.time(), 180)["running_records"] == []


def test_fresh_running_record_is_visible_without_making_health_red(tmp_path):
    dep = _deployment(tmp_path)
    path = dep / ".hermes-data/cron-plus/runs/lane/fresh.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "status": "running", "lane": "fresh", "started_at": MODULE.datetime.now(
            MODULE.timezone.utc).isoformat(),
    }))
    check = MODULE.inspect(dep, time.time(), 180, max_running_age=60)
    assert check["healthy"] is True
    assert check["running_records"][0]["stale"] is False


def test_running_record_at_exact_age_limit_is_not_stale(tmp_path):
    dep = _deployment(tmp_path)
    path = dep / ".hermes-data/cron-plus/runs/lane/boundary.json"
    path.parent.mkdir(parents=True)
    now = MODULE.datetime(2026, 1, 1, 1, tzinfo=MODULE.timezone.utc).timestamp()
    path.write_text(json.dumps({
        "status": "running", "started_at": "2026-01-01T00:00:00+00:00",
    }))
    check = MODULE.inspect(dep, now, 180, max_running_age=3600)
    assert check["running_records"][0]["age_seconds"] == 3600
    assert check["running_records"][0]["stale"] is False


def test_timestamp_less_running_record_fails_closed(tmp_path):
    dep = _deployment(tmp_path)
    path = dep / ".hermes-data/cron-plus/runs/lane/unknown.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status":"running","job_id":"unknown"}')
    check = MODULE.inspect(dep, time.time(), 180)
    assert check["healthy"] is False
    assert "unknown lane" not in check["reasons"][0]
    assert "unknown > 3600s" in check["reasons"][0]


def test_stale_corpus_lock_owner_fails_health_with_identity(tmp_path):
    dep = _deployment(tmp_path)
    owner = dep / ".okengine/corpus/lock-owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps({
        "pid": 42, "writer": "write-server", "operation": "upsert",
        "acquired_at": "2026-01-01T00:00:00+00:00",
    }))
    check = MODULE.inspect(dep, time.time(), 180, max_corpus_lock_age=60)
    assert check["healthy"] is False
    assert check["corpus_lock_owner"]["pid"] == 42
    assert check["corpus_lock_owner"]["stale"] is True
    assert "corpus lock owner is stale" in check["reasons"][0]


def test_fresh_corpus_lock_owner_is_visible_but_healthy(tmp_path):
    dep = _deployment(tmp_path)
    owner = dep / ".okengine/corpus/lock-owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps({"acquired_at": MODULE.datetime.now(
        MODULE.timezone.utc).isoformat()}))
    check = MODULE.inspect(dep, time.time(), 180, max_corpus_lock_age=60)
    assert check["healthy"] is True
    assert check["corpus_lock_owner"]["stale"] is False


def test_corpus_lock_owner_at_exact_age_limit_is_not_stale(tmp_path):
    dep = _deployment(tmp_path)
    owner = dep / ".okengine/corpus/lock-owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text('{"acquired_at":"2026-01-01T00:00:00+00:00"}')
    now = MODULE.datetime(2026, 1, 1, 0, 5, tzinfo=MODULE.timezone.utc).timestamp()
    check = MODULE.inspect(dep, now, 180, max_corpus_lock_age=300)
    assert check["corpus_lock_owner"]["age_seconds"] == 300
    assert check["corpus_lock_owner"]["stale"] is False


@pytest.mark.parametrize(("age", "stale"), [(300, False), (301, True)])
def test_default_corpus_lock_age_pins_both_sides_of_300_seconds(tmp_path, age, stale):
    dep = _deployment(tmp_path)
    owner = dep / ".okengine/corpus/lock-owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text('{"acquired_at":"2026-01-01T00:00:00+00:00"}')
    now = MODULE.datetime(2026, 1, 1, tzinfo=MODULE.timezone.utc).timestamp() + age
    check = MODULE.inspect(dep, now, 180)
    assert check["corpus_lock_owner"]["age_seconds"] == age
    assert check["corpus_lock_owner"]["stale"] is stale


@pytest.mark.parametrize("content, reason", [
    ("{", "unreadable"),
    ("[]", "not an object"),
    ('{"acquired_at":"not-a-date"}', "stale"),
])
def test_bad_corpus_lock_owner_evidence_fails_closed(tmp_path, content, reason):
    dep = _deployment(tmp_path)
    owner = dep / ".okengine/corpus/lock-owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(content)
    check = MODULE.inspect(dep, time.time(), 180)
    assert check["healthy"] is False
    assert reason in check["reasons"][0]


def test_explicit_restart_records_subprocess_and_main_recovery(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=3, stderr="restart failed")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    result = MODULE.restart(tmp_path)
    assert result["attempted"] is True and result["returncode"] == 3
    assert calls[0][0][-1] == "gateway" and calls[0][1]["cwd"] == tmp_path

    recovery = []
    monkeypatch.setattr(MODULE, "restart", lambda path: recovery.append(path) or {"returncode": 0})
    evidence = tmp_path / "evidence.json"
    assert MODULE.main([str(tmp_path / "dead"), "--evidence", str(evidence),
                        "--restart"]) == 1
    assert recovery == [(tmp_path / "dead").resolve()]
    assert json.loads(evidence.read_text())["deployments"][0]["recovery"]["returncode"] == 0


def test_successful_restart_terminalizes_stale_records(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, age=500)
    path = dep / ".hermes-data/cron-plus/runs/lane/stale.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "status": "running", "lane": "lane", "started_at": "2026-01-01T00:00:00Z",
    }))
    monkeypatch.setattr(MODULE, "restart", lambda _path: {"returncode": 0})
    evidence = tmp_path / "evidence.json"
    assert MODULE.main([str(dep), "--evidence", str(evidence), "--restart"]) == 1
    terminal = json.loads(path.read_text())
    assert terminal["status"] == "indeterminate"
    assert terminal["ended_at"] and "watchdog replaced" in terminal["error"]
    reconciliation = json.loads(evidence.read_text())["deployments"][0][
        "stale_run_reconciliation"]
    assert reconciliation == {"reconciled": 1, "errors": []}


def test_failed_restart_preserves_stale_running_evidence(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, age=500)
    path = dep / ".hermes-data/cron-plus/runs/lane/stale.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "running", "started_at": None}))
    monkeypatch.setattr(MODULE, "restart", lambda _path: {"returncode": 1})
    assert MODULE.main([
        str(dep), "--evidence", str(tmp_path / "evidence.json"), "--restart",
    ]) == 1
    assert json.loads(path.read_text())["status"] == "running"


def test_signal_terminated_restart_does_not_reconcile_stale_runs(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, age=500)
    path = dep / ".hermes-data/cron-plus/runs/lane/stale.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": "running", "started_at": None}))
    monkeypatch.setattr(MODULE, "restart", lambda _path: {"returncode": -9})
    MODULE.main([str(dep), "--evidence", str(tmp_path / "evidence.json"), "--restart"])
    assert json.loads(path.read_text())["status"] == "running"


def test_reconciliation_skips_changed_record_and_reports_unreadable_one(tmp_path):
    dep = _deployment(tmp_path)
    done = dep / ".hermes-data/cron-plus/runs/lane/done.json"
    broken = dep / ".hermes-data/cron-plus/runs/lane/broken.json"
    done.parent.mkdir(parents=True)
    done.write_text('{"status":"failed"}')
    broken.write_text("{")
    check = {"running_records": [
        {"stale": False, "run_record": str(done.relative_to(dep))},
        {"stale": True, "run_record": str(done.relative_to(dep))},
        {"stale": True, "run_record": str(broken.relative_to(dep))},
    ]}
    result = MODULE.reconcile_stale_runs(dep, check, time.time())
    assert result["reconciled"] == 0
    assert result["errors"] == [
        ".hermes-data/cron-plus/runs/lane/broken.json: JSONDecodeError"]


def test_reconciliation_preserves_terminal_statuses_on_both_sides_of_running(tmp_path):
    dep = _deployment(tmp_path)
    paths = []
    for name, status in (("lower", "failed"), ("higher", "succeeded")):
        path = dep / f".hermes-data/cron-plus/runs/lane/{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"status": status}))
        paths.append(path)
    check = {"running_records": [
        {"stale": True, "run_record": str(path.relative_to(dep))} for path in paths
    ]}
    assert MODULE.reconcile_stale_runs(dep, check, time.time())["reconciled"] == 0
    assert [json.loads(path.read_text())["status"] for path in paths] == ["failed", "succeeded"]
