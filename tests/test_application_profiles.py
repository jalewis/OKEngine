from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.contract


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


def test_profile_manifest_reports_all_malformed_contract_shapes():
    app = _load(SCRIPT, "application_profiles_manifest_edges")
    malformed = {
        "schema_version": 2,
        "id": "Bad_Id",
        "version": "v1",
        "extends": {
            "profile": "Bad Parent",
            "version": "1.0.0",
            "extra": True,
        },
        "requires": [],
        "binding_contract": {
            "minimum_proposition_classes": 0,
            "required_fields": [],
            "required_operations": "run",
            "required_roles": {
                "Bad-Role": {
                    "minimum": 0,
                    "required_fields": ["x", "x", 3],
                    "required_operations": ["", ""],
                    "allow_multiple": "yes",
                    "unknown": True,
                },
                "scalar": "bad",
            },
        },
        "operating_loop": [
            {"id": "later", "after": ["missing"]},
            {"id": "later", "after": "not-list"},
            "bad",
        ],
        "required_surfaces": ["same", "same"],
        "required_queues": [],
        "required_success_measures": "measure",
        "policy": [],
        "unknown": True,
    }
    errors = app.validate_profile_manifest(malformed)
    joined = "\n".join(errors)
    for expected in (
        "unknown profile key",
        "schema_version",
        "lowercase kebab-case",
        "semantic version",
        "unknown extends",
        "requires.extensions",
        "positive integer",
        "required_fields",
        "required_operations",
        "role id",
        "allow_multiple",
        "operating_loop",
        "required_surfaces",
        "required_queues",
        "required_success_measures",
        "policy",
    ):
        assert expected in joined

    inherited = {
        "schema_version": 1,
        "id": "child",
        "version": "1.0.0",
        "extends": {"profile": "parent", "version": ">=1.0.0"},
    }
    assert app.validate_profile_manifest(inherited, allow_inherited=True) == []


def test_profile_merge_rejects_weakened_roles_and_bad_floors():
    app = _load(SCRIPT, "application_profiles_merge_edges")
    with pytest.raises(app.ApplicationProfileError, match="semantic-version floors"):
        app._merge_floor("bad", ">=1.0.0", "requires.engine")
    assert app._merge_floor(">=2.0.0", ">=1.0.0", "x") == ">=2.0.0"
    assert app._merge_floor(">=1.0.0", ">=2.0.0", "x") == ">=2.0.0"
    with pytest.raises(app.ApplicationProfileError, match="cannot weaken"):
        app._merge_role_contract(
            {"allow_multiple": False},
            {"allow_multiple": True},
            "child",
            "evidence",
        )
    merged = app._merge_requires(
        {"engine": ">=1.0.0", "extensions": {"a": ">=1.0.0"}},
        {"engine": ">=2.0.0", "extensions": {"a": ">=0.5.0", "b": ">=1.0.0"}},
        "child",
    )
    assert merged["engine"] == ">=2.0.0"
    assert merged["extensions"]["a"] == ">=1.0.0"
    assert merged["extensions"]["b"] == ">=1.0.0"


def test_lifecycle_validator_reports_each_missing_governance_fact():
    app = _load(SCRIPT, "application_profiles_lifecycle_edges")
    assert app.validate_lifecycle_record([], {"forecast"}) == [
        "lifecycle record must be a mapping"
    ]
    errors = app.validate_lifecycle_record(
        {
            "proposition_type": "other",
            "changed_evidence": [3, ""],
            "assessment_change": {
                "prior": "same",
                "new": "same",
                "cause": "missing",
                "evidence": [],
            },
            "review": {"required": False, "status": "pending"},
            "resolution": {},
            "learning": {},
        },
        {"forecast"},
    )
    joined = "\n".join(errors)
    for expected in (
        "lifecycle record missing proposition",
        "unbound proposition_type",
        "changed_evidence",
        "assessment_change.evaluator",
        "distinct prior and new",
        "cause must name changed evidence",
        "evidence must include",
        "required review boundary",
        "explicitly approved",
        "resolution",
        "learning",
    ):
        assert expected in joined


def test_declaration_validator_accumulates_binding_and_role_errors(
    tmp_path, monkeypatch
):
    app = _load(SCRIPT, "application_profiles_declaration_edges")
    profile = {
        "id": "profile",
        "version": "1.2.0",
        "requires": {"extensions": {"ext.required": ">=2.0.0"}},
        "binding_contract": {
            "minimum_proposition_classes": 2,
            "required_fields": ["type", "namespace", "status_field", "confidence_field"],
            "required_operations": ["refresh", "resolve"],
            "required_roles": {
                "evidence": {
                    "minimum": 1,
                    "required_fields": [
                        "type",
                        "namespace",
                        "evidence_field",
                    ],
                    "required_operations": ["refresh"],
                    "allow_multiple": False,
                }
            },
        },
        "required_surfaces": ["surface"],
        "required_queues": ["queue"],
        "required_success_measures": ["measure"],
    }
    declaration = {
        "profile": "profile",
        "profile_version": "bad",
        "bindings": {
            "unknown": True,
            "propositions": [
                "scalar",
                {
                    "type": "missing-type",
                    "namespace": "missing-namespace",
                    "status_field": "missing-field",
                    "open_values": ["same"],
                    "resolved_values": ["same"],
                    "operations": {
                        "refresh": "missing-operation",
                    },
                },
            ],
            "roles": {
                "unknown": [],
                "evidence": [
                    "scalar",
                    {
                        "type": "missing-type",
                        "namespace": "missing-namespace",
                        "evidence_field": "missing-field",
                        "provided_by": "missing-pack",
                        "operations": {
                            "refresh": "missing-operation",
                            "extra": "other",
                        },
                        "extra": True,
                    },
                    {
                        "type": "missing-type",
                        "namespace": "missing-namespace",
                        "evidence_field": "missing-field",
                    },
                ],
            },
        },
        "surfaces": {},
        "queues": "bad",
        "success_measures": {},
        "unknown": True,
    }
    monkeypatch.setattr(app, "load_declaration", lambda _pack: declaration)
    monkeypatch.setattr(app, "load_profile", lambda *_args: profile)
    monkeypatch.setattr(
        app,
        "_enabled_extensions",
        lambda *_args: (
            {
                "versions": {"ext.required": "bad"},
                "state": {"okengine.reevaluation": {"config": {"proposition_types": ""}}},
                "records": [],
            },
            ["resolution failed"],
        ),
    )
    monkeypatch.setattr(
        app,
        "_schema_parts",
        lambda *_args: ({"known": {}}, {"known"}, {"known-field"}),
    )
    monkeypatch.setattr(app, "_operations", lambda *_args: {"known-operation"})
    monkeypatch.setattr(app, "_required_packs", lambda *_args: {"known-pack"})

    errors = app.validate(tmp_path, tmp_path)
    joined = "\n".join(errors)
    for expected in (
        "unknown application declaration",
        "profile_version",
        "extension resolution",
        "enabled version",
        "bindings unknown",
        "must be a mapping",
        "missing required field",
        "not declared in schema.types",
        "not declared in schema partitioning",
        "missing-field",
        "must not overlap",
        "operations.resolve is required",
        "unknown role",
        "permits only one",
        "provided_by",
        "unknown key",
        "unknown operation",
        "surfaces.surface is required",
        "queues must be a mapping",
        "success_measures.measure is required",
        "omits bound type",
    ):
        assert expected in joined


def test_declaration_validation_handles_resolution_and_schema_exceptions(
    tmp_path, monkeypatch
):
    app = _load(SCRIPT, "application_profiles_declaration_exceptions")
    monkeypatch.setattr(
        app,
        "load_declaration",
        lambda _pack: {
            "profile": "profile",
            "profile_version": "1.0.0",
            "bindings": {"propositions": [], "roles": {}},
            "surfaces": {},
            "queues": {},
            "success_measures": {},
        },
    )
    monkeypatch.setattr(
        app,
        "load_profile",
        lambda *_args: {
            "id": "profile",
            "version": "2.0.0",
            "requires": {"extensions": {}},
            "binding_contract": {
                "minimum_proposition_classes": 1,
                "required_fields": [],
                "required_operations": [],
            },
        },
    )
    monkeypatch.setattr(
        app,
        "_enabled_extensions",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("extension crash")),
    )
    monkeypatch.setattr(
        app,
        "_schema_parts",
        lambda *_args: (_ for _ in ()).throw(app.ApplicationProfileError("schema bad")),
    )
    errors = app.validate(tmp_path, tmp_path)
    assert any("extension crash" in error for error in errors)
    assert errors[-1] == "schema bad"


def test_profile_loader_and_inheritance_edge_paths(tmp_path):
    app = _load(SCRIPT, "application_profiles_loader_branch_edges")
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("- not\n- a mapping\n")
    with pytest.raises(app.ApplicationProfileError, match="top level must be a mapping"):
        app._load(scalar)
    with pytest.raises(app.ApplicationProfileError, match="cannot load YAML"):
        app._load(tmp_path / "missing.yaml")
    with pytest.raises(app.ApplicationProfileError, match="invalid application profile id"):
        app.load_profile("Bad Id", tmp_path)
    with pytest.raises(app.ApplicationProfileError, match="unknown application profile"):
        app.load_profile("missing", tmp_path)

    profile = tmp_path / "applications" / "child" / "application.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text("schema_version: 1\nid: wrong\nversion: 1.0.0\n")
    with pytest.raises(app.ApplicationProfileError, match="does not match directory"):
        app.load_profile("child", tmp_path)

    profile.write_text("schema_version: 2\nid: child\nversion: bad\n")
    with pytest.raises(app.ApplicationProfileError, match="schema_version"):
        app.load_profile("child", tmp_path)


def test_merge_helpers_take_additive_and_idempotent_branches():
    app = _load(SCRIPT, "application_profiles_merge_more_edges")
    assert app._merge_requires({}, {"other": "value"}, "child") == {"other": "value"}
    role = app._merge_role_contract(
        {"minimum": 1, "required_fields": ["type"], "required_operations": []},
        {"minimum": 2, "required_fields": ["namespace"],
         "required_operations": ["refresh"], "allow_multiple": False},
        "child", "evidence")
    assert role == {
        "minimum": 2,
        "required_fields": ["type", "namespace"],
        "required_operations": ["refresh"],
        "allow_multiple": False,
    }
    binding = app._merge_binding_contract({}, {
        "minimum_proposition_classes": 2,
        "required_fields": ["type"],
        "required_operations": ["refresh"],
        "required_roles": {"evidence": {"minimum": 1}},
    }, "child")
    assert binding["minimum_proposition_classes"] == 2
    assert "evidence" in binding["required_roles"]

    parent = {
        "id": "parent", "requires": {}, "binding_contract": {},
        "operating_loop": [{"id": "same", "after": []}],
    }
    child = {"id": "child", "operating_loop": [{"id": "same", "after": []}]}
    assert app._merge_profiles(parent, child)["operating_loop"] == [
        {"id": "same", "after": []}]


def test_manifest_validator_remaining_shape_branches():
    app = _load(SCRIPT, "application_profiles_manifest_more_edges")
    profile = {
        "schema_version": 1,
        "id": "child",
        "version": "1.0.0",
        "extends": "parent",
        "requires": {"extensions": {}},
        "binding_contract": {
            "minimum_proposition_classes": 1,
            "required_fields": ["type"],
            "required_operations": ["refresh"],
            "required_roles": [],
        },
        "operating_loop": [],
        "required_surfaces": ["surface"],
        "required_queues": ["queue"],
        "required_success_measures": ["measure"],
        "policy": {},
    }
    joined = "\n".join(app.validate_profile_manifest(profile))
    assert "extends must be a mapping" in joined
    assert "required_roles must be a mapping" in joined
    assert "operating_loop must be a non-empty list" in joined

    profile["extends"] = {
        "profile": "child", "version": ">=1.0.0"}
    profile["binding_contract"]["required_roles"] = {}
    profile["operating_loop"] = [
        {"id": "same", "after": []}, {"id": "same", "after": []}]
    joined = "\n".join(app.validate_profile_manifest(profile))
    assert "cannot reference itself" in joined
    assert "stage ids must be unique" in joined

    profile["operating_loop"] = [{"id": "first", "after": ["later"]}]
    assert any("later/unknown" in e for e in app.validate_profile_manifest(profile))


def test_schema_operations_and_required_pack_edge_paths(tmp_path):
    app = _load(SCRIPT, "application_profiles_support_more_edges")
    engine = tmp_path / "engine"
    pack = tmp_path / "pack"
    (engine / "config").mkdir(parents=True)
    pack.mkdir()
    (engine / "config/base-schema.yaml").write_text(
        "types: []\nokf:\n  namespaces: [base]\ncommon_optional: [shared]\n")
    (pack / "schema.yaml").write_text(
        "types:\n  scalar: nope\nokf:\n  namespaces: [pack]\n"
        "field_shapes: {shape: scalar}\n")
    types, namespaces, fields = app._schema_parts(pack, engine)
    assert types == {"scalar": "nope"}
    assert namespaces == {"base", "pack"}
    assert {"shared", "shape"} <= fields

    assert app._operations(pack, [
        {"id": "ext", "manifest": {"operations": []}}]) == set()
    crons = pack / "crons/domain-crons.json"
    crons.parent.mkdir()
    crons.write_text("{bad json")
    assert app._operations(pack, []) == set()
    crons.write_text('{"jobs": [{"id": "job"}, "bad", {}]}')
    assert app._operations(pack, []) == {"job"}

    assert app._required_packs(pack) == set()
    (pack / "pack.yaml").write_text("requires: scalar\n")
    assert app._required_packs(pack) == set()
    (pack / "pack.yaml").write_text("requires: [ext:one, 3, pack-a@1.0.0]\n")
    assert app._required_packs(pack) == {"pack-a"}
    (pack / "pack.yaml").write_text("[broken\n")
    assert app._required_packs(pack) == set()
    assert app.validate(tmp_path / "no-declaration", engine) == []


def test_lifecycle_non_mapping_nested_sections():
    app = _load(SCRIPT, "application_profiles_lifecycle_nested_edges")
    record = {
        "proposition": "p",
        "proposition_type": "forecast",
        "changed_evidence": ["source"],
        "assessment_change": [],
        "review": [],
        "resolution": {},
        "learning": {},
    }
    errors = app.validate_lifecycle_record(record, {"forecast"})
    assert "assessment_change must be a mapping" in errors
    assert "review must be a mapping" in errors


def test_manifest_operating_loop_rejects_non_list_dependency():
    app = _load(SCRIPT, "application_profiles_loop_after_edge")
    profile = {
        "schema_version": 1,
        "id": "profile",
        "version": "1.0.0",
        "requires": {"extensions": {}},
        "binding_contract": {
            "minimum_proposition_classes": 1,
            "required_fields": ["type"],
            "required_operations": ["refresh"],
        },
        "operating_loop": [{"id": "one", "after": "not-a-list"}],
        "required_surfaces": ["surface"],
        "required_queues": ["queue"],
        "required_success_measures": ["measure"],
        "policy": {},
    }
    assert "operating_loop one.after must be a list" in app.validate_profile_manifest(profile)


def test_declaration_validator_rejects_container_and_binding_shapes(tmp_path, monkeypatch):
    app = _load(SCRIPT, "application_profiles_binding_shape_edges")
    profile = {
        "id": "profile",
        "version": "1.0.0",
        "requires": {"extensions": {}},
        "binding_contract": {
            "minimum_proposition_classes": 2,
            "required_fields": ["type"],
            "required_operations": [],
            "required_roles": {
                "evidence": {
                    "minimum": 1,
                    "required_fields": ["type", "namespace", "evidence_field"],
                    "required_operations": ["refresh"],
                },
            },
        },
    }
    declaration = {
        "profile": "profile",
        "profile_version": "1.0.0",
        "bindings": "bad",
        "surfaces": {},
        "queues": {},
        "success_measures": {},
    }
    monkeypatch.setattr(app, "load_declaration", lambda _pack: declaration)
    monkeypatch.setattr(app, "load_profile", lambda *_args: profile)
    monkeypatch.setattr(
        app, "_enabled_extensions",
        lambda *_args: ({"versions": {}, "state": {}, "records": []}, []),
    )
    monkeypatch.setattr(app, "_schema_parts", lambda *_args: ({"kind": {}}, {"items"}, {"evidence"}))
    monkeypatch.setattr(app, "_operations", lambda *_args: {"refresh"})
    monkeypatch.setattr(app, "_required_packs", lambda *_args: set())

    def reject_profile(*_args):
        raise app.ApplicationProfileError("profile unavailable")

    monkeypatch.setattr(app, "load_profile", reject_profile)
    assert app.validate(tmp_path, tmp_path) == ["profile unavailable"]
    monkeypatch.setattr(app, "load_profile", lambda *_args: profile)

    errors = app.validate(tmp_path, tmp_path)
    assert "bindings.propositions must be a list" in errors
    assert "at least 2 proposition binding(s) required" in errors
    assert "bindings.roles.evidence requires at least 1 binding(s); found 0 (required by profile)" in errors

    declaration["bindings"] = {
        "propositions": [
            {
                "type": "kind", "namespace": "items",
                "open_values": [], "resolved_values": ["done"], "operations": "bad",
            },
            {
                "type": "kind", "namespace": "items",
                "open_values": ["open"], "resolved_values": [], "operations": {},
            },
        ],
        "roles": "bad",
    }
    errors = app.validate(tmp_path, tmp_path)
    joined = "\n".join(errors)
    assert "bindings.propositions[0].open_values must be a non-empty list" in errors
    assert "bindings.propositions[0].operations must be a mapping" in errors
    assert "bindings.propositions[1].type duplicates 'kind'" in errors
    assert "bindings.propositions[1].resolved_values must be a non-empty list" in errors
    assert "bindings.roles must be a mapping" in errors

    declaration["bindings"]["roles"] = {
        "evidence": [{
            "type": "kind", "namespace": "items", "operations": "bad",
        }],
    }
    errors = app.validate(tmp_path, tmp_path)
    assert "bindings.roles.evidence[0] missing required field(s): ['evidence_field']" in errors
    assert "bindings.roles.evidence[0].operations must be a mapping" in errors
