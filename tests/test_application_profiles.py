from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "application_profiles.py"
FIXTURE = REPO / "tests" / "fixtures" / "applications" / "che"
INHERITANCE = REPO / "tests" / "fixtures" / "applications" / "inheritance" / "profiles"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_engine(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    (engine / "applications").mkdir(parents=True)
    for profile in INHERITANCE.iterdir():
        shutil.copytree(profile, engine / "applications" / profile.name)
    for name in ("config", "extensions", "scripts"):
        (engine / name).symlink_to(REPO / name, target_is_directory=True)
    return engine


def test_che_profile_fixture_conforms():
    module = _load(SCRIPT, "application_profiles_valid")
    assert module.validate(FIXTURE, REPO) == []


def test_framework_validate_uses_application_contract():
    framework = _load(REPO / "scripts" / "framework_validate.py", "framework_application")
    report = framework.Report()
    framework.check_application_profile(FIXTURE, report)
    assert report.n_fail == 0
    assert ("OK", "application profile", "continuous-hypothesis 1.0.0") in report.rows


def test_catalog_profile_contract_is_self_conformant():
    module = _load(SCRIPT, "application_profiles_catalog")
    profile = yaml.safe_load(
        (REPO / "applications" / "continuous-hypothesis" / "application.yaml").read_text())
    assert module.validate_profile_manifest(profile) == []


def test_profile_inheritance_is_deterministic_and_additive(tmp_path):
    module = _load(SCRIPT, "application_profiles_inheritance")
    engine = _fixture_engine(tmp_path)

    first = module.load_profile("fixture-child", engine)
    second = module.load_profile("fixture-child", engine)

    assert first == second
    assert first["id"] == "fixture-child"
    assert [row["id"] for row in first["operating_loop"]] == ["register", "assess", "decide"]
    assert first["required_surfaces"] == ["assessment_review", "decision_trace"]
    assert first["required_queues"] == ["assessment_review", "decision_queue"]
    assert first["required_success_measures"] == ["review_queue_age", "decision_latency"]
    assert set(first["binding_contract"]["required_roles"]) == {
        "evidence_item", "decision_record"}
    assert first["policy"] == {
        "preserve_evidence": True, "optional_review": True, "reviewed_decisions": True}


@pytest.mark.parametrize(
    ("profile_id", "message"),
    [
        ("fixture-cycle-a", "inheritance cycle"),
        ("fixture-incompatible", "catalog parent version"),
        ("fixture-conflict", "operating_loop stage 'assess' conflicts"),
        ("fixture-weaken", "policy.preserve_evidence cannot weaken"),
    ],
)
def test_profile_inheritance_rejects_unsafe_composition(tmp_path, profile_id, message):
    module = _load(SCRIPT, f"application_profiles_{profile_id}")
    engine = _fixture_engine(tmp_path)
    with pytest.raises(module.ApplicationProfileError, match=message):
        module.load_profile(profile_id, engine)


def test_profile_inheritance_reports_missing_parent_origin(tmp_path):
    module = _load(SCRIPT, "application_profiles_missing_parent")
    engine = _fixture_engine(tmp_path)
    child = engine / "applications" / "fixture-missing" / "application.yaml"
    child.parent.mkdir()
    child.write_text("""\
schema_version: 1
id: fixture-missing
version: 0.1.0
name: Missing parent
extends: {profile: absent-parent, version: \">=1.0.0\"}
""")
    with pytest.raises(module.ApplicationProfileError) as caught:
        module.load_profile("fixture-missing", engine)
    assert "fixture-missing" in str(caught.value)
    assert "extends.profile 'absent-parent'" in str(caught.value)


def test_generic_role_bindings_validate_against_effective_schema_and_operations(tmp_path):
    module = _load(SCRIPT, "application_profiles_roles")
    engine = _fixture_engine(tmp_path)
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    schema_path = pack / "schema.yaml"
    schema = yaml.safe_load(schema_path.read_text())
    schema["types"].update({
        "artifact": {"required": ["type", "behavior", "evidence", "as_of"]},
        "decision": {"required": ["type", "subject", "status"]},
    })
    schema["partitioning"]["namespaces"].update({
        "artifacts": {"strategy": "flat"}, "decisions": {"strategy": "flat"}})
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False))
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    declaration["profile"] = "fixture-child"
    declaration["profile_version"] = "0.1.0"
    declaration["bindings"]["roles"] = {
        "evidence_item": [{
            "type": "artifact", "namespace": "artifacts", "behavior_field": "behavior",
            "evidence_field": "evidence", "as_of_field": "as_of",
        }],
        "decision_record": [{
            "type": "decision", "namespace": "decisions", "subject_field": "subject",
            "status_field": "status", "operations": {"refresh": "forecast-reassess"},
        }],
    }
    declaration["surfaces"]["decision_trace"] = "dashboards/decision-trace"
    declaration["queues"]["decision_queue"] = "decisions"
    declaration["success_measures"]["decision_latency"] = "dashboards/decision-trace"
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))

    assert module.validate(pack, engine) == []

    declaration["bindings"]["roles"].pop("evidence_item")
    declaration["bindings"]["roles"]["decision_record"][0]["subject_field"] = "not_in_schema"
    declaration["bindings"]["roles"]["decision_record"][0]["operations"]["refresh"] = \
        "missing-operation"
    declaration["bindings"]["roles"]["decision_record"][0]["unexpected"] = "value"
    declaration["bindings"]["roles"]["unknown_role"] = []
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))
    errors = module.validate(pack, engine)
    assert any("bindings.roles.evidence_item requires at least 1" in error for error in errors)
    assert any("not_in_schema" in error for error in errors)
    assert any("missing-operation" in error for error in errors)
    assert any("unknown key(s): ['unexpected']" in error for error in errors)
    assert any("unknown role(s): ['unknown_role']" in error for error in errors)


def test_profile_rejects_unindexed_class_and_missing_surface(tmp_path):
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    del declaration["surfaces"]["assessment_review"]
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))
    state_path = pack / ".okengine" / "extensions.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["enabled"]["okengine.reevaluation"]["config"]["proposition_types"] = "forecast"
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    module = _load(SCRIPT, "application_profiles_invalid")
    errors = module.validate(pack, REPO)
    assert any("assessment_review is required" in error for error in errors)
    assert any("omits bound type(s): ['diagnostic']" in error for error in errors)


def test_profile_rejects_unknown_operation_and_non_schema_field(tmp_path):
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    binding = declaration["bindings"]["propositions"][0]
    binding["confidence_field"] = "certainty_not_in_contract"
    binding["operations"]["resolve"] = "missing-operation"
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))

    module = _load(SCRIPT, "application_profiles_bad_binding")
    errors = module.validate(pack, REPO)
    assert any("certainty_not_in_contract" in error for error in errors)
    assert any("unknown operation 'missing-operation'" in error for error in errors)


def test_profile_accepts_engine_inherited_prediction_type(tmp_path):
    """A pack must not redeclare the core prediction type merely to bind an application."""
    pack = tmp_path / "pack"
    shutil.copytree(FIXTURE, pack)
    schema_path = pack / "schema.yaml"
    schema = yaml.safe_load(schema_path.read_text())
    del schema["types"]["forecast"]
    schema["common_optional"] = ["evidence", "outcome", "needs_review"]
    schema_path.write_text(yaml.safe_dump(schema, sort_keys=False))
    declaration_path = pack / ".okengine" / "application.yaml"
    declaration = yaml.safe_load(declaration_path.read_text())
    binding = declaration["bindings"]["propositions"][0]
    binding.update({
        "type": "prediction", "namespace": "predictions",
        "resolution_field": "outcome", "review_field": "needs_review",
    })
    declaration_path.write_text(yaml.safe_dump(declaration, sort_keys=False))
    state_path = pack / ".okengine" / "extensions.yaml"
    state = yaml.safe_load(state_path.read_text())
    state["enabled"]["okengine.reevaluation"]["config"]["proposition_types"] = \
        "prediction,diagnostic"
    state_path.write_text(yaml.safe_dump(state, sort_keys=False))

    module = _load(SCRIPT, "application_profiles_inherited")
    assert module.validate(pack, REPO) == []


def test_two_class_dependency_and_closed_lifecycle_proof(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    shutil.copytree(FIXTURE / "wiki", vault / "wiki")
    monkeypatch.setenv("OKENGINE_REEVAL_TYPES", "forecast,diagnostic")
    monkeypatch.setenv("OKENGINE_REEVAL_OPEN_STATUSES", "open,active,disputed")
    edge = _load(REPO / "extensions" / "okengine.reevaluation" / "edge_index.py", "che_edges")
    artifact = edge.build(vault)

    assert artifact["proposition_count"] == 2
    assert [row["page"] for row in artifact["edges"]["sources/report-a"]] == ["forecasts/f1"]
    assert [row["page"] for row in artifact["edges"]["sources/report-b"]] == ["diagnostics/d1"]
    changed = {"sources/report-a"}
    affected = {
        row["page"]
        for source in changed
        for row in artifact["edges"].get(source, [])
    }
    assert affected == {"forecasts/f1"}, "changed evidence must not create a global cross-join"

    lifecycle = yaml.safe_load((FIXTURE / "lifecycle.yaml").read_text())
    app = _load(SCRIPT, "application_profiles_lifecycle")
    assert app.validate_lifecycle_record(lifecycle, {"forecast", "diagnostic"}) == []
    assert lifecycle["assessment_change"]["prior"] != lifecycle["assessment_change"]["new"]

    lifecycle["review"]["status"] = "pending"
    errors = app.validate_lifecycle_record(lifecycle, {"forecast", "diagnostic"})
    assert any("explicitly approved" in error for error in errors)
