import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


MODULE = Path(__file__).parents[2] / "patches" / "cron-plus" / "artifact_records.py"
SPEC = importlib.util.spec_from_file_location("artifact_records", MODULE)
artifact_records = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact_records)


def _job(minimum=1):
    return {"no_agent": True, "artifact_contract": {"api": 1, "min_artifacts": minimum}}


def test_collects_validated_artifact_and_strips_control_line(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    target = vault / "wiki" / "report.md"
    target.parent.mkdir(parents=True)
    target.write_text("result\n")
    digest = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setenv("WIKI_PATH", str(vault))
    declaration = json.dumps({
        "path": "wiki/report.md", "operation": "create", "count": 7,
        "sha256": digest,
    })

    response, artifacts = artifact_records.collect(
        _job(), f"useful output\nOKENGINE_ARTIFACT: {declaration}", tmp_path / "runtime")

    assert response == "useful output"
    assert artifacts == [{
        "path": target.resolve().as_posix(), "operation": "create",
        "count": 7, "sha256": digest,
    }]


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd"])
def test_refuses_paths_outside_mounted_scope(tmp_path, monkeypatch, path):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(vault))
    line = "OKENGINE_ARTIFACT: " + json.dumps({"path": path, "operation": "verify"})

    with pytest.raises(artifact_records.ArtifactError, match="escapes mounted"):
        artifact_records.collect(_job(), line, tmp_path / "runtime")


def test_required_missing_artifact_fails_instead_of_false_green(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "vault"))
    with pytest.raises(artifact_records.ArtifactError, match="requires at least 1"):
        artifact_records.collect(_job(), "script exited zero", tmp_path / "runtime")


def test_declared_but_absent_artifact_fails(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(vault))
    line = 'OKENGINE_ARTIFACT: {"path":"wiki/missing.md","operation":"create"}'
    with pytest.raises(artifact_records.ArtifactError, match="artifact is missing"):
        artifact_records.collect(_job(), line, tmp_path / "runtime")


def test_jobs_without_contract_are_unchanged(tmp_path):
    response = 'OKENGINE_ARTIFACT: {"path":"anything","operation":"create"}'
    assert artifact_records.collect({"no_agent": True}, response, tmp_path) == (response, [])


def test_model_job_cannot_claim_deterministic_artifacts(tmp_path):
    with pytest.raises(artifact_records.ArtifactError, match="only valid for no_agent"):
        artifact_records.collect(
            {"no_agent": False, "artifact_contract": {"api": 1}}, "", tmp_path)


def test_hash_mismatch_and_invalid_count_fail_closed(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "out"
    target.write_text("actual")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    for extra, match in [
        ({"sha256": "sha256:" + "0" * 64}, "does not match"),
        ({"count": -1}, "non-negative"),
    ]:
        value = {"path": "out", "operation": "update", **extra}
        with pytest.raises(artifact_records.ArtifactError, match=match):
            artifact_records.collect(
                _job(), "OKENGINE_ARTIFACT: " + json.dumps(value), tmp_path / "runtime")


def test_runtime_contract_validation_rejects_each_invalid_boundary():
    assert artifact_records.validate_contract(
        {"api": 1, "min_artifacts": 1, "extra": True}, "job"
    ) == ["job has unknown key(s): ['extra']"]
    for api in (False, 0, 1.0, 2):
        assert artifact_records.validate_contract({"api": api}) == [
            "artifact_contract.api must be 1"
        ]
    assert artifact_records.validate_contract({"api": 1, "min_artifacts": 0}) == []
    for minimum in (True, -1, 1.5, "1"):
        assert artifact_records.validate_contract(
            {"api": 1, "min_artifacts": minimum}
        ) == ["artifact_contract.min_artifacts must be a non-negative integer"]


def test_inside_distinguishes_equal_descendant_and_unrelated_paths(tmp_path):
    root = (tmp_path / "root").resolve()
    same_value = Path(str(root))
    assert artifact_records._inside(same_value, [root]) is True
    assert artifact_records._inside(root / "child", [root]) is True
    assert artifact_records._inside(tmp_path / "other", [root]) is False
    assert artifact_records._inside(tmp_path / "z-unrelated", [root]) is False


@pytest.mark.parametrize("path", [None, 1, "", "   "])
def test_rejects_every_empty_or_non_string_artifact_path(tmp_path, monkeypatch, path):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(vault))
    line = "OKENGINE_ARTIFACT: " + json.dumps(
        {"path": path, "operation": "verify"}
    )
    with pytest.raises(artifact_records.ArtifactError, match="non-empty string"):
        artifact_records.collect(_job(), line, tmp_path / "runtime")


def test_absolute_artifact_inside_secondary_mounted_root_is_allowed(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    home = tmp_path / "runtime"
    target = home / "output" / "result.txt"
    target.parent.mkdir(parents=True)
    target.write_text("result", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    line = "OKENGINE_ARTIFACT: " + json.dumps(
        {"path": str(target), "operation": "verify"}
    )

    _, artifacts = artifact_records.collect(_job(), line, home)
    assert artifacts[0]["path"] == target.resolve().as_posix()


def test_rejects_directory_and_malformed_declaration(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    directory = vault / "wiki" / "report"
    directory.mkdir(parents=True)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    directory_line = "OKENGINE_ARTIFACT: " + json.dumps(
        {"path": "wiki/report", "operation": "verify"}
    )
    with pytest.raises(artifact_records.ArtifactError, match="not a file"):
        artifact_records.collect(_job(), directory_line, tmp_path / "runtime")
    with pytest.raises(artifact_records.ArtifactError, match="malformed"):
        artifact_records.collect(
            _job(), "OKENGINE_ARTIFACT: {", tmp_path / "runtime"
        )


def test_rejects_bool_and_string_counts_and_bad_hash_shape(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "out").write_text("actual", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    for extra, match in [
        ({"count": True}, "non-negative"),
        ({"count": "1"}, "non-negative"),
        ({"sha256": 1}, "sha256:<hex>"),
        ({"sha256": "md5:abc"}, "sha256:<hex>"),
    ]:
        line = "OKENGINE_ARTIFACT: " + json.dumps(
            {"path": "out", "operation": "verify", **extra}
        )
        with pytest.raises(artifact_records.ArtifactError, match=match):
            artifact_records.collect(_job(), line, tmp_path / "runtime")

    zero_count = "OKENGINE_ARTIFACT: " + json.dumps(
        {"path": "out", "operation": "verify", "count": 0}
    )
    _, artifacts = artifact_records.collect(_job(), zero_count, tmp_path / "runtime")
    assert artifacts[0]["count"] == 0


def test_hash_mismatch_is_rejected_regardless_of_lexical_order(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "out").write_text("actual", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    for digest in ("sha256:" + "0" * 64, "sha256:" + "f" * 64):
        line = "OKENGINE_ARTIFACT: " + json.dumps(
            {"path": "out", "operation": "verify", "sha256": digest}
        )
        with pytest.raises(artifact_records.ArtifactError, match="does not match"):
            artifact_records.collect(_job(), line, tmp_path / "runtime")


def test_contract_default_minimum_and_surplus_declarations(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("one", "two"):
        (vault / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(vault))
    default_contract = {"no_agent": True, "artifact_contract": {"api": 1}}
    with pytest.raises(artifact_records.ArtifactError, match="at least 1"):
        artifact_records.collect(default_contract, "", tmp_path / "runtime")

    lines = "\n".join(
        "OKENGINE_ARTIFACT: "
        + json.dumps({"path": name, "operation": "verify"})
        for name in ("one", "two")
    )
    _, artifacts = artifact_records.collect(_job(1), lines, tmp_path / "runtime")
    assert [Path(item["path"]).name for item in artifacts] == ["one", "two"]


def test_truthy_non_boolean_no_agent_cannot_claim_artifacts(tmp_path):
    with pytest.raises(artifact_records.ArtifactError, match="only valid for no_agent"):
        artifact_records.collect(
            {"no_agent": 1, "artifact_contract": {"api": 1, "min_artifacts": 0}},
            "",
            tmp_path,
        )
