import importlib.util
import os
import subprocess
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[2] / "ci" / "mutation_timeout.py"


def load():
    spec = importlib.util.spec_from_file_location("mutation_timeout_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Process:
    pid = 42
    returncode = 7

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    def communicate(self, timeout=None):
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_success_forwards_output_and_returncode(monkeypatch, capsys):
    m = load()
    process = Process([("stdout", "stderr")])
    invocation = {}

    def popen(*args, **kwargs):
        invocation.update(kwargs)
        return process

    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setenv("PYTHONPATH", "/installed/editable")
    monkeypatch.setattr(m.Path, "cwd", lambda: Path("/worker/checkout"))
    assert m.main(["--seconds", "1", "--", "command"]) == 7
    captured = capsys.readouterr()
    assert captured.out == "stdout" and captured.err == "stderr"
    assert invocation["env"]["PYTHONPATH"].split(os.pathsep) == [
        "/worker/checkout/src",
        "/worker/checkout/scripts/cron",
        "/worker/checkout/okengine-mcp",
        "/worker/checkout",
        "/installed/editable",
    ]


def test_checkout_environment_needs_no_inherited_pythonpath(monkeypatch, tmp_path):
    m = load()
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert m.checkout_environment(tmp_path)["PYTHONPATH"].split(os.pathsep) == [
        str(tmp_path / "src"),
        str(tmp_path / "scripts" / "cron"),
        str(tmp_path / "okengine-mcp"),
        str(tmp_path),
    ]


def test_timeout_terminates_group_and_returns_reserved_code(monkeypatch, capsys):
    m = load()
    timeout = subprocess.TimeoutExpired("command", 1)
    process = Process([timeout, ("partial-out", "partial-err")])
    monkeypatch.setattr(m.subprocess, "Popen", lambda *args, **kwargs: process)
    signals = []
    monkeypatch.setattr(m.os, "killpg", lambda pid, signal: signals.append((pid, signal)))
    assert m.main(["--seconds", "1", "command"]) == 124
    assert signals == [(42, m.signal.SIGTERM)]
    captured = capsys.readouterr()
    assert "OKENGINE_MUTATION_TIMEOUT" in captured.err
    assert "partial-out" in captured.out and "partial-err" in captured.err


def test_unresponsive_process_is_killed_after_term_grace(monkeypatch):
    m = load()
    timeout = subprocess.TimeoutExpired("command", 1)
    process = Process([timeout, timeout, ("", "")])
    monkeypatch.setattr(m.subprocess, "Popen", lambda *args, **kwargs: process)
    signals = []
    monkeypatch.setattr(m.os, "killpg", lambda pid, signal: signals.append(signal))
    assert m.main(["--seconds", "2", "--", "command"]) == 124
    assert signals == [m.signal.SIGTERM, m.signal.SIGKILL]


@pytest.mark.parametrize("lookup_on", ["term", "kill"])
def test_already_exited_process_group_is_tolerated(monkeypatch, lookup_on):
    m = load()
    timeout = subprocess.TimeoutExpired("command", 1)
    outcomes = [timeout, ("", "")] if lookup_on == "term" else [timeout, timeout, ("", "")]
    process = Process(outcomes)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *args, **kwargs: process)
    calls = 0

    def killpg(pid, sig):
        nonlocal calls
        calls += 1
        if lookup_on == "term" or calls == 2:
            raise ProcessLookupError

    monkeypatch.setattr(m.os, "killpg", killpg)
    assert m.main(["--seconds", "1", "command"]) == 124


@pytest.mark.parametrize("argv", [
    ["--seconds", "0", "command"],
    ["--seconds", "1"],
])
def test_invalid_budget_or_missing_command_is_usage_error(argv):
    with pytest.raises(SystemExit) as exc:
        load().main(argv)
    assert exc.value.code == 2
