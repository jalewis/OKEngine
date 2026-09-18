"""Execution contracts for the carried-Hermes patch applicator."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


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
