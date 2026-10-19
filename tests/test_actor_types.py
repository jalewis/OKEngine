"""Which page types are actors, resolved from the composed schema (okengine#1020)."""
from __future__ import annotations

import pytest

from okengine import actor_types as at

_SCHEMA = {
    "types": {
        "intrusion-set": {"required": ["type"], "role": "actor"},
        "Threat-Actor": {"role": " Actor "},
        "malware": {"required": ["type"]},
        "campaign": {"role": "campaign"},
        "broken": "not a mapping",
    },
    "type_aliases": {"apt-group": "intrusion-set", "threat_actor": "actor", "cve": "vulnerability"},
}


def test_actor_is_always_an_actor_type_and_declarations_add_to_it():
    assert at.actor_types(_SCHEMA) == {"actor", "intrusion-set", "threat-actor", "apt-group",
                                       "threat_actor"}


@pytest.mark.parametrize("schema", [None, {}, [], "x", {"types": ["actor"], "type_aliases": 3}])
def test_an_absent_or_malformed_schema_leaves_only_the_engine_actor_type(schema):
    assert at.actor_types(schema) == {"actor"}


@pytest.mark.parametrize("value,expected", [
    ("intrusion-set", True), ("INTRUSION-SET", True), (" threat-actor ", True),
    ("apt-group", True), ("Actor", True), ("threat_actor", True),
    ("malware", False), ("campaign", False), ("cve", False), ("", False), (None, False),
    ("broken", False),
])
def test_is_actor_type(value, expected):
    assert at.is_actor_type(_SCHEMA, value) is expected


@pytest.mark.parametrize("value,expected", [
    ("actor", True), (" ACTOR ", True), ("threat_actor", True),
    ("intrusion-set", False), ("apt-group", False), ("malware", False), ("", False),
])
def test_only_the_engine_type_and_its_aliases_are_the_default_actor_type(value, expected):
    assert at.is_default_actor_type(_SCHEMA, value) is expected


def test_the_default_actor_type_needs_no_schema():
    assert at.is_default_actor_type(None, "actor") and not at.is_default_actor_type(None, "x")
    assert not at.is_default_actor_type({"type_aliases": ["x"]}, "x")


@pytest.mark.parametrize("aliases,value", [
    ({"aaa": "actor"}, "zzz"),      # another alias of `actor` is not this type
    ({"zzz": "abc"}, "zzz"),        # this alias maps somewhere that is not `actor`
    ({"zzz": "intrusion-set"}, "zzz"),
])
def test_an_alias_is_the_default_actor_type_only_when_it_is_exactly_an_alias_of_actor(aliases, value):
    assert not at.is_default_actor_type({"type_aliases": aliases}, value)


def test_undeclared_types_need_both_a_count_and_a_share_of_actor_references():
    counts = {"adversary": 30, "intrusion-set": 40, "malware": 4, "campaign": 2}
    # intrusion-set is declared; malware is 4 of 76 (< 20%); campaign is under the floor.
    assert at.undeclared_actor_types(_SCHEMA, counts) == {"adversary": 30}
    assert at.undeclared_actor_types({}, {"threat-actor": 3}) == {"threat-actor": 3}
    assert at.undeclared_actor_types({}, {"threat-actor": 2}) == {}, "below the reference floor"
    assert at.undeclared_actor_types({}, {"a": 4, "b": 16}) == {"a": 4, "b": 16}, "exactly 20%"
    assert at.undeclared_actor_types({}, {"a": 4, "b": 17}) == {"b": 17}, "just under 20%"


@pytest.mark.parametrize("counts", [{}, {"x": 0}])
def test_no_references_report_nothing(counts):
    assert at.undeclared_actor_types({}, counts) == {}


def test_reference_targets_read_page_references_only():
    fm = {"actor": "[[entities/t/a/ta505|TA505]]",
          "actors": ["wiki/entities/l/lazarus.md", "Lazarus", 7, "/entities/x/"],
          "related": "entities/m/malware"}
    assert at.reference_targets(fm) == ["entities/t/a/ta505", "entities/l/lazarus", "entities/x"]
    assert at.reference_targets({"actor": None}) == []
    assert at.reference_targets({"attributed_to": "entities/t/ta505"}) == ["entities/t/ta505"]
