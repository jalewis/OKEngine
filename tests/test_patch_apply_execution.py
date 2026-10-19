"""Execution contracts for the carried-Hermes patch applicator."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=check)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    engine = tmp_path / "engine"
    patchdir = engine / "patches"
    hermes = tmp_path / "hermes"
    patchdir.mkdir(parents=True)
    hermes.mkdir()
    shutil.copy2(REPO / "patches" / "apply.sh", patchdir / "apply.sh")
    (engine / "engine-manifest.yaml").write_text("runtime:\n  pinned_tag: v2026.7.7.2\n")
    _run("git", "init", "-q", cwd=hermes)
    _run("git", "config", "user.email", "test@example.invalid", cwd=hermes)
    _run("git", "config", "user.name", "Test", cwd=hermes)
    (hermes / "target.txt").write_text("before\n")
    _run("git", "add", "target.txt", cwd=hermes)
    _run("git", "commit", "-qm", "base", cwd=hermes)
    (hermes / "target.txt").write_text("after\n")
    diff = _run("git", "diff", "--", "target.txt", cwd=hermes).stdout
    (patchdir / "01-test.patch").write_text(diff)
    (patchdir / "README.md").write_text("| 01 | `01-test.patch` | target | test |\n")
    _run("git", "checkout", "--", "target.txt", cwd=hermes)
    return patchdir, hermes


def test_apply_script_selects_target_patch_set_from_manifest(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    target = patchdir / "target-v2026.9.14"
    target.mkdir()
    shutil.move(patchdir / "01-test.patch", target / "01-test.patch")
    shutil.move(patchdir / "README.md", target / "README.md")
    (target / "inventory.json").write_text(
        '{"dispositions":[{"artifact":"patches/target-v2026.9.14/01-test.patch"}]}\n',
        encoding="utf-8")
    (patchdir.parent / "engine-manifest.yaml").write_text(
        "runtime:\n  pinned_tag: v2026.9.14\n", encoding="utf-8")

    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)

    assert "1 applied, 0 already-present" in result.stdout
    assert (hermes / "target.txt").read_text() == "after\n"


def test_apply_script_fails_closed_for_unknown_manifest_pin(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    (patchdir.parent / "engine-manifest.yaml").write_text(
        "runtime:\n  pinned_tag: v2099.1.1\n", encoding="utf-8")

    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)

    assert result.returncode == 3
    assert "no governed Hermes patch set" in result.stderr


def test_apply_script_applies_then_skips_idempotently(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    first = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)
    assert "1 applied, 0 already-present" in first.stdout
    assert (hermes / "target.txt").read_text() == "after\n"
    second = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)
    assert "0 applied, 1 already-present" in second.stdout


def test_apply_script_returns_two_on_patch_conflict(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    (hermes / "target.txt").write_text("diverged\n")
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "does NOT apply" in result.stderr


def test_apply_script_rejects_silently_relocated_hunk(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    target = hermes / "target.txt"
    before = "".join(f"line-{i}\n" for i in range(1, 11))
    target.write_text(before)
    _run("git", "add", "target.txt", cwd=hermes)
    _run("git", "commit", "--amend", "--no-edit", "-q", cwd=hermes)
    target.write_text(before.replace("line-5\n", "changed-5\n"))
    (patchdir / "01-test.patch").write_text(
        _run("git", "diff", "--", "target.txt", cwd=hermes).stdout
    )
    _run("git", "checkout", "--", "target.txt", cwd=hermes)
    target.write_text("unrelated header\n" + before)
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "relocates a hunk" in result.stderr
    assert target.read_text() == "unrelated header\n" + before


def test_apply_script_rejects_relocated_already_applied_hunk(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    target = hermes / "target.txt"
    before = "".join(f"line-{i}\n" for i in range(1, 11))
    target.write_text(before)
    _run("git", "add", "target.txt", cwd=hermes)
    _run("git", "commit", "--amend", "--no-edit", "-q", cwd=hermes)
    patched = before.replace("line-5\n", "changed-5\n")
    target.write_text(patched)
    (patchdir / "01-test.patch").write_text(
        _run("git", "diff", "--", "target.txt", cwd=hermes).stdout
    )
    target.write_text("unrelated header\n" + patched)
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "reverse check relocates a hunk" in result.stderr


def test_apply_script_rejects_missing_registered_patch(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    (patchdir / "README.md").write_text("| 02 | `02-missing.patch` | target | test |\n")
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 3
    assert "MISSING" in result.stderr


def _sequential_series(tmp_path: Path) -> tuple[Path, Path]:
    """A target-pin series whose second patch rewrites the line the first patch introduced, the
    shape of the real target series (03, 09-22, 13/14, 18/19 and 23 all edit one scheduler hunk)."""
    patchdir, hermes = _fixture(tmp_path)
    target = patchdir / "target-v2026.9.14"
    target.mkdir()
    (patchdir / "01-test.patch").unlink()
    (patchdir / "README.md").unlink()
    path = hermes / "target.txt"
    for number, (old, new) in enumerate((("before\n", "after\n"), ("after\n", "final\n")), start=1):
        path.write_text(new)
        diff = _run("git", "diff", "--", "target.txt", cwd=hermes).stdout
        (target / f"0{number}-step.patch").write_text(diff)
        _run("git", "add", "target.txt", cwd=hermes)
        _run("git", "commit", "-qm", f"step {number}", cwd=hermes)
    _run("git", "reset", "-q", "--hard", "HEAD~2", cwd=hermes)
    (target / "inventory.json").write_text(
        '{"dispositions":[{"artifact":"patches/target-v2026.9.14/01-step.patch"},'
        '{"artifact":"patches/target-v2026.9.14/02-step.patch"}]}\n', encoding="utf-8")
    (patchdir.parent / "engine-manifest.yaml").write_text(
        "runtime:\n  pinned_tag: v2026.9.14\n", encoding="utf-8")
    return patchdir, hermes


def test_rerun_on_a_fully_patched_sequential_series_is_a_noop(tmp_path):
    """okengine#991 audit 1 [16]: once patch 02 rewrote patch 01's line, patch 01 no longer
    reversed on its own, so a re-run blamed 'Hermes drift' and exited 2 on a correct tree."""
    patchdir, hermes = _sequential_series(tmp_path)
    first = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)
    assert "2 applied, 0 already-present" in first.stdout
    second = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert second.returncode == 0, second.stderr
    assert "0 applied, 2 already-present" in second.stdout
    assert (hermes / "target.txt").read_text() == "final\n"


def test_rerun_resumes_a_partially_patched_sequential_series(tmp_path):
    patchdir, hermes = _sequential_series(tmp_path)
    _run("git", "apply", str(patchdir / "target-v2026.9.14" / "01-step.patch"), cwd=hermes)
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 0, result.stderr
    assert "already applied: 01-step.patch" in result.stdout
    assert "1 applied, 1 already-present" in result.stdout
    assert (hermes / "target.txt").read_text() == "final\n"


def test_sequential_series_on_a_diverged_tree_still_fails_as_drift(tmp_path):
    patchdir, hermes = _sequential_series(tmp_path)
    (hermes / "target.txt").write_text("diverged\n")
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "does NOT apply: 01-step.patch" in result.stderr
    assert (hermes / "target.txt").read_text() == "diverged\n"


def _three_step_series(tmp_path: Path) -> tuple[Path, Path]:
    """A 30-line file: 01 edits line 3, 02 re-edits line 3 and edits line 20, 03 edits line 12."""
    patchdir, hermes = _fixture(tmp_path)
    target = patchdir / "target-v2026.9.14"
    target.mkdir()
    (patchdir / "01-test.patch").unlink()
    (patchdir / "README.md").unlink()
    path = hermes / "target.txt"
    lines = [f"line-{i}\n" for i in range(1, 31)]
    path.write_text("".join(lines))
    _run("git", "commit", "-qam", "base 30", cwd=hermes)
    steps = [{3: "one-3\n"}, {3: "two-3\n", 20: "two-20\n"}, {12: "three-12\n"}]
    for number, edits in enumerate(steps, start=1):
        for line_no, text in edits.items():
            lines[line_no - 1] = text
        path.write_text("".join(lines))
        (target / f"0{number}-step.patch").write_text(
            _run("git", "diff", "--", "target.txt", cwd=hermes).stdout)
        _run("git", "commit", "-qam", f"step {number}", cwd=hermes)
    _run("git", "reset", "-q", "--hard", "HEAD~3", cwd=hermes)
    (target / "inventory.json").write_text(json.dumps({"dispositions": [
        {"artifact": f"patches/target-v2026.9.14/0{n}-step.patch"} for n in (1, 2, 3)]}))
    (patchdir.parent / "engine-manifest.yaml").write_text(
        "runtime:\n  pinned_tag: v2026.9.14\n", encoding="utf-8")
    return patchdir, hermes


def test_drift_in_a_later_patch_of_a_patched_tree_names_that_patch(tmp_path):
    """QA on !1250: a fully patched tree with drift inside patch 02's hunk was reported as
    '01 does NOT apply ... on top of 0 already-applied', blaming a patch that is fine."""
    patchdir, hermes = _three_step_series(tmp_path)
    _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)
    path = hermes / "target.txt"
    path.write_text(path.read_text().replace("two-20\n", "two-20 local edit\n"))
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "does NOT apply: 02-step.patch" in result.stderr, result.stderr
    assert "from the top of the series" in result.stderr
    assert "target.txt" in result.stderr
    assert "two-20 local edit" in path.read_text()


def test_upstream_drift_on_a_pristine_tree_names_the_first_failing_patch(tmp_path):
    patchdir, hermes = _three_step_series(tmp_path)
    path = hermes / "target.txt"
    path.write_text(path.read_text().replace("line-12\n", "upstream-12\n"))
    _run("git", "commit", "-qam", "upstream moved line 12", cwd=hermes)
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "does NOT apply: 03-step.patch" in result.stderr, result.stderr
    assert "upstream drift" in result.stderr and "from the top" not in result.stderr


def test_local_drift_ahead_of_a_partial_prefix_names_the_next_patch(tmp_path):
    patchdir, hermes = _three_step_series(tmp_path)
    _run("git", "apply", str(patchdir / "target-v2026.9.14" / "01-step.patch"), cwd=hermes)
    path = hermes / "target.txt"
    path.write_text(path.read_text().replace("line-20\n", "local-20\n"))
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2
    assert "does NOT apply: 02-step.patch" in result.stderr, result.stderr
    assert "next patch after" in result.stderr and "from the top" not in result.stderr


_NUMSTAT_FAILS = 'if [ "$1" = diff ] && [ "$2" = --numstat ]; then exit 1; fi\n'
# The top-level snapshot is the first write-tree; every later one (inside the diagnosis) fails.
_LATE_WRITE_TREE_FAILS = (
    'if [ "$1" = write-tree ]; then\n'
    '  n=$(( $(cat "$SHIM_COUNT" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$SHIM_COUNT"\n'
    '  [ "$n" -gt 1 ] && exit 1\n'
    'fi\n')


@pytest.mark.parametrize("failing_step", [_NUMSTAT_FAILS, _LATE_WRITE_TREE_FAILS],
                         ids=["numstat", "write-tree"])
def test_a_failing_diagnosis_falls_back_to_the_stopping_patch_with_exit_two(tmp_path, failing_step):
    """QA on !1256: under `set -euo pipefail`, a diagnosis that printed nothing made `read` hit EOF
    and the script exit 1 before the drift line; and a diagnosis failing PARTWAY carried on with
    empty data and confidently named the wrong patch (01). A failed diagnosis must name the patch
    the series stopped at and keep the exit-2 drift contract."""
    patchdir, hermes = _three_step_series(tmp_path)
    _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)
    path = hermes / "target.txt"
    path.write_text(path.read_text().replace("three-12\n", "three-12 local edit\n"))
    shim = tmp_path / "shim"
    shim.mkdir()
    real_git = shutil.which("git")
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n" + failing_step + f'exec "{real_git}" "$@"\n')
    (shim / "git").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
           "SHIM_COUNT": str(tmp_path / "count")}
    result = subprocess.run(["bash", str(patchdir / "apply.sh"), str(hermes)], cwd=patchdir,
                            text=True, capture_output=True, env=env)
    assert result.returncode == 2, result.stderr
    assert "does NOT apply: 03-step.patch" in result.stderr, result.stderr
    assert "differing:" not in result.stderr and "from the top of the series" not in result.stderr
    assert "three-12 local edit" in path.read_text()


def test_a_failed_first_snapshot_exits_one_with_an_error_and_leaves_the_checkout_alone(tmp_path):
    """QA on !1256 (okengine#993): when `update-index` failed while snapshotting the checkout,
    before any patch was applied, the script exited 1 with no message at all. It must still fail
    closed with exit 1 (an environment/checkout problem, not drift), and say why."""
    patchdir, hermes = _three_step_series(tmp_path)
    shim = tmp_path / "shim"
    shim.mkdir()
    real_git = shutil.which("git")
    (shim / "git").write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = update-index ]; then exit 1; fi\n'
        f'exec "{real_git}" "$@"\n')
    (shim / "git").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(patchdir / "apply.sh"), str(hermes)], cwd=patchdir,
                            text=True, capture_output=True, env=env)
    assert result.returncode == 1, result.stderr
    assert "ERROR: could not snapshot the checkout" in result.stderr, result.stderr
    assert "applied" not in result.stdout
    assert (hermes / "target.txt").read_text() == "".join(f"line-{i}\n" for i in range(1, 31))


# --- okengine#1042 [34]: a subdirectory of another work tree is not a Hermes checkout -----------

def test_a_hermes_tree_nested_in_another_repo_is_refused_not_reported_patched(tmp_path):
    """[34]: an unpacked Hermes source inside some other git repo passed the work-tree check, the
    patch paths resolved against the OUTER root, and the run said '0 applied, N already-present'
    with exit 0 on a tree that carried no patch at all."""
    patchdir, hermes = _fixture(tmp_path)
    outer = tmp_path / "outer"
    outer.mkdir()
    _run("git", "init", "-q", cwd=outer)
    _run("git", "config", "user.email", "test@example.invalid", cwd=outer)
    _run("git", "config", "user.name", "Test", cwd=outer)
    nested = outer / "hermes"
    nested.mkdir()
    (nested / "target.txt").write_text("before\n")
    _run("git", "add", "hermes/target.txt", cwd=outer)
    _run("git", "commit", "-qm", "vendored hermes", cwd=outer)
    result = _run("bash", str(patchdir / "apply.sh"), str(nested), cwd=patchdir, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "not the top level" in result.stderr
    assert "already-present" not in result.stdout
    assert (nested / "target.txt").read_text() == "before\n"


# --- okengine#1042 [35]: the manifest's pinned commit is enforced, not advisory -----------------

def _pin_sha(patchdir: Path, sha: str) -> None:
    manifest = patchdir.parent / "engine-manifest.yaml"
    manifest.write_text(manifest.read_text() + f"  pinned_sha: {sha}   # the tag's commit\n")


def test_a_checkout_at_a_different_commit_than_the_pinned_sha_is_refused(tmp_path):
    """[35]: the active series tolerates hunk offsets, so placement is only proven on the exact
    commit the series was cut against. A different commit used to get a warning and exit 0."""
    patchdir, hermes = _fixture(tmp_path)
    _pin_sha(patchdir, "0" * 40)
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not at the pinned commit" in result.stderr
    assert (hermes / "target.txt").read_text() == "before\n"


def test_a_checkout_at_the_pinned_sha_applies(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    _pin_sha(patchdir, _run("git", "rev-parse", "HEAD", cwd=hermes).stdout.strip())
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir)
    assert "1 applied, 0 already-present" in result.stdout
    assert (hermes / "target.txt").read_text() == "after\n"


def test_an_explicit_override_applies_off_pin_with_a_warning(tmp_path):
    """A Hermes bump rebases the series against a new commit before the manifest moves."""
    patchdir, hermes = _fixture(tmp_path)
    _pin_sha(patchdir, "0" * 40)
    env = {**os.environ, "OKENGINE_ALLOW_UNPINNED_HERMES": "1"}
    result = subprocess.run(["bash", str(patchdir / "apply.sh"), str(hermes)], cwd=patchdir,
                            text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert "OKENGINE_ALLOW_UNPINNED_HERMES" in result.stderr
    assert (hermes / "target.txt").read_text() == "after\n"


def test_a_malformed_pinned_sha_fails_closed(tmp_path):
    patchdir, hermes = _fixture(tmp_path)
    _pin_sha(patchdir, "not-a-sha")
    result = _run("bash", str(patchdir / "apply.sh"), str(hermes), cwd=patchdir, check=False)
    assert result.returncode == 3
    assert "pinned_sha" in result.stderr


def test_the_real_manifest_pins_a_full_commit_sha():
    """The enforcement above is only as good as the value it reads."""
    text = (REPO / "engine-manifest.yaml").read_text(encoding="utf-8")
    runtime_sha = text.split("pinned_sha:", 1)[1].split()[0]
    assert len(runtime_sha) == 40 and all(c in "0123456789abcdef" for c in runtime_sha)
