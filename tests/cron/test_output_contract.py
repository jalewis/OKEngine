from pathlib import Path
import hashlib
import importlib.util
import json
import sys


MOD = Path(__file__).parents[2] / "scripts" / "cron" / "output_contract.py"
spec = importlib.util.spec_from_file_location("output_contract", MOD)
oc = importlib.util.module_from_spec(spec)
sys.modules["output_contract"] = oc
spec.loader.exec_module(oc)


def contract(**overrides):
    value = {
        "api": 1,
        "allowed_namespaces": ["sources"],
        "allowed_types": ["source"],
        "operations": ["create", "update"],
        "required_fields": ["type", "raw"],
        "required_relationships": [],
        "body": {"required": True, "min_non_whitespace": 80},
        "unknown_fields": "review",
        "unresolved_links": "review",
        "placeholder_links": "reject",
        "completion": "per-selected-item",
    }
    value.update(overrides)
    return value


def test_valid_contract():
    assert oc.validate(contract()) == []
    assert oc.digest(contract()) == oc.digest({**contract()})
    assert oc.digest(contract()) != oc.digest(contract(allowed_types=["entity"]))


def test_optional_relationship_contract_is_valid_and_cannot_overlap_required():
    assert oc.validate(contract(optional_relationships=["prediction_candidate"])) == []
    assert any("optional_relationships must be a list" in error for error in
               oc.validate(contract(optional_relationships="prediction_candidate")))
    assert any("contains duplicates" in error for error in
               oc.validate(contract(optional_relationships=["candidate", "candidate"])))
    assert any("overlaps required_relationships" in error for error in
               oc.validate(contract(required_relationships=["candidate"],
                                    optional_relationships=["candidate"])))


def test_required_write_path_is_run_mode_and_wiki_relative():
    assert oc.validate(contract(completion="run",
                                required_write_path="briefings/daily-{date}.md")) == []
    assert any("requires completion=run" in error for error in
               oc.validate(contract(required_write_path="briefings/daily-{date}.md")))
    assert any("wiki-relative" in error for error in
               oc.validate(contract(completion="run", required_write_path="../escape.md")))
    assert any("non-empty string" in error for error in
               oc.validate(contract(completion="run", required_write_path="")))


def test_digest_is_exactly_sorted_compact_utf8_json():
    value = {"z": "é", "a": 1}
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    assert oc.digest(value) == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert oc.digest(value) == oc.digest({"a": 1, "z": "é"})


def test_unknown_version_key_and_bad_body_fail_loud():
    bad = contract(api=2, surprise=True, body={"required": False, "min_non_whitespace": 10})
    errors = oc.validate(bad)
    assert any("unknown key" in e for e in errors)
    assert any("api must be 1" in e for e in errors)
    assert any("cannot set a minimum" in e for e in errors)


def test_validation_reports_only_actual_unknown_keys_and_exact_version_equality():
    errors = oc.validate(contract(surprise=True))
    assert errors == ["output_contract has unknown key(s): ['surprise']"]
    assert oc.validate(contract(api=0)) == ["output_contract.api must be 1"]
    # Python numeric equality is the declared API comparison; object identity is not.
    assert oc.validate(contract(api=1.0)) == []
    assert not any("unknown key" in error for error in oc.validate({"api": 1}))


def test_pack_policy_can_tighten_floor():
    floor = contract(allowed_namespaces=["sources", "entities"],
                     allowed_types=["source", "entity"], unknown_fields="review",
                     body={"required": True, "min_non_whitespace": 40})
    policy = contract(allowed_namespaces=["sources"], allowed_types=["source"],
                      operations=["create"], required_fields=["publisher"],
                      unknown_fields="reject", unresolved_links="reject",
                      body={"required": True, "min_non_whitespace": 100})
    effective = oc.compose(floor, policy)
    assert effective["allowed_namespaces"] == ["sources"]
    assert effective["operations"] == ["create"]
    assert effective["required_fields"] == ["type", "raw", "publisher"]
    assert effective["unknown_fields"] == "reject"
    assert effective["body"]["min_non_whitespace"] == 100


def test_optional_relationships_compose_without_weakening_the_floor():
    floor = contract(optional_relationships=["prediction_candidate"])
    policy = contract(optional_relationships=["evidence_candidate"])
    effective = oc.compose(floor, policy)
    assert effective["optional_relationships"] == ["prediction_candidate", "evidence_candidate"]
    assert "optional_relationships" not in oc.compose(contract(), contract())
    promoted = oc.compose(floor, contract(required_relationships=["prediction_candidate"]))
    assert promoted["required_relationships"] == ["prediction_candidate"]
    assert "optional_relationships" not in promoted
    stronger_floor = oc.compose(contract(required_relationships=["prediction_candidate"]),
                                contract(optional_relationships=["prediction_candidate"]))
    assert stronger_floor["required_relationships"] == ["prediction_candidate"]
    assert "optional_relationships" not in stronger_floor


def test_pack_policy_cannot_weaken_floor():
    floor = contract(unknown_fields="reject")
    policy = contract(unknown_fields="allow")
    try:
        oc.compose(floor, policy)
    except ValueError as exc:
        assert "may not weaken" in str(exc)
    else:
        raise AssertionError("weaker policy accepted")


def test_pack_policy_can_narrow_domain_generic_wildcard_floor():
    floor = contract(allowed_namespaces=["*"], allowed_types=["*"])
    policy = contract(allowed_namespaces=["entities"], allowed_types=["company"])
    effective = oc.compose(floor, policy)
    assert effective["allowed_namespaces"] == ["entities"]
    assert effective["allowed_types"] == ["company"]


# ── validation rejection paths (okengine#462, tranche T1) ──────────────────────
# validate() never raises for author input; it must ACCUMULATE actionable errors.
# These assert each rejection actually fires, since a contract that fails to
# reject is a contract that silently permits an unbounded model write.

def test_non_object_contract_is_rejected():
    for junk in (None, [], "contract", 7):
        assert oc.validate(junk) == ["output_contract must be an object"]


def test_list_fields_must_be_lists_of_nonempty_unique_strings():
    assert any("must be a list of non-empty strings" in e
               for e in oc.validate(contract(allowed_namespaces="sources")))
    assert any("must be a list of non-empty strings" in e
               for e in oc.validate(contract(allowed_types=["source", "   "])))
    assert any("must be a list of non-empty strings" in e
               for e in oc.validate(contract(required_fields=["type", 7])))
    assert any("must not be empty" in e
               for e in oc.validate(contract(allowed_namespaces=[])))
    assert any("contains duplicates" in e
               for e in oc.validate(contract(allowed_types=["source", "source"])))
    assert any("contains duplicates" in e
               for e in oc.validate(contract(required_relationships=["a", "a"])))
    assert oc._strings([f"value-{index}" for index in range(300)], "many")
    try:
        oc._strings(["value"], "where", True)
    except TypeError:
        pass
    else:
        raise AssertionError("nonempty is a keyword-only contract")


def test_operations_must_be_supported_verbs():
    errors = oc.validate(contract(operations=["create", "obliterate"]))
    assert any("unsupported value(s)" in e and "obliterate" in e for e in errors)
    assert any("must not be empty" in e for e in oc.validate(contract(operations=[])))
    assert any("must be a list of non-empty strings" in e
               for e in oc.validate(contract(operations="create")))


def test_body_spec_is_validated():
    assert any("body must be an object" in e for e in oc.validate(contract(body=[])))
    assert any("body has unknown key(s)" in e
               for e in oc.validate(contract(body={"required": True, "tone": "brisk"})))
    assert any("body.required must be boolean" in e
               for e in oc.validate(contract(body={"required": "yes"})))
    for bad in (-1, "80", 1.5, True):
        assert any("min_non_whitespace must be a non-negative integer" in e
                   for e in oc.validate(contract(body={"required": True,
                                                       "min_non_whitespace": bad})))
    for bad in (0, -1, "8000", 1.5, True):
        assert any("max_non_whitespace must be a positive integer" in e
                   for e in oc.validate(contract(body={"required": True,
                                                       "max_non_whitespace": bad})))
    assert any("must be at least the minimum" in e for e in oc.validate(contract(
        body={"required": True, "min_non_whitespace": 100, "max_non_whitespace": 99})))


def test_pack_policy_can_tighten_body_maximum_but_not_raise_floor_cap():
    floor = contract(body={"required": True, "min_non_whitespace": 40,
                           "max_non_whitespace": 1000})
    policy = contract(body={"required": True, "min_non_whitespace": 80,
                            "max_non_whitespace": 800})
    assert oc.compose(floor, policy)["body"] == {
        "required": True,
        "min_non_whitespace": 80,
        "max_non_whitespace": 800,
    }
    try:
        oc.compose(floor, contract(body={"required": True, "min_non_whitespace": 80,
                                         "max_non_whitespace": 1200}))
    except ValueError as exc:
        assert "max_non_whitespace policy may not weaken" in str(exc)
    else:
        raise AssertionError("weaker maximum accepted")


def test_body_maximum_accepts_positive_one_and_equal_minimum():
    assert oc.validate(contract(body={"required": True, "min_non_whitespace": 1,
                                      "max_non_whitespace": 1})) == []


def test_pack_policy_can_preserve_body_maximum():
    floor = contract(body={"required": True, "min_non_whitespace": 40,
                           "max_non_whitespace": 1000})
    policy = contract(body={"required": True, "min_non_whitespace": 80,
                            "max_non_whitespace": 1000})
    assert oc.compose(floor, policy)["body"] == {
        "required": True, "min_non_whitespace": 80, "max_non_whitespace": 1000,
    }


def test_enforcement_policies_and_completion_are_closed_enums():
    for key in ("unknown_fields", "unresolved_links", "placeholder_links"):
        errors = oc.validate(contract(**{key: "ignore"}))
        assert any(f"{key} must be one of" in e for e in errors)
    assert any("completion must be one of" in e
               for e in oc.validate(contract(completion="whenever")))


# ── composition edge cases ─────────────────────────────────────────────────────

def test_compose_with_absent_sides():
    assert oc.compose(None, None) is None

    policy_only = oc.compose(None, contract(allowed_types=["entity"]))
    assert policy_only["allowed_types"] == ["entity"]

    floor_only = oc.compose(contract(allowed_types=["source"]), None)
    assert floor_only["allowed_types"] == ["source"]


def test_compose_refuses_invalid_input_on_either_side():
    for floor, policy in ((None, contract(api=2)),
                          (contract(api=2), None),
                          (contract(api=2), contract()),
                          (contract(), contract(api=2))):
        try:
            oc.compose(floor, policy)
        except ValueError as exc:
            assert "api must be 1" in str(exc)
        else:
            raise AssertionError("invalid contract composed")


def test_wildcard_policy_over_a_concrete_floor_is_rejected_as_widening():
    """Pins CURRENT behaviour, which is narrower than compose() appears to intend.

    compose() has a branch for "policy is '*', floor is concrete" that sets
    narrowed = floor -- i.e. it looks written to let a pack say '*' meaning
    "inherit whatever the engine allows". But the widen guard immediately after
    always fires for that case ('*' is never a member of a concrete floor), so
    the branch can never reach a successful return. Recorded as a finding rather
    than silently "fixed" here: changing it is a policy decision about the
    composition contract, not a test-coverage change.
    """
    floor = contract(allowed_namespaces=["sources", "entities"])
    policy = contract(allowed_namespaces=["*"])
    try:
        oc.compose(floor, policy)
    except ValueError as exc:
        assert "may not widen" in str(exc)
    else:
        raise AssertionError("wildcard policy over a concrete floor was accepted -- "
                             "behaviour changed; revisit the dead branch in compose()")


def test_disjoint_composition_is_refused_rather_than_silently_empty():
    floor = contract(allowed_namespaces=["sources"])
    policy = contract(allowed_namespaces=["entities"])
    try:
        oc.compose(floor, policy)
    except ValueError as exc:
        assert "has no allowed values" in str(exc) or "may not widen" in str(exc)
    else:
        raise AssertionError("disjoint composition accepted")


def test_policy_may_not_widen_operations_or_scope():
    try:
        oc.compose(contract(operations=["create"]), contract(operations=["create", "tombstone"]))
    except ValueError as exc:
        assert "may not widen" in str(exc)
    else:
        raise AssertionError("widened operations accepted")

    try:
        oc.compose(contract(allowed_types=["source"]),
                   contract(allowed_types=["source", "entity"]))
    except ValueError as exc:
        assert "may not widen" in str(exc)
    else:
        raise AssertionError("widened types accepted")


def test_policy_may_not_weaken_body_or_completion():
    try:
        oc.compose(contract(body={"required": True, "min_non_whitespace": 80}),
                   contract(body={"required": False}))
    except ValueError as exc:
        assert "body.required policy may not weaken" in str(exc)
    else:
        raise AssertionError("weakened body.required accepted")

    try:
        oc.compose(contract(body={"required": True, "min_non_whitespace": 80}),
                   contract(body={"required": True, "min_non_whitespace": 10}))
    except ValueError as exc:
        assert "min_non_whitespace policy may not weaken" in str(exc)
    else:
        raise AssertionError("weakened body minimum accepted")

    try:
        oc.compose(contract(completion="per-selected-item"), contract(completion="run"))
    except ValueError as exc:
        assert "completion policy may not weaken" in str(exc)
    else:
        raise AssertionError("weakened completion accepted")


def test_policy_may_not_replace_required_write_path():
    floor = contract(completion="run", required_write_path="briefings/{date}.md")
    policy = contract(completion="run", required_write_path="reports/{date}.md")
    try:
        oc.compose(floor, policy)
    except ValueError as exc:
        assert "required_write_path policy may not replace" in str(exc)
    else:
        raise AssertionError("replaced required write path accepted")


def test_policy_rank_checks_every_adjacent_transition():
    for field in ("unknown_fields", "unresolved_links", "placeholder_links"):
        # Every tightening transition is accepted and preserves the selected policy.
        for floor_value, policy_value in (
            ("allow", "review"), ("allow", "reject"), ("review", "reject"),
        ):
            effective = oc.compose(
                contract(**{field: floor_value}), contract(**{field: policy_value}),
            )
            assert effective[field] == policy_value
        # Every weakening transition is rejected, including adjacent ranks.
        for floor_value, policy_value in (
            ("review", "allow"), ("reject", "review"), ("reject", "allow"),
        ):
            try:
                oc.compose(contract(**{field: floor_value}), contract(**{field: policy_value}))
            except ValueError as exc:
                assert f"{field} policy may not weaken" in str(exc)
            else:
                raise AssertionError(f"{field}: {floor_value}->{policy_value} weakening accepted")


def test_body_composition_defaults_and_required_or_are_exact():
    empty = contract(body={})
    assert oc.compose(empty, empty)["body"] == {
        "required": False, "min_non_whitespace": 0,
    }
    required_by_policy = oc.compose(
        contract(body={"required": False}),
        contract(body={"required": True}),
    )
    assert required_by_policy["body"] == {
        "required": True, "min_non_whitespace": 0,
    }
