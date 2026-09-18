from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "framework.py"


def _load():
    spec = importlib.util.spec_from_file_location("framework_direct", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_usage_unknown_and_both_dispatch_shapes(monkeypatch, capsys):
    m = _load()
    assert m.main([]) == 2
    assert m.main(["help"]) == 0
    assert "Available commands" in capsys.readouterr().out
    assert m.main(["missing"]) == 2
    assert "unknown command" in capsys.readouterr().err

    fake = type("Fake", (), {
        "main": staticmethod(lambda args: 7),
        "audit": staticmethod(lambda args: 8),
    })
    monkeypatch.setattr(m, "_load", lambda *_a: fake)
    assert m.main(["validate", "x"]) == 7
    assert m.main(["audit", "x"]) == 8


def test_entrypoint_help(monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
