"""engine_source_guard: refuse to build a deployment from a tree that is not release state.

The incident: a deployment's ENGINE_DIR pointed at a working checkout on a feature branch. A plain
`docker compose build` there would have produced a cockpit missing `_subject_key` -- silently
reverting a fix that was live and serving -- while the build, the container health check and the
rendered page all reported success. Nothing anywhere would have said the deploy went backwards.

So the contract under test is mostly about what must NOT read as buildable, including the case where
the probe itself fails: a guard that treats an unanswerable question as "fine" is the defect it is
meant to catch.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "engine_source_guard.py"

pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="engine_source_guard absent")


def _load():
    spec = importlib.util.spec_from_file_location("engine_source_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine_source_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _repo(tmp_path, branch="main", dirty=False, tag=None):
    root = tmp_path / "tree"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True, text=True)
    run("init", "-q", "-b", branch)
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (root / "f.txt").write_text("one", encoding="utf-8")
    run("add", "-A")
    run("commit", "-qm", "one")
    if tag:
        run("tag", tag)
    if dirty:
        (root / "f.txt").write_text("changed", encoding="utf-8")
    return root


def test_a_clean_main_checkout_is_buildable(tmp_path):
    mod = _load()
    state = mod.git_state(_repo(tmp_path))
    assert state["branch"] == "main" and state["dirty"] is False
    assert mod.evaluate(state) == []


def test_a_feature_branch_is_refused(tmp_path):
    """The actual incident. The tree was eight commits behind main and would have reverted a live fix."""
    mod = _load()
    problems = mod.evaluate(mod.git_state(_repo(tmp_path, branch="fix/something")))
    assert problems and any("not main" in p for p in problems)


def test_an_uncommitted_change_is_refused(tmp_path):
    """An image built from a dirty tree contains code that is in no revision, so the deployment
    cannot be attributed to anything and cannot be reproduced."""
    mod = _load()
    problems = mod.evaluate(mod.git_state(_repo(tmp_path, dirty=True)))
    assert problems and any("uncommitted" in p for p in problems)


def test_an_exact_release_tag_is_buildable_even_detached(tmp_path):
    """Rolling back to a prior release is a legitimate deploy and lands on a detached HEAD. Refusing
    it would make the guard something operators route around, and a routed-around gate is not one."""
    mod = _load()
    root = _repo(tmp_path, branch="main", tag="v9.9.9")
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "v9.9.9"], check=True,
                   capture_output=True)
    state = mod.git_state(root)
    assert state["branch"] is None and state["tag"] == "v9.9.9"
    assert mod.evaluate(state) == []


def test_a_directory_that_is_not_a_checkout_is_refused(tmp_path):
    """`ENGINE_DIR` pointing somewhere unversioned means the image contents belong to no revision."""
    mod = _load()
    plain = tmp_path / "plain"
    plain.mkdir()
    problems = mod.evaluate(mod.git_state(plain))
    assert problems and "not a git checkout" in problems[0]


def test_an_unanswerable_probe_is_refused_not_treated_as_clean(tmp_path):
    """The whole point, stated as its own case. `git status` failing yields dirty=None, which is
    UNKNOWN. A guard that reads None as False would pass the tree it could not inspect -- the same
    silent-success shape the guard exists to stop."""
    mod = _load()
    unknown = {"is_repo": True, "head": "abc", "branch": "main", "dirty": None, "tag": None}
    problems = mod.evaluate(unknown)
    assert problems and any("could not determine" in p for p in problems)


def test_the_allowed_branch_set_is_configurable(tmp_path):
    mod = _load()
    state = mod.git_state(_repo(tmp_path, branch="release"))
    assert mod.evaluate(state) != []
    assert mod.evaluate(state, allowed=("release",)) == []


def test_cli_exits_nonzero_and_explains_the_consequence(tmp_path, capsys):
    mod = _load()
    rc = mod.main([str(_repo(tmp_path, branch="wip"))])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED" in err
    assert "REVERT" in err, "the message must state what silently goes wrong, not just 'wrong branch'"


def test_cli_exits_zero_on_a_clean_main_tree(tmp_path, capsys):
    mod = _load()
    assert mod.main([str(_repo(tmp_path))]) == 0
    assert "OK — buildable" in capsys.readouterr().out


def test_the_override_is_loud_and_still_reports_the_state(tmp_path, capsys, monkeypatch):
    """An escape hatch is necessary (a hotfix branch build during an incident) but it must leave
    evidence in the log; a silent override is indistinguishable from no guard."""
    mod = _load()
    monkeypatch.setenv("OKENGINE_ALLOW_UNRELEASED_BUILD", "1")
    assert mod.main([str(_repo(tmp_path, branch="wip"))]) == 0
    captured = capsys.readouterr()
    assert "OVERRIDDEN" in captured.err and "REFUSED" in captured.err


def test_deploy_runs_the_guard_before_it_builds_anything():
    """A guard nobody calls is not a guard. This pins the WIRING, and the ORDER: it must run before
    the first build, or it reports on a tree whose image has already been replaced."""
    deploy = (REPO / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "engine_source_guard.py" in deploy, "deploy.sh must invoke the guard"
    # Compare INVOCATIONS, not mentions: deploy.sh names build-engine-image.sh in its header
    # comment long before it runs it, and matching that made this test compare against prose.
    guard_at = deploy.index('scripts/engine_source_guard.py"')
    build_at = deploy.index('bash "$ENGINE_DIR/scripts/build-engine-image.sh"')
    assert guard_at < build_at, "the guard must run before the first image build, not after"


def test_the_guard_is_executable():
    import stat
    assert SCRIPT.stat().st_mode & stat.S_IEXEC


def test_git_being_unavailable_reads_as_unknown_and_refuses(tmp_path, monkeypatch, capsys):
    """git absent or unrunnable (no binary, timeout, fork failure) must not read as a clean tree.

    This is the same contract as the dirty=None case, one layer down: `_git` returning None means
    the question could not be answered, and `git_state` must then report is_repo False rather than
    inventing a verdict. A guard whose probe crashed and passed anyway is worse than no guard --
    it puts a green tick on a tree nobody inspected.
    """
    mod = _load()
    real = _repo(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._git(real, "rev-parse", "HEAD") is None
    state = mod.git_state(real)
    assert state["is_repo"] is False
    problems = mod.evaluate(state)
    assert problems and "not a git checkout" in problems[0]
    assert mod.main([str(real)]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_a_git_subprocess_error_is_also_unknown(tmp_path, monkeypatch):
    """subprocess raises SubprocessError (e.g. TimeoutExpired) rather than OSError on a hung git;
    both are 'no answer', and only catching one would leave the other crashing the deploy."""
    mod = _load()
    real = _repo(tmp_path)
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            mod.subprocess.TimeoutExpired(cmd="git", timeout=30)))
    assert mod._git(real, "status") is None
