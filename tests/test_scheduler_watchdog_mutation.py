"""Behavioral contracts for scripts/scheduler_watchdog.py that its critical mutation campaign
showed were unpinned (okengine#612): exact tick-age arithmetic and boundaries, run-record
status filtering, the recovery subprocess contract, CLI defaults and exit codes, and the
evidence file format."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import create_autospec

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/scheduler_watchdog.py"


def _load(name: str = "scheduler_watchdog_mutation"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load()


def _deployment(tmp_path: Path, mtime: float | None = None) -> Path:
    cron = tmp_path / ".hermes-data/cron-plus"
    cron.mkdir(parents=True)
    tick = cron / ".tick.lock"
    tick.write_text("")
    if mtime is not None:
        os.utime(tick, (mtime, mtime))
    return tmp_path


# --- tick age ----------------------------------------------------------------------------------

def test_tick_age_is_now_minus_mtime_even_for_an_ancient_tick(tmp_path):
    dep = _deployment(tmp_path, mtime=1000)
    now = 1_700_000_000.0
    check = MODULE.inspect(dep, now, 180)
    assert check["tick_age_seconds"] == now - 1000
    assert check["healthy"] is False


def test_a_tick_stamped_exactly_now_has_zero_age(tmp_path):
    dep = _deployment(tmp_path, mtime=1_700_000_000)
    assert MODULE.inspect(dep, 1_700_000_000.0, 180)["tick_age_seconds"] == 0.0


def test_a_future_tick_clamps_to_zero_age(tmp_path):
    dep = _deployment(tmp_path, mtime=1_700_000_010)
    assert MODULE.inspect(dep, 1_700_000_000.0, 180)["tick_age_seconds"] == 0.0


def test_a_tick_exactly_at_the_age_limit_is_healthy(tmp_path):
    dep = _deployment(tmp_path, mtime=1_700_000_000)
    check = MODULE.inspect(dep, 1_700_000_180.0, 180)
    assert check["tick_age_seconds"] == 180
    assert check["healthy"] is True, check["reasons"]


# --- run records -------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["succeeded", "stopped", "timeout"])
def test_terminal_statuses_sorting_after_running_are_not_running_records(tmp_path, status):
    dep = _deployment(tmp_path, mtime=time.time())
    path = dep / ".hermes-data/cron-plus/runs/lane/done.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"status": status, "started_at": "2020-01-01T00:00:00Z"}))
    check = MODULE.inspect(dep, time.time(), 180)
    assert check["running_records"] == []
    assert check["healthy"] is True


def test_a_malformed_record_does_not_hide_running_records_in_other_lanes(tmp_path):
    dep = _deployment(tmp_path, mtime=time.time())
    lanes = ("alpha", "beta", "gamma", "delta")
    for lane in lanes:
        directory = dep / f".hermes-data/cron-plus/runs/{lane}"
        directory.mkdir(parents=True)
        (directory / "broken.json").write_text("{")
        (directory / "live.json").write_text(json.dumps({
            "status": "running", "lane": lane,
            "started_at": MODULE.datetime.now(MODULE.timezone.utc).isoformat()}))
    check = MODULE.inspect(dep, time.time(), 180)
    assert sorted(item["lane"] for item in check["running_records"]) == sorted(lanes)


# --- restart -----------------------------------------------------------------------------------

def test_restart_runs_a_bounded_text_capture_and_reports_exact_elapsed_and_stderr_tail(
        tmp_path, monkeypatch):
    stderr = "a" * 500 + "b" * 1000
    run = create_autospec(subprocess.run, return_value=subprocess.CompletedProcess(
        [], 0, "", stderr))
    clock = create_autospec(time.monotonic, side_effect=[100.0, 250.1234])
    monkeypatch.setattr(MODULE.subprocess, "run", run)
    monkeypatch.setattr(MODULE.time, "monotonic", clock)
    result = MODULE.restart(tmp_path)
    assert run.call_args.args[0] == [
        "docker", "compose", "up", "-d", "--no-deps", "--force-recreate", "gateway"]
    assert run.call_args.kwargs == {
        "cwd": tmp_path, "capture_output": True, "text": True, "timeout": 180}
    assert result["elapsed_seconds"] == 150.123
    assert result["stderr"] == "b" * 1000


# --- main --------------------------------------------------------------------------------------

def test_main_requires_an_evidence_path(tmp_path):
    with pytest.raises(SystemExit) as raised:
        MODULE.main([str(tmp_path)])
    assert raised.value.code == 2


def test_healthy_main_exits_zero_with_default_limits_and_versioned_evidence(tmp_path, capsys):
    dep = _deployment(tmp_path / "dep", mtime=time.time())
    evidence = tmp_path / "nested/monitor/evidence.json"
    assert MODULE.main([str(dep), "--evidence", str(evidence)]) == 0
    text = evidence.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["schema_version"] == 1
    assert payload["max_tick_age_seconds"] == 180
    assert payload["healthy"] is True
    assert text.startswith('{\n  "schema_version": 1,\n  "observed_at": ')
    assert text.endswith("}\n")
    printed = capsys.readouterr().out
    assert printed.startswith('{"deployments": [')
    assert json.loads(printed) == payload


def test_the_cli_runs_main_when_executed_as_a_script(tmp_path):
    evidence = tmp_path / "evidence.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "dead"), "--evidence", str(evidence)],
        capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 1, completed.stderr
    assert json.loads(evidence.read_text())["healthy"] is False


def test_importing_under_any_module_name_does_not_run_the_cli():
    module = _load("Watchdog")
    assert callable(module.main)
