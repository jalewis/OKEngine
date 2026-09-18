"""Stable-name cron-plus adapter contract (okengine#406)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import pytest
from types import SimpleNamespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("framework_jobs", REPO / "scripts/framework_jobs.py")
jobs = importlib.util.module_from_spec(spec)
sys.modules["framework_jobs"] = jobs
spec.loader.exec_module(jobs)


def _deployment(tmp_path: Path, values=None) -> Path:
    dep = tmp_path / "pack"
    (dep / "wiki").mkdir(parents=True)
    (dep / ".hermes-data/cron-plus").mkdir(parents=True)
    (dep / "docker-compose.yml").write_text("services: {}\n")
    values = values if values is not None else [
        {"id": "abc123hash", "name": "source-refresh", "enabled": True,
         "schedule": {"kind": "cron", "expr": "0 * * * *"}},
        {"id": "def456hash", "name": "actor-review", "enabled": False,
         "schedule": {"kind": "cron", "expr": "0 1 * * *"}},
    ]
    (dep / ".hermes-data/cron-plus/jobs.json").write_text(json.dumps({"jobs": values}))
    return dep


def _call(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = jobs.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_list_and_inspect_are_name_based_and_hide_runtime_ids(tmp_path):
    dep = _deployment(tmp_path)
    code, out, err = _call(["list", str(dep), "--json"])
    assert code == 0 and not err
    assert [item["name"] for item in json.loads(out)["jobs"]] == ["actor-review", "source-refresh"]
    assert "abc123hash" not in out and '"id"' not in out

    code, out, err = _call(["inspect", str(dep), "source-refresh", "--json"])
    assert code == 0 and not err and json.loads(out)["name"] == "source-refresh"
    assert "abc123hash" not in out


def test_duplicate_composed_names_fail_closed(tmp_path):
    dep = _deployment(tmp_path, [
        {"id": "one", "name": "same"}, {"id": "two", "name": "same"},
    ])
    code, _, err = _call(["list", str(dep)])
    assert code == 1
    assert "duplicate composed job name: same" in err


def test_actions_resolve_name_to_hash_only_at_internal_boundary(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    invoked = []
    monkeypatch.setattr(jobs, "_invoke", lambda deployment, action, runtime_id:
                        invoked.append((deployment, action, runtime_id)))

    for action in ("run", "pause", "resume"):
        code, out, err = _call([action, str(dep), "source-refresh", "--json"])
        assert code == 0 and not err
        assert json.loads(out)["job"] == "source-refresh"
        assert "abc123hash" not in out
    assert invoked == [(dep.resolve(), action, "abc123hash")
                       for action in ("run", "pause", "resume")]


def test_unknown_name_never_invokes_cron_plus(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    monkeypatch.setattr(jobs, "_invoke", lambda *_: (_ for _ in ()).throw(AssertionError()))
    code, _, err = _call(["run", str(dep), "missing"])
    assert code == 1 and "job not found: missing" in err


def test_wait_reports_terminal_runtime_state(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    states = iter([
        {"id": "abc123hash", "name": "source-refresh", "last_run_at": None},
        {"id": "abc123hash", "name": "source-refresh",
         "last_run_at": "2026-07-23T01:00:00+00:00", "last_run_success": True},
    ])
    monkeypatch.setattr(jobs, "_job", lambda *_: next(states))
    monkeypatch.setattr(jobs, "_invoke", lambda *_: None)
    code, out, err = _call(["run", str(dep), "source-refresh", "--wait",
                            "--poll-interval", "0", "--json"])
    assert code == 0 and not err
    assert json.loads(out)["completed"] is True
    assert json.loads(out)["success"] is True


def test_deployment_and_live_state_failures(tmp_path, monkeypatch):
    code, _, err = _call(["list", str(tmp_path)])
    assert code == 1 and "not an OKEngine deployment" in err
    dep = _deployment(tmp_path)
    (dep / "docker-compose.yml").unlink()
    code, _, err = _call(["list", str(dep)])
    assert code == 1 and "no docker-compose.yml" in err

    (dep / "docker-compose.yml").write_text("services: {}\n")
    state = dep / jobs.LIVE_JOBS
    state.write_text("{bad")
    code, _, err = _call(["list", str(dep)])
    assert code == 1 and "invalid cron-plus jobs state" in err

    state.unlink()
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(returncode=1, stderr="gateway down", stdout=""))
    code, _, err = _call(["list", str(dep)])
    assert code == 1 and "gateway down" in err
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(returncode=0, stderr="", stdout="{bad"))
    code, _, err = _call(["list", str(dep)])
    assert code == 1 and "gateway returned invalid" in err


def test_job_registry_rejects_bad_shapes(tmp_path):
    for index, (raw, expected) in enumerate([
        ({"wrong": []}, "must contain a jobs list"),
        ([1], "non-object job"),
        ([{"id": "x"}], "stable name and runtime id"),
    ]):
        dep = _deployment(tmp_path / str(index), [])
        (dep / jobs.LIVE_JOBS).write_text(json.dumps(raw))
        code, _, err = _call(["list", str(dep)])
        assert code == 1 and expected in err


def test_human_list_inspect_empty_and_logs(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    code, out, err = _call(["list", str(dep)])
    assert code == 0 and not err
    assert "source-refresh" in out and "0 * * * *" in out and "never" in out
    code, out, err = _call(["inspect", str(dep), "source-refresh"])
    assert code == 0 and "schedule:" in out and "abc123hash" not in out

    empty = _deployment(tmp_path / "empty", [])
    assert _call(["list", str(empty)])[1] == "No jobs configured.\n"
    calls = []
    monkeypatch.setattr(jobs.subprocess, "call", lambda command, env:
                        calls.append((command, env)) or 3)
    assert _call(["logs", str(dep), "source-refresh", "--follow"])[0] == 3
    assert calls[0][0][-1] == "--follow"
    assert calls[0][1]["CRON_PACK_DIR"] == str(dep.resolve())


def test_invoke_failure_wait_timeout_and_human_change(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(returncode=2, stderr="contains hash"))
    code, _, err = _call(["pause", str(dep), "source-refresh"])
    assert code == 1 and "selected job" in err and "abc123hash" not in err

    monkeypatch.setattr(jobs, "_invoke", lambda *_: None)
    times = iter([0, 2])
    monkeypatch.setattr(jobs, "time", SimpleNamespace(
        monotonic=lambda: next(times), sleep=lambda _seconds: None))
    code, _, err = _call(["run", str(dep), "source-refresh", "--wait",
                          "--timeout", "1", "--poll-interval", "0"])
    assert code == 1 and "timed out waiting" in err

    code, out, err = _call(["resume", str(dep), "source-refresh"])
    assert code == 0 and not err and out == "Resume accepted: source-refresh\n"


def test_permission_fallback_success_and_emit_noop(tmp_path, monkeypatch, capsys):
    dep = _deployment(tmp_path)
    original_read_text = jobs.Path.read_text

    def denied(path, *args, **kwargs):
        if path == dep / jobs.LIVE_JOBS:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(jobs.Path, "read_text", denied)
    payload = {"jobs": [{"id": "runtime", "name": "job"}]}
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr=""))
    assert jobs._jobs(dep) == {"job": payload["jobs"][0]}
    jobs._emit({"not": "printed"}, as_json=False)
    assert capsys.readouterr().out == ""


def test_invoke_success_wait_polls_logs_without_follow_and_invalid_timeout(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    monkeypatch.setattr(jobs.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=0))
    jobs._invoke(dep, "pause", "runtime")

    states = iter([
        {"id": "runtime", "name": "source-refresh", "last_run_at": None},
        {"id": "runtime", "name": "source-refresh", "last_run_at": None},
        {"id": "runtime", "name": "source-refresh", "last_run_at": "later"},
    ])
    monkeypatch.setattr(jobs, "_job", lambda *_: next(states))
    monkeypatch.setattr(jobs, "_invoke", lambda *_: None)
    sleeps = []
    monkeypatch.setattr(jobs.time, "sleep", sleeps.append)
    code, out, err = _call([
        "run", str(dep), "source-refresh", "--wait", "--timeout", "10",
        "--poll-interval", "0.25",
    ])
    assert code == 0 and not err and "and completed" in out
    assert sleeps == [0.25]

    calls = []
    monkeypatch.setattr(jobs.subprocess, "call", lambda command, env: calls.append(command) or 0)
    # Restore registry lookup because the state iterator above is exhausted.
    monkeypatch.undo()
    monkeypatch.setattr(jobs.subprocess, "call", lambda command, env: calls.append(command) or 0)
    assert _call(["logs", str(dep), "source-refresh"])[0] == 0
    assert "--follow" not in calls[-1]

    with pytest.raises(SystemExit):
        jobs.main(["run", str(dep), "source-refresh", "--timeout", "0"])
