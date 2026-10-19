"""okengine#994 [24]: the suite refuses to run against another checkout's ``okengine``.

~/.venvs/okengine carries an editable okengine pointing at whichever engines/ worktree last ran
``pip install -e``; a release worktree's gates silently tested that other source. tests/conftest.py
now fails the session (pytest UsageError) unless okengine resolves to this checkout's ``src/`` or a
regular installed distribution (CI installs the candidate wheel into site-packages).
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _guard():
    spec = importlib.util.spec_from_file_location("okengine_conftest_994", REPO / "tests" / "conftest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.okengine_source_problem


def test_this_checkouts_src_is_accepted():
    problem = _guard()
    assert problem(str(REPO / "src" / "okengine" / "__init__.py"), REPO) is None


@pytest.mark.parametrize("installed", ["site-packages", "dist-packages"])
def test_an_installed_distribution_is_accepted(tmp_path, installed):
    origin = tmp_path / "lib" / "python3.12" / installed / "okengine" / "__init__.py"
    assert _guard()(str(origin), REPO) is None


def test_an_unimportable_package_is_not_this_guards_finding():
    assert _guard()(None, REPO) is None


def test_another_worktrees_source_is_rejected_with_the_fix(tmp_path):
    other = tmp_path / "engines" / "okengine-980" / "src" / "okengine" / "__init__.py"
    message = _guard()(str(other), REPO)
    assert message is not None
    assert str(other.parent) in message and str(REPO / "src") in message
    assert "PYTHONPATH=src:okengine-mcp" in message


def test_a_root_without_its_own_okengine_source_has_nothing_to_guard(tmp_path):
    """A disposable fixture that copies this conftest (tests/ci/test_test_tmpfs.py) has no
    src/okengine, so whatever okengine resolves to cannot be shadowing it."""
    other = tmp_path / "engines" / "okengine-980" / "src" / "okengine" / "__init__.py"
    assert _guard()(str(other), tmp_path / "population") is None


def test_a_sibling_whose_name_extends_src_is_rejected(tmp_path):
    """Path containment, not a string prefix: <repo>/src-old is not <repo>/src."""
    assert _guard()(str(REPO / "src-old" / "okengine" / "__init__.py"), REPO) is not None


def _collect(pythonpath: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=pythonpath)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "--import-mode=importlib", str(Path(__file__))],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


def test_a_session_resolving_another_checkout_fails_loudly(tmp_path):
    """Negative fixture: the real conftest, a real pytest session, okengine shadowed by a stray
    package earlier on the path. The session must refuse to start."""
    fake = tmp_path / "other-worktree" / "src" / "okengine"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("")
    result = _collect(f"{fake.parent}{os.pathsep}{REPO / 'okengine-mcp'}")
    out = result.stdout + result.stderr
    assert result.returncode == 4, out              # pytest's usage-error exit
    assert "not this checkout's" in out and "PYTHONPATH=src:okengine-mcp" in out, out


def test_a_session_resolving_this_checkout_starts(tmp_path):
    result = _collect(f"{REPO / 'src'}{os.pathsep}{REPO / 'okengine-mcp'}")
    assert result.returncode == 0, result.stdout + result.stderr
