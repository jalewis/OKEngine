"""framework budget subcommand — dispatch + manual resume (okengine#97)."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_framework_registers_budget():
    fw = _load("framework", "framework.py")
    assert "budget" in fw._COMMANDS, "framework should dispatch the 'budget' subcommand"


def test_budget_requires_a_mode():
    fb = _load("framework_budget", "framework_budget.py")
    with pytest.raises(SystemExit):          # mutually-exclusive group is required
        fb.main([])


def test_budget_resume_dispatches_to_guard(tmp_path, monkeypatch, capsys):
    """`framework budget --resume` runs the guard's resume path; with no active trip
    it's a clean no-op (exit 0) rather than the old 'unknown command' error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))   # no state file -> not paused
    fb = _load("framework_budget", "framework_budget.py")
    assert fb.main(["--resume"]) == 0
    assert "nothing to resume" in capsys.readouterr().out


def test_budget_status_reports_not_paused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fb = _load("framework_budget", "framework_budget.py")
    assert fb.main(["--status"]) == 0
    assert "not paused" in capsys.readouterr().out
