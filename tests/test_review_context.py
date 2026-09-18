import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_context", ROOT / "scripts/review_context.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _repo(tmp_path, *, branch="main"):
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"],
                   check=True)
    (tmp_path / "engine-manifest.yaml").write_text("engine_release: v9.1.2\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", str(tmp_path)],
                   check=True)
    subprocess.run(["git", "-C", str(tmp_path), "fetch", "-q", "origin",
                    "main:refs/remotes/origin/main"], check=True)
    return tmp_path


def test_clean_current_branch_is_attributable(tmp_path):
    context = MODULE.collect(_repo(tmp_path))
    assert context["engine_release"] == "v9.1.2"
    assert context["branch"] == "main" and context["dirty"] is False
    assert context["ahead_of_origin_main"] == context["behind_origin_main"] == 0
    assert MODULE.problems(context) == []


def test_dirty_stale_and_unavailable_remote_are_explicit(tmp_path):
    repo = _repo(tmp_path)
    (repo / "dirty.txt").write_text("x")
    assert "working tree is dirty" in MODULE.problems(MODULE.collect(repo))
    subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)
    assert "origin/main distance is unavailable" in MODULE.problems(MODULE.collect(repo))


def test_detached_exact_tag_is_allowed_but_untagged_detach_is_not(tmp_path):
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "tag", "v9.1.2"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--detach"], check=True)
    assert MODULE.problems(MODULE.collect(repo)) == []
    subprocess.run(["git", "-C", str(repo), "tag", "-d", "v9.1.2"],
                   check=True, stdout=subprocess.DEVNULL)
    assert "detached revision is not an exact tag" in MODULE.problems(MODULE.collect(repo))


def test_override_is_loud_and_persisted(tmp_path, capsys):
    artifact = tmp_path / "evidence/context.json"
    code = MODULE.main(["--repo", str(tmp_path), "--strict", "--allow-unattributable",
                        "--artifact", str(artifact)])
    assert code == 0 and "OVERRIDDEN" in capsys.readouterr().out
    payload = json.loads(artifact.read_text())
    assert payload["override"] is True and payload["problems"]


def test_framework_exposes_canonical_review_context():
    spec = importlib.util.spec_from_file_location("framework", ROOT / "scripts/framework.py")
    framework = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(framework)
    assert framework._COMMANDS["review-context"] == ("review_context", "review_context.py")


def test_generated_evidence_state_is_ignored_by_source_checkout():
    ignored = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "artifacts/review-context.json",
         ".okengine/effective-policy.json", ".okengine/qualification/report.json"],
        check=False, capture_output=True, text=True,
    )
    assert ignored.returncode == 0
    assert set(ignored.stdout.splitlines()) == {
        "artifacts/review-context.json",
        ".okengine/effective-policy.json",
        ".okengine/qualification/report.json",
    }


def test_command_failure_clean_render_and_strict_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        OSError("git unavailable")))
    assert MODULE._git(tmp_path, "status") is None
    clean = {
        "is_repo": True, "branch": "main", "exact_tag": None, "sha": "a" * 40,
        "engine_release": "v1", "dirty": False, "remote_distance_available": True,
        "ahead_of_origin_main": 0, "behind_origin_main": 0,
    }
    assert "attributable and current" in MODULE.render(clean, [])
    behind = dict(clean, behind_origin_main=2)
    assert any("2 commit(s) behind" in error for error in MODULE.problems(behind))

    monkeypatch.setattr(MODULE, "collect", lambda _repo: {
        "is_repo": False, "sha": None, "engine_release": None, "dirty": None,
        "remote_distance_available": False, "detached": False,
    })
    assert MODULE.main(["--repo", str(tmp_path), "--strict", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["problems"]
    monkeypatch.setenv("OKENGINE_ALLOW_UNATTRIBUTABLE_EVIDENCE", "1")
    assert MODULE.main(["--repo", str(tmp_path), "--strict"]) == 0
