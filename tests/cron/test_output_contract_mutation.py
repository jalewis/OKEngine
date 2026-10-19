"""Behavioral contracts of output-contract validation and composition the original suite left
unpinned: exact error lists, value equality (contracts arrive parsed from JSON, so their strings are
not the interned literals in this module), composition order, and the floor's required path."""
from pathlib import Path
import importlib.util
import json

import pytest

MOD = Path(__file__).parents[2] / "scripts" / "cron" / "output_contract.py"
spec = importlib.util.spec_from_file_location("output_contract_mutation", MOD)
oc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oc)


def contract(**overrides):
    value = {
        "api": 1,
        "allowed_namespaces": ["sources"],
        "allowed_types": ["source"],
        "operations": ["create", "update"],
        "required_fields": ["type"],
        "required_relationships": [],
        "body": {"required": True, "min_non_whitespace": 0},
        "unknown_fields": "review",
        "unresolved_links": "review",
        "placeholder_links": "review",
        "completion": "run",
    }
    value.update(overrides)
    return value


def parsed(value: dict) -> dict:
    """The contract exactly as a deployment loads it: from JSON text, with fresh strings."""
    return json.loads(json.dumps(value))


# --- validate -----------------------------------------------------------------------------------

def test_a_parsed_run_contract_may_name_its_required_path():
    assert oc.validate(parsed(contract(required_write_path="briefings/{date}.md"))) == []


def test_a_required_path_without_a_completion_reports_both_problems():
    value = contract(required_write_path="briefings/x.md")
    del value["completion"]
    assert oc.validate(value) == [
        "output_contract.completion must be one of ['per-selected-item', 'run']",
        "output_contract.required_write_path requires completion=run",
    ]


def test_an_unknown_completion_with_a_required_path_reports_both_problems():
    assert oc.validate(contract(completion="aaa", required_write_path="x.md")) == [
        "output_contract.completion must be one of ['per-selected-item', 'run']",
        "output_contract.required_write_path requires completion=run",
    ]


def test_a_body_minimum_needs_a_body_that_may_be_required_or_absent():
    assert oc.validate(contract(body={"min_non_whitespace": 5})) == []
    assert oc.validate(contract(body={"required": True, "min_non_whitespace": 5})) == []
    assert oc.validate(contract(body={"required": False, "min_non_whitespace": 5})) == [
        "output_contract.body cannot set a minimum while required is false"]


def test_a_non_boolean_required_is_reported_once():
    assert oc.validate(contract(body={"required": 0, "min_non_whitespace": 5})) == [
        "output_contract.body.required must be boolean"]


@pytest.mark.parametrize("optional", ["x", "xy"])
def test_a_scalar_optional_relationships_is_a_shape_error_not_an_overlap(optional):
    assert oc.validate(contract(required_relationships=["x"],
                                optional_relationships=optional)) == [
        "output_contract.optional_relationships must be a list of non-empty strings"]


def test_a_scalar_required_relationships_is_a_shape_error_not_an_overlap():
    assert oc.validate(contract(required_relationships="x",
                                optional_relationships=["x"])) == [
        "output_contract.required_relationships must be a list of non-empty strings"]


# --- compose: allowed sets ------------------------------------------------------------------------

def test_narrowing_keeps_the_floors_order():
    floor = contract(allowed_namespaces=["a", "b", "c"])
    policy = contract(allowed_namespaces=["c", "b"])
    assert oc.compose(floor, policy)["allowed_namespaces"] == ["b", "c"]


# --- compose: enforcement ranks -------------------------------------------------------------------

def test_allow_may_not_weaken_a_review_floor():
    with pytest.raises(ValueError, match="unknown_fields policy may not weaken"):
        oc.compose(contract(unknown_fields="review"), contract(unknown_fields="allow"))


def test_review_may_not_weaken_a_reject_floor():
    with pytest.raises(ValueError, match="placeholder_links policy may not weaken"):
        oc.compose(contract(placeholder_links="reject"), contract(placeholder_links="review"))


def test_allow_may_tighten_to_review():
    out = oc.compose(contract(unresolved_links="allow"), contract(unresolved_links="review"))
    assert out["unresolved_links"] == "review"


# --- compose: body minima -----------------------------------------------------------------------

def test_a_policy_without_a_minimum_weakens_a_floor_minimum():
    with pytest.raises(ValueError, match="min_non_whitespace policy may not weaken"):
        oc.compose(contract(body={"required": True, "min_non_whitespace": 1}),
                   contract(body={"required": True}))


def test_a_policy_without_a_minimum_matches_a_zero_floor():
    out = oc.compose(contract(body={"required": True, "min_non_whitespace": 0}),
                     contract(body={"required": True}))
    assert out["body"] == {"required": True, "min_non_whitespace": 0}


def test_a_floor_without_a_minimum_accepts_a_zero_policy():
    out = oc.compose(contract(body={"required": True}),
                     contract(body={"required": True, "min_non_whitespace": 0}))
    assert out["body"] == {"required": True, "min_non_whitespace": 0}


def test_no_minimum_on_either_side_composes_to_zero():
    out = oc.compose(contract(body={"required": True}), contract(body={"required": True}))
    assert out["body"] == {"required": True, "min_non_whitespace": 0}


# --- compose: required write path ---------------------------------------------------------------

def test_the_floors_required_path_survives_a_policy_without_one():
    out = oc.compose(contract(required_write_path="briefings/{date}.md"), contract())
    assert out["required_write_path"] == "briefings/{date}.md"


def test_a_policy_may_add_a_required_path_the_floor_lacks():
    out = oc.compose(contract(), contract(required_write_path="briefings/{date}.md"))
    assert out["required_write_path"] == "briefings/{date}.md"


def test_no_required_path_on_either_side_leaves_the_key_out():
    assert "required_write_path" not in oc.compose(contract(), contract())


def test_an_equal_parsed_path_is_not_a_replacement():
    floor = parsed(contract(required_write_path="briefings/{date}.md"))
    policy = parsed(contract(required_write_path="briefings/{date}.md"))
    assert floor["required_write_path"] is not policy["required_write_path"]
    assert oc.compose(floor, policy)["required_write_path"] == "briefings/{date}.md"


@pytest.mark.parametrize("floor_path, policy_path", [("a.md", "z.md"), ("z.md", "a.md")])
def test_a_different_path_replaces_the_floor_in_either_order(floor_path, policy_path):
    with pytest.raises(ValueError, match="required_write_path policy may not replace"):
        oc.compose(contract(required_write_path=floor_path),
                   contract(required_write_path=policy_path))
