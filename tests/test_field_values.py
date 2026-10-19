"""okengine#983 QA: the write path's "is this field unchanged?" comparison is type-strict.

`0 == False` and `1 == True` in Python, so a guard that compared with `!=` let a write store `0`
over `false` as "unchanged". `field_values.same_value` compares canonical YAML instead, and the
#980 attestation guard uses the same helper.
"""
from __future__ import annotations

import datetime

import pytest

from okengine import attestation
from okengine.field_values import canonical, same_value


@pytest.mark.parametrize(("left", "right"), [
    (False, 0), (True, 1), (True, "true"), (1, 1.0), ("1", 1), (None, ""), ([], {}),
    ([0], [False]), ({"a": 1}, {"a": True}), (datetime.date(2026, 1, 2), "2026-01-02"),
])
def test_values_of_different_types_are_not_the_same(left, right):
    assert not same_value(left, right)
    assert not same_value(right, left)


@pytest.mark.parametrize("value", [
    False, 0, "x", None, [1, "a", {"k": [True]}], {"b": 1, "a": 2}, datetime.date(2026, 1, 2),
])
def test_a_verbatim_echo_is_the_same(value):
    assert same_value(value, value)


def test_mapping_order_is_not_a_change():
    assert same_value({"a": 1, "b": 2}, {"b": 2, "a": 1})


def test_list_order_is_a_change():
    assert not same_value([1, 2], [2, 1])


def test_a_non_yaml_value_falls_back_to_type_and_repr():
    class Opaque:
        def __repr__(self):
            return "opaque"

    assert canonical(Opaque()) == "!test_a_non_yaml_value_falls_back_to_type_and_repr.<locals>.Opaque opaque"
    assert same_value(Opaque(), Opaque())
    assert not same_value(Opaque(), "opaque")


@pytest.mark.parametrize(("stored", "sent"), [(True, 1), (False, 0), (True, "true")])
def test_the_attestation_guard_refuses_a_type_change(stored, sent):
    prev = {"type": "source", "bounded_auto_accept": stored}
    assert attestation.operator_field_changes(prev, dict(prev, bounded_auto_accept=sent)) == [
        "bounded_auto_accept"]


def test_the_attestation_guard_refuses_a_type_change_of_record_identity():
    prev = {"type": "source", "local_only": True, "publisher": "1"}
    refusal = attestation.mcp_write_refusal(prev, dict(prev, publisher=1), body_changed=False)
    assert refusal and refusal.startswith("refused: publisher")


@pytest.mark.parametrize(("stored", "sent"), [(False, 0), (True, 1), (1, 1.0), (0, False)])
def test_the_record_identity_guard_refuses_a_python_equal_type_change(stored, sent):
    """okengine#991 [12]: each pair is `==` in Python, so only a type-strict comparison refuses it.
    The `"1"`/`1` case above is unequal under plain `!=` as well and cannot tell the two apart."""
    prev = {"type": "source", "local_only": True, "authority_id": stored}
    refusal = attestation.mcp_write_refusal(prev, dict(prev, authority_id=sent), body_changed=False)
    assert refusal and refusal.startswith("refused: authority_id"), refusal


def test_the_attestation_guard_still_accepts_an_echo():
    prev = {"type": "source", "local_only": True, "publisher": "Acme", "bounded_auto_accept": False}
    assert attestation.mcp_write_refusal(prev, dict(prev), body_changed=False) is None
