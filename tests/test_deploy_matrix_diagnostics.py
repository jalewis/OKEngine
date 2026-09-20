"""Keep actionable co-install failures in runner logs, not just the last line."""
import subprocess
from unittest.mock import create_autospec

import pytest

from scripts import deploy_matrix as matrix


@pytest.mark.parametrize("code", [0, 1])
def test_install_keeps_full_failure_diagnostics(tmp_path, monkeypatch, capsys, code):
    result = subprocess.CompletedProcess([], code, "first detail\nlast output", "root cause\nrollback")
    framework = create_autospec(matrix.framework, return_value=result)
    monkeypatch.setattr(matrix, "framework", framework)
    host, pack = tmp_path / "host", tmp_path / "guest"
    assert matrix._install(host, pack, "taxonomy", ["subtree", "taxonomy"]) is result
    framework.assert_called_once_with("install-domain", str(host), str(pack), "--apply",
                                     "--shape", "taxonomy")
    output = capsys.readouterr()
    if code:
        assert "guest (taxonomy), exit 1" in output.out
        assert result.stdout in output.out
        assert result.stderr in output.err
    else:
        assert output.out == output.err == ""
