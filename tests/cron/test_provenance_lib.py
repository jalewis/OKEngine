"""The declared value vocabulary, resolved once for every lane that asks.

`corpus_audit` (which REPORTS violations) and `repair_carried_provenance` (which REFUSES to
carry one) each had their own resolver, and they had already diverged: the repair lane's copy
dropped the `extensible` flag — the single bit separating "the write path would reject this"
from "a pack is legitimately growing its vocabulary". These pin the one resolver both now use.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "provenance_lib.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="provenance_lib absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("provenance_lib", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["provenance_lib"] = mod
    spec.loader.exec_module(mod)
    return mod


OPEN = {"enums": {"k": ["news"]}, "field_enums": {"k": {"enum": "k", "extensible": True}}}
CLOSED = {"enums": {"k": ["news"]}, "field_enums": {"k": {"enum": "k"}}}


# --- enum_rules ----------------------------------------------------------------------------------

def test_a_named_enum_resolves_to_its_values_and_its_extensibility():
    mod = _load()
    assert mod.enum_rules(CLOSED) == {"k": ({"news"}, False)}
    assert mod.enum_rules(OPEN) == {"k": ({"news"}, True)}


def test_a_bare_list_rule_is_a_closed_allow_list():
    """`k: [a, b]` states the whole vocabulary inline; there is nothing left to extend."""
    mod = _load()
    assert mod.enum_rules({"field_enums": {"k": ["a", "b"]}}) == {"k": ({"a", "b"}, False)}


def test_an_enum_naming_no_declared_list_constrains_nothing():
    """A dangling reference must not become an EMPTY allow-list — that would reject everything."""
    mod = _load()
    assert mod.enum_rules({"enums": {}, "field_enums": {"k": {"enum": "absent"}}}) == {}


def test_a_rule_that_is_neither_a_list_nor_a_mapping_is_ignored():
    mod = _load()
    assert mod.enum_rules({"field_enums": {"k": "nonsense"}}) == {}


def test_a_schema_declaring_no_enums_at_all_resolves_to_no_rules():
    mod = _load()
    assert mod.enum_rules({}) == {}


def test_enum_values_are_compared_as_strings():
    """A YAML `2` and a page's `'2'` are the same value to a reader; make them the same here."""
    mod = _load()
    assert mod.enum_rules({"field_enums": {"k": [1, 2]}}) == {"k": ({"1", "2"}, False)}


# --- closed_enums --------------------------------------------------------------------------------

def test_closed_enums_omits_the_extensible_ones():
    """A lane refusing to write outside an EXTENSIBLE enum would enforce a rule the schema
    explicitly declined to make."""
    mod = _load()
    schema = {"enums": {"open": ["a"], "shut": ["b"]},
              "field_enums": {"open": {"enum": "open", "extensible": True},
                              "shut": {"enum": "shut"}}}
    assert mod.closed_enums(schema) == {"shut": {"b"}}


# --- classify_value ------------------------------------------------------------------------------

def test_a_declared_value_is_conformant():
    mod = _load()
    assert mod.classify_value("k", "news", mod.enum_rules(CLOSED)) is mod.CONFORMANT


def test_an_undeclared_value_on_a_closed_enum_is_drift():
    mod = _load()
    assert mod.classify_value("k", "cyber-news", mod.enum_rules(CLOSED)) == mod.DRIFT


def test_an_undeclared_value_on_an_extensible_enum_is_novel_not_drift():
    mod = _load()
    assert mod.classify_value("k", "cyber-news", mod.enum_rules(OPEN)) == mod.NOVEL


def test_an_unconstrained_field_is_never_a_finding():
    mod = _load()
    assert mod.classify_value("other", "anything", mod.enum_rules(CLOSED)) is mod.CONFORMANT


@pytest.mark.parametrize("value", [None, ["news", "report"], 3, {"a": 1}, True])
def test_a_non_string_value_is_left_to_the_shape_checks(value):
    """Stringifying a list here would report `['news', 'report']` as an out-of-enum value on
    every page that carries one — a finding manufactured by the checker, not by the corpus."""
    mod = _load()
    assert mod.classify_value("k", value, mod.enum_rules(CLOSED)) is mod.CONFORMANT


def test_a_present_but_empty_value_is_still_a_violation():
    """Absent is 'the ingest did not know'; present-and-blank is a lane writing nothing into a
    field that required a vocabulary."""
    mod = _load()
    assert mod.classify_value("k", "", mod.enum_rules(CLOSED)) == mod.DRIFT


def test_the_two_consumers_resolve_the_same_schema_the_same_way():
    """The regression that motivated the shared module: two resolvers, one missing `extensible`.

    Loaded from their real files so a future edit that re-inlines either copy fails here.
    """
    mod = _load()
    for name in ("corpus_audit", "repair_carried_provenance"):
        path = MOD.parent / f"{name}.py"
        if not path.is_file():
            pytest.skip(f"{name} absent")
        spec = importlib.util.spec_from_file_location(name, path)
        consumer = importlib.util.module_from_spec(spec)
        sys.modules[name] = consumer
        spec.loader.exec_module(consumer)
        resolver = getattr(consumer, "_enum_rules", None) or consumer.enum_rules
        if resolver is mod.enum_rules:
            assert resolver(OPEN) == {"k": ({"news"}, True)}
        else:                                    # the closed-only view
            assert resolver(OPEN) == {} and resolver(CLOSED) == {"k": {"news"}}
