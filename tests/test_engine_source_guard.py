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
from unittest.mock import create_autospec

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


def test_source_subdirectory_cannot_pin_the_parent_revision(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    child = engine / "plain"
    child.mkdir()
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("KEEP=original\n")
    assert mod.git_state(engine)["dirty"] is False
    assert mod.git_state(child)["is_repo"] is False
    assert mod.main([str(child), "--pack", str(pack)]) == 1
    assert env.read_text() == "KEEP=original\n"


def test_unknown_top_level_probe_refuses_source_without_rewriting_config(tmp_path, monkeypatch):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("KEEP=original\n")
    real_run = subprocess.run

    def unavailable_root(command, *args, **kwargs):
        if command[-2:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 1, "", "root unavailable")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run",
                        create_autospec(subprocess.run, side_effect=unavailable_root))
    assert mod.git_state(engine)["is_repo"] is False
    assert mod.main([str(engine), "--pack", str(pack)]) == 1
    assert env.read_text() == "KEEP=original\n"


def test_linked_release_worktree_is_a_valid_source_root(tmp_path):
    mod = _load()
    engine = _repo(tmp_path, tag="v9.9.9")
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(engine), "worktree", "add", "--detach",
                    str(linked), "v9.9.9"], check=True, capture_output=True)
    assert (linked / ".git").is_file()
    assert mod.evaluate(mod.git_state(linked)) == []


def test_canonical_source_alias_is_buildable(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(engine, target_is_directory=True)
    assert mod.evaluate(mod.git_state(alias)) == []


@pytest.mark.parametrize("source_name,foreign_name", [("a", "z"), ("z", "a")])
def test_inherited_git_context_cannot_attribute_another_source_root(
        tmp_path, monkeypatch, source_name, foreign_name):
    mod = _load()
    source_parent = tmp_path / source_name
    foreign_parent = tmp_path / foreign_name
    source_parent.mkdir()
    foreign_parent.mkdir()
    source = _repo(source_parent)
    foreign = _repo(foreign_parent)
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    assert mod.git_state(source)["is_repo"] is False
    assert mod.main([str(source)]) == 1


def test_malformed_git_worktree_metadata_is_not_a_verified_source(tmp_path, monkeypatch):
    mod = _load()
    engine = _repo(tmp_path)
    real_run = subprocess.run

    def malformed(command, *args, **kwargs):
        if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(command, 0, "unexpected\n", "")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run",
                        create_autospec(subprocess.run, side_effect=malformed))
    assert mod.git_state(engine)["is_repo"] is False
    assert mod.main([str(engine)]) == 1


def test_unknown_source_revision_refuses_validation_and_config_writes(tmp_path, monkeypatch):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("KEEP=original\n")
    real_run = subprocess.run

    def unavailable_head(command, *args, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 1, "", "revision unavailable")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run",
                        create_autospec(subprocess.run, side_effect=unavailable_head))
    assert mod.git_state(engine)["head"] is None
    assert mod.main([str(engine)]) == 1
    assert mod.main([str(engine), "--pack", str(pack)]) == 1
    assert env.read_text() == "KEEP=original\n"


@pytest.mark.parametrize("expected", ["0" * 40, "f" * 40])
def test_any_different_qualified_revision_is_refused_without_rewriting_config(tmp_path, expected):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("KEEP=original\n")
    assert mod.git_state(engine)["head"] != expected
    assert mod.main([str(engine), "--pack", str(pack), "--expected-sha", expected]) == 1
    assert env.read_text() == "KEEP=original\n"


@pytest.mark.parametrize("old_revision", ["0" * 40, "f" * 40])
def test_repaired_source_pin_reports_change_then_current_state(tmp_path, capsys, old_revision):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text(f"ENGINE_DIR='{engine.resolve()}'\nOKENGINE_SOURCE_SHA={old_revision}\n")
    assert mod.main([str(engine), "--pack", str(pack)]) == 0
    assert "deployment build source pinned" in capsys.readouterr().out
    assert f"OKENGINE_SOURCE_SHA={mod.git_state(engine)['head']}" in env.read_text()
    assert mod.main([str(engine), "--pack", str(pack)]) == 0
    assert "deployment build source current" in capsys.readouterr().out


def test_source_pin_updates_preserve_operator_comment_associations(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("# engine context\nENGINE_DIR=/old\n# operator compose\nCOMPOSE_FILE=operator.yml\n")
    assert mod.main([str(engine), "--pack", str(pack)]) == 0
    assert env.read_text().splitlines()[:4] == [
        "# engine context", f"ENGINE_DIR='{engine.resolve()}'",
        "# operator compose", "COMPOSE_FILE=operator.yml"]


def test_a_known_unreleased_revision_cannot_be_pinned_directly(tmp_path):
    mod = _load()
    engine = _repo(tmp_path, branch="wip")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("KEEP=original\n")
    with pytest.raises(ValueError, match="unverified"):
        mod.pin_deployment_source(pack, engine, mod.git_state(engine))
    assert env.read_text() == "KEEP=original\n"


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


def test_pin_repairs_obsolete_context_preserves_secrets_and_is_idempotent(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    env = pack / ".env"
    env.write_text("# keep comment\nSECRET=private=value\nENGINE_DIR=/obsolete\nENGINE_DIR=/duplicate\nCOMPOSE_FILE=docker-compose.yml:operator.yml\n")
    assert mod.main([str(engine), "--pack", str(pack)]) == 0
    text = env.read_text()
    assert text.count("ENGINE_DIR=") == 1
    assert f"ENGINE_DIR='{engine.resolve()}'" in text
    assert "SECRET=private=value" in text and "operator.yml" in text
    assert "# keep comment" in text and env.stat().st_mode & 0o777 == 0o600
    assert mod.pin_deployment_source(pack, engine, mod.git_state(engine)) is False


def test_missing_and_wrong_revision_sources_cannot_rewrite_deployment(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "docker-compose.yml").write_text("services: {}\n")
    (pack / ".env").write_text("KEEP=original\n")
    for source, extras in ((tmp_path / "missing", []), (engine, ["--expected-sha", "wrong"])):
        assert mod.main([str(source), "--pack", str(pack), *extras]) == 1
        assert (pack / ".env").read_text() == "KEEP=original\n"


def test_source_pin_refuses_non_deployments_and_unverified_state(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    with pytest.raises(ValueError, match="no docker-compose"):
        mod.pin_deployment_source(tmp_path, engine, mod.git_state(engine))
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    with pytest.raises(ValueError, match="unverified"):
        mod.pin_deployment_source(tmp_path, engine, {})


def test_source_pin_refuses_a_multiline_host_path(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    with pytest.raises(ValueError, match="one line"):
        mod.pin_deployment_source(tmp_path, Path("bad\ncontext"), mod.git_state(engine))
    assert not (tmp_path / ".env").exists()


def test_cli_pin_reports_non_deployment_and_write_failure(tmp_path, monkeypatch, capsys):
    mod = _load()
    engine = _repo(tmp_path)
    assert mod.main([str(engine), "--pack", str(tmp_path)]) == 1
    assert "no docker-compose" in capsys.readouterr().err
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    def denied(*args, **kwargs):
        raise PermissionError("fixture denies writing the config")
    monkeypatch.setattr(mod.os, "open", denied)
    assert mod.main([str(engine), "--pack", str(tmp_path)]) == 1
    assert "config" in capsys.readouterr().err
    assert not (tmp_path / ".env").exists()


def test_cli_pin_creates_owner_only_config_and_checks_exact_revision(tmp_path):
    mod = _load()
    engine = _repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    sha = mod.git_state(engine)["head"]
    assert mod.main([str(engine), "--pack", str(tmp_path), "--expected-sha", sha]) == 0
    assert f"OKENGINE_SOURCE_SHA={sha}" in (tmp_path / ".env").read_text()
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_failed_source_config_replace_preserves_original_secrets_and_cleans_staging(
        tmp_path, monkeypatch):
    mod = _load()
    engine = _repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    env = tmp_path / ".env"
    env.write_text("SECRET=original\nENGINE_DIR=/obsolete\n")
    def denied(*args, **kwargs):
        raise PermissionError("cannot replace config")
    monkeypatch.setattr(mod.os, "replace", denied)
    assert mod.main([str(engine), "--pack", str(tmp_path)]) == 1
    assert env.read_text() == "SECRET=original\nENGINE_DIR=/obsolete\n"
    assert list(tmp_path.glob(".okengine-source-*")) == []
