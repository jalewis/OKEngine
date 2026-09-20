from __future__ import annotations

import json
import subprocess
import datetime as dt
from pathlib import Path

import pytest

from ci import change_scope


pytestmark = pytest.mark.invariant


def test_documentation_classifier_accepts_docs_and_root_release_notes():
    result = change_scope.classify(["docs/guide.md", "CHANGELOG.md", "README.md"])
    assert result["scope"] == "docs-only"
    assert result["docs_only"] is True
    assert result["non_documentation_paths"] == []


def test_documentation_classifier_rejects_mixed_diff():
    result = change_scope.classify(["docs/guide.md", "src/okengine/runtime.py"])
    assert result["scope"] == "mixed-or-code"
    assert result["docs_only"] is False
    assert result["non_documentation_paths"] == ["src/okengine/runtime.py"]


def test_dependency_txt_is_not_misclassified_as_documentation():
    result = change_scope.classify(["requirements-dev.txt"])
    assert result["scope"] == "mixed-or-code"


@pytest.mark.parametrize("path", [
    "prompts/research.md",
    "templates/pack/CLAUDE.md",
    "extensions/okengine.example/prompts/run.md",
])
def test_behavioral_markdown_is_not_documentation(path):
    assert change_scope.classify([path])["scope"] == "mixed-or-code"


def test_empty_diff_is_undetectable_not_docs_only():
    with pytest.raises(ValueError, match="no changed paths"):
        change_scope.classify([])


def test_budget_passes_and_fails_at_the_declared_boundary():
    scope = change_scope.classify(["CHANGELOG.md"])
    passing = change_scope.evaluate_budget(
        scope, "2026-09-18T12:00:00Z", "2026-09-18T12:05:00Z", 300
    )
    failing = change_scope.evaluate_budget(
        scope, "2026-09-18T12:00:00Z", "2026-09-18T12:05:00.001Z", 300
    )
    assert passing["within_budget"] is True
    assert failing["within_budget"] is False


def test_budget_does_not_apply_to_mixed_diff():
    scope = change_scope.classify(["README.md", "pyproject.toml"])
    result = change_scope.evaluate_budget(
        scope, "2026-09-18T12:00:00Z", "2026-09-18T13:00:00Z", 300
    )
    assert result["budget_applies"] is False
    assert result["within_budget"] is None


def test_time_and_budget_reject_naive_or_reversed_timestamps():
    with pytest.raises(ValueError, match="timezone"):
        change_scope.parse_time("2026-09-18T12:00:00")
    scope = change_scope.classify(["README.md"])
    with pytest.raises(ValueError, match="later than"):
        change_scope.evaluate_budget(
            scope, "2026-09-18T12:00:01Z", "2026-09-18T12:00:00Z", 300
        )


def test_changed_paths_requires_real_ancestor(tmp_path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("one\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "README.md").write_text("two\n")
    subprocess.run(["git", "commit", "-qam", "docs"], cwd=tmp_path, check=True)

    current = Path.cwd()
    try:
        import os
        os.chdir(tmp_path)
        assert change_scope.changed_paths(base, "HEAD") == ["README.md"]
        with pytest.raises(ValueError, match="non-zero diff base"):
            change_scope.changed_paths("0" * 40, "HEAD")
    finally:
        os.chdir(current)


def test_cli_writes_failure_artifact_before_over_budget_verdict(tmp_path: Path, monkeypatch):
    output = tmp_path / "scope.json"
    monkeypatch.setattr(change_scope, "changed_paths", lambda _base, _head: ["README.md"])
    assert change_scope.main([
        "--base", "abc", "--output", str(output),
        "--pipeline-created-at", "2026-09-18T12:00:00Z", "--budget-seconds", "300",
        "--now", "2026-09-18T12:05:01Z",
    ]) == 1
    assert json.loads(output.read_text())["within_budget"] is False


def test_cli_writes_success_and_uses_current_time_when_now_is_absent(tmp_path: Path, monkeypatch):
    output = tmp_path / "scope.json"
    monkeypatch.setattr(change_scope, "changed_paths", lambda _base, _head: ["README.md"])
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
    assert change_scope.main([
        "--base", "abc", "--output", str(output),
        "--pipeline-created-at", created, "--budget-seconds", "300",
    ]) == 0
    assert json.loads(output.read_text())["within_budget"] is True


def test_cli_can_publish_scope_without_a_duration_verdict(tmp_path: Path, monkeypatch):
    output = tmp_path / "scope.json"
    monkeypatch.setattr(change_scope, "changed_paths", lambda _base, _head: ["README.md"])
    assert change_scope.main(["--base", "abc", "--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["scope"] == "docs-only"
    assert "budget_applies" not in report


def test_cli_publishes_undetectable_artifact_before_error(tmp_path: Path, monkeypatch):
    output = tmp_path / "scope.json"
    monkeypatch.setattr(
        change_scope, "changed_paths", lambda _base, _head: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["git", "diff"])
        )
    )
    assert change_scope.main(["--base", "abc", "--output", str(output)]) == 2
    assert json.loads(output.read_text())["scope"] == "undetectable"


def test_script_entrypoint_propagates_failure_and_writes_artifact(tmp_path: Path):
    output = tmp_path / "scope.json"
    script = Path(change_scope.__file__)
    result = subprocess.run(
        [str(Path(__import__("sys").executable)), str(script), "--base", "0" * 40,
         "--output", str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "UNDETECTABLE" in result.stdout
    assert json.loads(output.read_text())["scope"] == "undetectable"
