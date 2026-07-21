from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "application_profiles.py"
FIXTURE = REPO / "tests" / "fixtures" / "applications" / "threat-informed-detection"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mutated_pack(tmp_path: Path, mutate) -> Path:
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    mutate(declaration, pack)
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))
    return pack


def test_tid_profile_inherits_che_without_copying_it():
    module = _load(SCRIPT, "tid_profile_effective")
    raw = yaml.safe_load(
        (REPO / "applications" / "threat-informed-detection" / "application.yaml").read_text())
    effective = module.load_profile("threat-informed-detection", REPO)

    assert raw["extends"] == {"profile": "continuous-hypothesis", "version": ">=1.0.0"}
    assert "scheduled_reevaluation_only" not in raw["policy"]
    assert effective["policy"]["scheduled_reevaluation_only"] is True
    assert effective["policy"]["preserve_source_procedure"] is True
    assert [row["id"] for row in effective["operating_loop"][:2]] == ["register", "assess"]
    assert effective["operating_loop"][-1]["id"] == "measure-outcome"
    assert effective["required_surfaces"][:3] == [
        "dependency_explanation", "assessment_review", "portfolio_learning"]
    assert effective["required_surfaces"][-1] == "gap_workbench"
    assert len(effective["binding_contract"]["required_roles"]) == 10


def test_tid_synthetic_composition_conforms():
    module = _load(SCRIPT, "tid_profile_valid")
    assert module.validate(FIXTURE, REPO) == []

    framework = _load(REPO / "scripts" / "framework_validate.py", "tid_framework_valid")
    report = framework.Report()
    framework.check_application_profile(FIXTURE, report)
    assert report.n_fail == 0
    assert ("OK", "application profile", "threat-informed-detection 0.1.0") in report.rows


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda declaration, _pack: declaration["bindings"]["roles"].pop("telemetry_source"),
            "bindings.roles.telemetry_source requires at least 1 binding(s); found 0",
        ),
        (
            lambda declaration, _pack: declaration["bindings"]["roles"]["validation_result"][0]
            ["operations"].pop("import"),
            "bindings.roles.validation_result[0].operations.import is required",
        ),
        (
            lambda declaration, _pack: declaration["surfaces"].pop("coverage_facets"),
            "surfaces.coverage_facets is required",
        ),
        (
            lambda declaration, _pack: declaration["queues"].pop("telemetry_gaps"),
            "queues.telemetry_gaps is required",
        ),
        (
            lambda declaration, _pack: declaration["success_measures"].pop(
                "current_validation_rate"),
            "success_measures.current_validation_rate is required",
        ),
    ],
)
def test_tid_negative_fixture_fails_at_intended_contract(tmp_path, mutation, expected):
    module = _load(SCRIPT, f"tid_profile_negative_{expected.split('.')[0]}")
    pack = _mutated_pack(tmp_path, mutation)
    errors = module.validate(pack, REPO)
    assert any(expected in error for error in errors)


def test_tid_requires_inherited_che_extensions(tmp_path):
    module = _load(SCRIPT, "tid_profile_che_dependency")

    def remove_assessments(_declaration, pack):
        state_path = pack / ".okengine" / "extensions.yaml"
        state = yaml.safe_load(state_path.read_text())
        state["enabled"].pop("okengine.assessments")
        state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    pack = _mutated_pack(tmp_path, remove_assessments)
    errors = module.validate(pack, REPO)
    assert any(
        "required extension okengine.assessments is not enabled "
        "(inherited from continuous-hypothesis)" in error
        for error in errors
    )


def test_tid_errors_distinguish_child_and_inherited_surfaces(tmp_path):
    module = _load(SCRIPT, "tid_profile_requirement_origin")

    def remove_surfaces(declaration, _pack):
        declaration["surfaces"].pop("assessment_review")
        declaration["surfaces"].pop("coverage_facets")

    errors = module.validate(_mutated_pack(tmp_path, remove_surfaces), REPO)
    assert "surfaces.assessment_review is required (inherited from continuous-hypothesis)" in errors
    assert "surfaces.coverage_facets is required (required by threat-informed-detection)" in errors


def test_external_role_requires_declared_provider_without_copying_schema(tmp_path):
    module = _load(SCRIPT, "tid_external_role")
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    schema_path = pack / "schema.yaml"
    schema = yaml.safe_load(schema_path.read_text())
    schema["types"].pop("threat-procedure")
    schema["partitioning"]["namespaces"].pop("procedures")
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False))
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    declaration["bindings"]["roles"]["threat_procedure"][0]["provided_by"] = \
        "okpack-analysis-overlay"
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))
    (pack / "pack.yaml").write_text(
        "name: consumer\nversion: 0.1.0\nrequires: [okpack-analysis-overlay@>=0.5.0]\n")

    assert module.validate(pack, REPO) == []

    (pack / "pack.yaml").write_text("name: consumer\nversion: 0.1.0\nrequires: []\n")
    errors = module.validate(pack, REPO)
    assert any("provided_by 'okpack-analysis-overlay' must name a pack declared" in error
               for error in errors)


def test_external_role_contract_is_checked_when_composed_schema_is_present(tmp_path):
    module = _load(SCRIPT, "tid_external_role_composed")
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    binding = declaration["bindings"]["roles"]["threat_procedure"][0]
    binding["provided_by"] = "okpack-analysis-overlay"
    binding["namespace"] = "missing-producer-namespace"
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))
    (pack / "pack.yaml").write_text(
        "name: consumer\nversion: 0.1.0\nrequires: [okpack-analysis-overlay@>=0.5.0]\n")

    errors = module.validate(pack, REPO)
    assert any("missing-producer-namespace" in error and "not declared" in error
               for error in errors)
