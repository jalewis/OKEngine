"""Contract tests for the executable framework launcher (okengine#403)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
EXECUTABLE = REPO / "bin" / "framework"
PYTHON_ENTRYPOINT = REPO / "scripts" / "framework.py"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def test_launcher_is_executable_and_matches_python_invocation(tmp_path):
    assert os.access(EXECUTABLE, os.X_OK)

    direct = _run([str(EXECUTABLE), "--help"], cwd=tmp_path)
    compatible = _run([sys.executable, str(PYTHON_ENTRYPOINT), "--help"], cwd=tmp_path)

    assert direct.returncode == compatible.returncode == 0
    assert direct.stdout == compatible.stdout
    assert direct.stderr == compatible.stderr == ""
    assert "operations" in direct.stdout


def test_launcher_preserves_usage_errors(tmp_path):
    direct = _run([str(EXECUTABLE), "not-a-command"], cwd=tmp_path)
    compatible = _run(
        [sys.executable, str(PYTHON_ENTRYPOINT), "not-a-command"], cwd=tmp_path
    )

    assert direct.returncode == compatible.returncode == 2
    assert direct.stdout == compatible.stdout == ""
    assert direct.stderr == compatible.stderr


def test_launcher_works_through_path_symlink(tmp_path):
    linked = tmp_path / "framework"
    linked.symlink_to(EXECUTABLE)

    result = _run([str(linked), "--help"], cwd=tmp_path)

    assert result.returncode == 0
    assert "Available commands:" in result.stdout


def test_launcher_dispatches_operations_command(tmp_path):
    result = _run([str(EXECUTABLE), "operations", "--help"], cwd=tmp_path)

    assert result.returncode == 0
    assert "framework operations" in result.stdout
    assert "list,history,inspect,plan,run,status,logs,resume,cancel" in result.stdout
