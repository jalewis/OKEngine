import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[2] / "scripts" / "cron" / "artifact_contract.py"
SPEC = importlib.util.spec_from_file_location("artifact_contract_test", MODULE)
artifact_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact_contract)


def test_accepts_only_the_declared_schema():
    assert artifact_contract.validate({"api": 1}) == []
    assert artifact_contract.validate({"api": 1, "min_artifacts": 0}) == []

    assert artifact_contract.validate({"api": 1, "extra": True}, "job") == [
        "job has unknown key(s): ['extra']"
    ]


def test_rejects_wrong_shape_api_and_minimum_types():
    assert artifact_contract.validate([]) == ["artifact_contract must be an object"]
    assert artifact_contract.validate({}) == ["artifact_contract.api must be 1"]
    for api in (False, 1.0, 2):
        assert artifact_contract.validate({"api": api}) == [
            "artifact_contract.api must be 1"
        ]

    for value in (True, -1, 1.5, "1"):
        assert artifact_contract.validate({"api": 1, "min_artifacts": value}) == [
            "artifact_contract.min_artifacts must be a non-negative integer"
        ]
