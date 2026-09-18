"""Pinned cron-plus runner against real patched Hermes v0.21.3 no-agent jobs."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import cron.jobs as cron_jobs
import pytest


CRON_PLUS_SHA = "bdb6cf5f73815b66cf6b33a252a08dc8f78c836d"
ENGINE = Path(__file__).resolve().parents[3]


@pytest.fixture
def _isolated_plugin_imports():
    """Do not leak the pinned plugin's flat module names into later target tests."""
    before = set(sys.modules)
    yield
    plugin = Path(os.environ["OKENGINE_CRON_PLUS_SOURCE"]).resolve()
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name not in before and path and Path(path).resolve().is_relative_to(plugin):
            sys.modules.pop(name, None)


def _run(plugin: Path, target: Path, home: Path, vault: Path, job_id: str):
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(home), "WIKI_PATH": str(vault),
        "CRON_PLUS_DISABLED": "1", "PYTHONPATH": os.pathsep.join((str(target), str(plugin))),
        "OKENGINE_CRON_SCRIPTS": str(ENGINE / "scripts" / "cron"),
    })
    return subprocess.run(
        [sys.executable, str(plugin / "runner.py"), "--job-id", job_id],
        cwd=target, env=env, capture_output=True, text=True, timeout=90, check=False,
    )


def _record(home: Path, job_id: str, stderr: str = "") -> dict:
    records = list((home / "cron-plus" / "runs" / job_id).glob("*.json"))
    assert len(records) == 1, (
        f"expected one durable cron-plus record for {job_id}: {records}; "
        f"runner stderr: {stderr[-1200:]}"
    )
    return json.loads(records[0].read_text(encoding="utf-8"))


def test_pinned_runner_records_real_target_success_and_failure(tmp_path):
    plugin = Path(os.environ["OKENGINE_CRON_PLUS_SOURCE"]).resolve()
    target = Path.cwd().resolve()
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=plugin,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert actual == CRON_PLUS_SHA

    home = tmp_path / "hermes-home"
    store = home / "cron-plus" / "jobs.json"
    store.parent.mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    scripts = home / "scripts"
    scripts.mkdir()
    fail = scripts / "fail.sh"
    fail.write_text("#!/bin/sh\nprintf 'fixture-script-failed\\n'\nexit 7\n", encoding="utf-8")
    fail.chmod(0o700)
    good = scripts / "good.sh"
    good.write_text("#!/bin/sh\nprintf 'fixture-script-succeeded\\n'\n", encoding="utf-8")
    good.chmod(0o700)
    jobs = [
        {"id": "fixture-fail", "name": "fixture-fail", "enabled": True, "no_agent": True,
         "script": str(fail), "schedule": {"kind": "interval", "interval_s": 3600},
         "deliver": "local"},
        {"id": "fixture-good", "name": "fixture-good", "enabled": True, "no_agent": True,
         "script": str(good), "schedule": {"kind": "interval", "interval_s": 3600},
         "deliver": "local"},
    ]
    store.write_text(json.dumps({"jobs": jobs}) + "\n", encoding="utf-8")

    failed = _run(plugin, target, home, vault, "fixture-fail")
    assert failed.returncode == 1, (
        f"failed no-agent target job unexpectedly completed: {failed.stderr[-800:]}"
    )
    saved = json.loads(store.read_text(encoding="utf-8"))["jobs"]
    failed_job = next(job for job in saved if job["id"] == "fixture-fail")
    assert failed_job["last_run_success"] is False
    assert "fixture-script-failed" in str(failed_job["last_error"])
    failed_record = _record(home, "fixture-fail", failed.stderr)
    assert failed_record["status"] == "failed"
    assert "fixture-script-failed" in str(failed_record["error"])
    assert failed_record["executed_tool_call_turns"] == 0

    succeeded = _run(plugin, target, home, vault, "fixture-good")
    assert succeeded.returncode == 0, (
        f"successful no-agent target job failed: {succeeded.stderr[-800:]}"
    )
    saved = json.loads(store.read_text(encoding="utf-8"))["jobs"]
    good_job = next(job for job in saved if job["id"] == "fixture-good")
    assert good_job["last_run_success"] is True
    assert good_job["last_error"] is None
    good_record = _record(home, "fixture-good", succeeded.stderr)
    assert good_record["status"] == "succeeded"
    assert good_record["error"] is None
    outputs = list((home / "cron-plus" / "output" / "fixture-good").glob("*.md"))
    assert len(outputs) == 1 and "fixture-script-succeeded" in outputs[0].read_text(encoding="utf-8")


def test_pinned_runner_persists_failed_advertised_mcp_agent_job(
    tmp_path, monkeypatch, _isolated_plugin_imports,
):
    """The real pinned runner must not turn native registry loss into a clean fire."""
    plugin = Path(os.environ["OKENGINE_CRON_PLUS_SOURCE"]).resolve()
    target = Path.cwd().resolve()
    actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=plugin,
                            capture_output=True, text=True, check=True).stdout.strip()
    assert actual == CRON_PLUS_SHA

    home = tmp_path / "hermes-home"
    vault = tmp_path / "vault"
    home.mkdir()
    (vault / "wiki").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("CRON_PLUS_DISABLED", "1")
    monkeypatch.syspath_prepend(str(plugin))
    (home / "config.yaml").write_text(
        "model:\n  default: local-fixture\ncron:\n  preflight: false\n",
        encoding="utf-8",
    )
    job_id = "fixture-mcp-registry-loss"
    name = "mcp__okengine_read__get_page"
    job = {
        "id": job_id, "name": job_id, "prompt": "disposable prompt",
        "enabled": True, "state": "scheduled", "enabled_toolsets": ["terminal"],
        "schedule": {"kind": "interval", "interval_s": 3600},
        "deliver": "local", "model": None, "provider": None, "base_url": None,
    }
    store = home / "cron-plus" / "jobs.json"
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps({"jobs": [job]}) + "\n", encoding="utf-8")

    # Import the exact installed plugin after setting HERMES_HOME: its jobs
    # module computes the store path at import time. This is a runner contract,
    # not a replacement for the separate real-registry-dispatch fixture.
    spec = importlib.util.spec_from_file_location("_cron_plus_runner_contract", plugin / "runner.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert Path(runner.jobs_mod.__file__).resolve() == plugin / "jobs.py"
    assert runner.jobs_mod.CRON_PLUS_HOME == store.parent
    runtime = {
        "api_key": "disposable-key", "base_url": "http://127.0.0.1:8845/v1",
        "provider": "custom", "api_mode": "chat_completions",
    }
    monkeypatch.setattr(sys, "argv", [str(plugin / "runner.py"), "--job-id", job_id])
    with patch("run_agent.AIAgent", autospec=True) as agent_cls, patch(
        "cron.scheduler._hermes_home", home), patch(
            "cron.scheduler_delivery._resolve_origin", autospec=True,
            return_value=None), patch(
                "hermes_cli.env_loader.load_hermes_dotenv", autospec=True), patch(
                    "hermes_cli.env_loader.reset_secret_source_cache", autospec=True), patch(
                        "hermes_state_registry.acquire", autospec=True,
                        return_value=None), patch(
                            "tools.mcp_tool_discovery.discover_mcp_tools",
                            autospec=True, return_value=[]), patch(
                                "hermes_cli.runtime_provider.resolve_runtime_provider",
                                autospec=True, return_value=runtime):
        agent = agent_cls.return_value
        agent.run_conversation.return_value = {"final_response": "looks successful"}
        agent._okengine_mcp_registry_losses = [name]
        with cron_jobs.use_cron_store(home):
            assert runner.main() == 1
    assert agent_cls.called, "the fixture must reach native agent execution"

    saved = json.loads(store.read_text(encoding="utf-8"))["jobs"]
    assert len(saved) == 1 and saved[0]["last_run_success"] is False
    assert name in str(saved[0]["last_error"])
    record = _record(home, job_id)
    assert record["status"] == "failed"
    assert name in str(record["error"])
