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


def test_budget_status_reports_full_pause_state(tmp_path, monkeypatch, capsys):
    fb = _load("framework_budget_paused", "framework_budget.py")

    class Guard:
        @staticmethod
        def _hermes_home():
            return str(tmp_path)

        @staticmethod
        def _load_state():
            return {
                "paused": True,
                "paused_names": ["raw-backfill", "entity-backfill"],
                "reason": "daily cap",
                "tripped_at": "2026-07-29T12:00:00Z",
            }

    monkeypatch.setattr(fb, "_guard", lambda: Guard)
    assert fb.main(["--status"]) == 0
    output = capsys.readouterr().out
    assert "PAUSED" in output
    assert "2 cost-bearing" in output
    assert "daily cap" in output
    assert "2026-07-29T12:00:00Z" in output
    assert "raw-backfill, entity-backfill" in output
    assert "budget --resume" in output


def test_budget_status_handles_minimal_legacy_pause_state(tmp_path, monkeypatch, capsys):
    fb = _load("framework_budget_legacy_pause", "framework_budget.py")

    class Guard:
        _hermes_home = staticmethod(lambda: str(tmp_path))
        _load_state = staticmethod(lambda: {"paused": True})

    monkeypatch.setattr(fb, "_guard", lambda: Guard)
    assert fb.main(["--status"]) == 0
    output = capsys.readouterr().out
    assert "0 cost-bearing" in output
    assert "reason:" not in output and "tripped_at:" not in output and "paused:" not in output


def test_budget_missing_state_directory_fails_loudly(tmp_path, monkeypatch, capsys):
    fb = _load("framework_budget_missing", "framework_budget.py")

    class Guard:
        @staticmethod
        def _hermes_home():
            return str(tmp_path / "missing")

    monkeypatch.setattr(fb, "_guard", lambda: Guard)
    assert fb.main(["--status"]) == 2
    assert "state dir" in capsys.readouterr().err
