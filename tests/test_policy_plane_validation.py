"""Validation coverage for the policy plane (okengine#462, tranche T1).

`tools/policy_plane.py` declares what each actor is allowed to write: operations,
paths, types, which fields may be updated and which are protected. `validate_*`
is the gate that stops a malformed or self-contradictory policy from being loaded
in the first place — a policy that fails to reject is a policy that silently
grants.

These assert each rejection fires, including the two internal-consistency checks
that catch a policy which contradicts itself (a field both updatable and
protected; a required field outside the updatable set).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "tools" / "policy_plane.py"


def load_pp():
    spec = importlib.util.spec_from_file_location("policy_plane_validation", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def pp():
    return load_pp()


def capability(**overrides):
    value = {
        "rule_id": "cap-1",
        "operations": ["create", "update"],
        "paths": ["entities/**"],
        "types": ["actor"],
        "update_fields": ["title", "aliases"],
        "required_fields": ["title"],
        "protected_fields": ["id"],
        "body": "allow",
    }
    value.update(overrides)
    return value


# ── load_document ──────────────────────────────────────────────────────────────

def test_load_document_rejects_unreadable_and_malformed_policies(pp, tmp_path):
    with pytest.raises(pp.PolicyError, match="cannot load policy"):
        pp.load_document(tmp_path / "absent.yaml")

    broken = tmp_path / "broken.yaml"
    broken.write_text("rules: [unclosed\n", encoding="utf-8")
    with pytest.raises(pp.PolicyError, match="cannot load policy"):
        pp.load_document(broken)


def test_load_document_requires_a_mapping(pp, tmp_path):
    listy = tmp_path / "listy.yaml"
    listy.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(pp.PolicyError, match="must be a mapping"):
        pp.load_document(listy)


def test_load_document_treats_an_empty_file_as_an_empty_policy(pp, tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert pp.load_document(empty) == {}


# ── validate_capability ────────────────────────────────────────────────────────

def test_valid_capability_has_no_errors(pp):
    assert pp.validate_capability("importer", capability()) == []


def test_capability_must_be_a_mapping(pp):
    assert pp.validate_capability("importer", ["nope"]) == \
        ["capability importer: must be a mapping"]


def test_capability_rejects_unknown_keys(pp):
    errors = pp.validate_capability("importer", capability(vibes="high"))
    assert any("unknown keys" in e and "vibes" in e for e in errors)


def test_capability_list_fields_must_be_lists_of_nonempty_strings(pp):
    for key in ("operations", "paths", "types", "update_fields",
                "required_fields", "protected_fields"):
        errors = pp.validate_capability("importer", capability(**{key: "not-a-list"}))
        assert any(f"{key}: must be a list of non-empty strings" in e for e in errors), key
        errors = pp.validate_capability("importer", capability(**{key: ["ok", ""]}))
        assert any(f"{key}: must be a list of non-empty strings" in e for e in errors), key


def test_capability_rejects_unknown_operations_and_body_modes(pp):
    errors = pp.validate_capability("importer", capability(operations=["create", "yeet"]))
    assert any("unknown operations" in e and "yeet" in e for e in errors)

    errors = pp.validate_capability("importer", capability(body="maybe"))
    assert any("body: must be one of" in e for e in errors)


def test_capability_requires_a_stable_rule_id(pp):
    for bad in (None, "", 7):
        value = capability()
        if bad is None:
            value.pop("rule_id")
        else:
            value["rule_id"] = bad
        errors = pp.validate_capability("importer", value)
        assert any("rule_id: required stable rule ID" in e for e in errors), bad


def test_capability_rejects_self_contradictory_field_sets(pp):
    """A field cannot be both updatable and protected, and a required field must be
    within the updatable set — otherwise the policy can never be satisfied."""
    errors = pp.validate_capability("importer", capability(
        update_fields=["title", "id"], protected_fields=["id"]))
    assert any("both allowed and protected" in e and "id" in e for e in errors)

    errors = pp.validate_capability("importer", capability(
        update_fields=["title"], required_fields=["title", "origin"]))
    assert any("required fields are not allowed" in e and "origin" in e for e in errors)


# ── validate_document ──────────────────────────────────────────────────────────

def rule(**overrides):
    value = {
        "id": "rule-1",
        "severity": "high",
        "evaluator": sorted(load_pp().EVALUATORS)[0],
        "enforcement": [sorted(load_pp().ENFORCEMENT_POINTS)[0]],
    }
    value.update(overrides)
    return value


def test_document_requires_the_current_schema_version(pp):
    errors = pp.validate_document({"schema_version": 999, "rules": []})
    assert any("schema_version must be" in e for e in errors)


def test_document_requires_rules_to_be_a_list(pp):
    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION, "rules": {}})
    assert any("rules must be a list" in e for e in errors)


def test_document_rejects_non_mapping_rules(pp):
    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION, "rules": ["nope"]})
    assert any("rules[0] must be a mapping" in e for e in errors)


def test_document_rejects_missing_and_duplicate_rule_ids(pp):
    errors = pp.validate_document(
        {"schema_version": pp.SCHEMA_VERSION, "rules": [rule(id=""), rule(id=7)]})
    assert any(".id must be a non-empty string" in e for e in errors)

    errors = pp.validate_document(
        {"schema_version": pp.SCHEMA_VERSION, "rules": [rule(), rule()]})
    assert any("duplicate rule ID rule-1" in e for e in errors)


def test_document_rejects_unknown_severity_evaluator_and_enforcement(pp):
    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION,
                                   "rules": [rule(severity="apocalyptic")]})
    assert any(".severity must be one of" in e for e in errors)

    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION,
                                   "rules": [rule(evaluator="vibes")]})
    assert any(".evaluator unknown" in e for e in errors)

    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION,
                                   "rules": [rule(enforcement="everywhere")]})
    assert any(".enforcement must be a non-empty string list" in e for e in errors)

    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION,
                                   "rules": [rule(enforcement=["nowhere-real"])]})
    assert any("unknown targets" in e for e in errors)


def test_document_missing_required_rule_keys_is_reported(pp):
    errors = pp.validate_document({"schema_version": pp.SCHEMA_VERSION, "rules": [{"id": "r"}]})
    assert any("missing" in e for e in errors)
