"""Schema composition conflict detection (okengine#462, tranche T1).

`compose_schema` folds engine base + pack + every enabled extension fragment into
one schema. Its defining property is **no silent shadowing**: two owners claiming
the same id is a HARD error, never a last-writer-wins overwrite. That is the whole
reason the feature exists -- a second declaration quietly replacing the first is
exactly the multi-surface drift it was built to prevent.

Every test here asserts an ERROR is produced (and names the offending id), because
a conflict that fails to be reported is a composition that ships wrong.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
CRON = REPO / "scripts" / "cron"
sys.path.insert(0, str(CRON))
spec = importlib.util.spec_from_file_location("schema_lib_compose", CRON / "schema_lib.py")
sl = importlib.util.module_from_spec(spec)
sys.modules["schema_lib_compose"] = sl
spec.loader.exec_module(sl)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types:\n"
        "  actor: {required: [type]}\n"
        "  openable: {required: [type], extensible: true}\n"
        "enums:\n"
        "  flexible: [one, two]\n"
        "field_enums:\n"
        "  flexible_field: {enum: flexible, extensible: true}\n",
        encoding="utf-8")
    return tmp_path


def compose(vault, *fragments):
    return sl.compose_schema(vault, list(fragments))


# ── baseline ─────────────────────────────────────────────────────────────────

def test_composition_without_fragments_is_sound(vault):
    composed, errors = compose(vault)
    assert errors == []
    assert "actor" in composed["types"]


def test_an_extension_may_own_a_new_type(vault):
    composed, errors = compose(vault, ("ext:demo", {"owns": {"types": {"widget": {"required": ["type"]}}}}))
    assert errors == [], errors
    assert "widget" in composed["types"]
    assert composed["owners"]["types"]["widget"] == "ext:demo"


# ── owns: no shadowing ───────────────────────────────────────────────────────

def test_two_extensions_owning_the_same_type_is_a_hard_error(vault):
    _, errors = compose(vault,
                        ("ext:a", {"owns": {"types": {"widget": {"required": ["type"]}}}}),
                        ("ext:b", {"owns": {"types": {"widget": {"required": ["type"]}}}}))
    assert any("widget" in e for e in errors), errors


def test_duplicate_namespace_does_not_skip_later_new_namespace(vault):
    composed, errors = compose(vault, ("ext:demo", {"owns": {"namespaces": {
        "entities": {}, "new-one": {"strategy": "flat"},
        "new-two": {"strategy": "by-letter"},
    }}}))
    assert errors == [
        "ext:demo: namespace 'entities' already owned by engine (own = new ids only)"]
    assert composed["partitioning"]["namespaces"]["new-one"] == {"strategy": "flat"}
    assert composed["partitioning"]["namespaces"]["new-two"] == {"strategy": "by-letter"}


@pytest.mark.parametrize("namespace", ["dashboards", "operational"])
def test_extension_cannot_own_reserved_derived_namespace(vault, namespace):
    composed, errors = compose(vault, ("ext:demo", {"owns": {"namespaces": {
        namespace: {"strategy": "flat"},
    }}}))
    assert errors == [
        f"ext:demo: namespace '{namespace}' is reserved for "
        "engine-derived/operational artifacts"
    ]
    assert namespace not in composed["partitioning"]["namespaces"]


@pytest.mark.parametrize("namespace", ["dashboards", "operational"])
def test_pack_cannot_own_reserved_derived_namespace(vault, namespace):
    schema = vault / "schema.yaml"
    schema.write_text(
        schema.read_text(encoding="utf-8")
        + f"partitioning:\n  namespaces:\n    {namespace}: {{strategy: flat}}\n",
        encoding="utf-8",
    )
    sl._SCHEMA_CACHE.clear()

    _, errors = compose(vault)

    assert errors == [
        f"pack: namespace '{namespace}' is reserved for engine-derived/operational artifacts"
    ]


def test_an_extension_may_not_reclaim_a_pack_type(vault):
    _, errors = compose(vault, ("ext:a", {"owns": {"types": {"actor": {"required": ["type"]}}}}))
    assert any("actor" in e for e in errors), errors


# NOTE on the fragment grammar: `namespaces` and `types` are nested under `owns`,
# but `enums`, `field_enums`, `field_shapes`, `extends` and `field_items` are
# TOP-LEVEL fragment keys. Nesting the latter under `owns` silently composes to
# nothing rather than erroring, so these tests use the real shape.

def test_duplicate_enum_ownership_is_reported(vault):
    _, errors = compose(vault,
                        ("ext:a", {"enums": {"colours": ["red"]}}),
                        ("ext:b", {"enums": {"colours": ["blue"]}}))
    assert any("colours" in e for e in errors), errors


def test_a_non_list_enum_is_rejected(vault):
    _, errors = compose(vault, ("ext:a", {"enums": {"colours": "red"}}))
    assert any("must be a list" in e for e in errors), errors


def test_duplicate_field_enum_declaration_is_reported(vault):
    frag = {"field_enums": {"shared_field": {"enum": "flexible"}}}
    _, errors = compose(vault, ("ext:a", frag), ("ext:b", frag))
    assert any("shared_field" in e for e in errors), errors


# ── extends: additive only ───────────────────────────────────────────────────

def test_extending_an_extensible_type_with_an_optional_field_is_allowed(vault):
    composed, errors = compose(vault, ("ext:demo", {
        "extends": {"openable": {"fields": {"extra": {"optional": True}}}}}))
    assert errors == [], errors
    assert "extra" in composed["types"]["openable"]["fields"]


def test_extending_a_type_not_marked_extensible_is_refused(vault):
    _, errors = compose(vault, ("ext:demo", {
        "extends": {"actor": {"fields": {"extra": {"optional": True}}}}}))
    assert any("not marked extensible" in e for e in errors), errors


def test_refused_extension_does_not_skip_later_extensions(vault):
    composed, errors = compose(vault, ("ext:demo", {"extends": {
        "actor": {"fields": {"forbidden": {"optional": True}}},
        "openable": {"fields": {"accepted": {"optional": True}}},
    }}))
    assert errors == ["ext:demo: type 'actor' is not marked extensible by its owner"]
    assert "forbidden" not in composed["types"]["actor"].get("fields", {})
    assert composed["types"]["openable"]["fields"]["accepted"] == {"optional": True}


def test_extending_an_unknown_type_is_refused(vault):
    _, errors = compose(vault, ("ext:demo", {
        "extends": {"nope": {"fields": {"x": {"optional": True}}}}}))
    assert any("unknown type" in e for e in errors), errors


def test_an_extended_field_must_be_optional(vault):
    """An extension cannot impose a new REQUIRED field on someone else's type."""
    _, errors = compose(vault, ("ext:demo", {
        "extends": {"openable": {"fields": {"mandatory": {"optional": False}}}}}))
    assert any("must be optional" in e for e in errors), errors


def test_two_extensions_claiming_the_same_extended_field_conflict(vault):
    frag = {"extends": {"openable": {"fields": {"extra": {"optional": True}}}}}
    _, errors = compose(vault, ("ext:a", frag), ("ext:b", frag))
    assert any("already claimed" in e for e in errors), errors


def test_a_non_mapping_extends_block_is_refused(vault):
    _, errors = compose(vault, ("ext:demo", {"extends": {"openable": ["not", "a", "mapping"]}}))
    assert any("must be a mapping" in e for e in errors), errors


# ── extends: enum values ─────────────────────────────────────────────────────

def test_an_extensible_enum_accepts_new_values(vault):
    composed, errors = compose(vault, ("ext:demo", {"extends": {"flexible": {"add": ["three"]}}}))
    assert errors == [], errors
    assert "three" in composed["enums"]["flexible"]


def test_extensible_enum_name_requires_exact_value_equality(vault):
    # "alpha" sorts before "flexible". A relational comparison must not grant
    # extension authority to the wrong enum.
    (vault / "schema.yaml").write_text(
        "types: {actor: {required: [type]}}\n"
        "enums: {alpha: [one], flexible: [one]}\n"
        "field_enums:\n"
        "  flexible_field: {enum: flexible, extensible: true}\n",
        encoding="utf-8")
    _, errors = compose(vault, ("ext:demo", {
        "extends": {"alpha": {"add": ["two"]}}}))
    assert errors == ["ext:demo: enum 'alpha' is not extensible"]


def test_a_closed_enum_refuses_new_values(vault):
    (vault / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\nenums:\n  closed: [one]\n", encoding="utf-8")
    _, errors = compose(vault, ("ext:demo", {"extends": {"closed": {"add": ["two"]}}}))
    assert any("not extensible" in e for e in errors), errors


def test_extension_cannot_reopen_an_engine_owned_closed_enum(vault):
    composed, errors = compose(vault, ("ext:hostile", {
        "field_enums": {"unrelated": {"enum": "tlp", "extensible": True}},
        "extends": {"tlp": {"add": ["SUPER_SECRET_BYPASS"]}},
    }))
    assert errors == ["ext:hostile: enum 'tlp' is not extensible"]
    assert "SUPER_SECRET_BYPASS" not in composed["enums"]["tlp"]


def test_re_adding_an_existing_enum_value_is_reported(vault):
    _, errors = compose(vault, ("ext:demo", {"extends": {"flexible": {"add": ["one"]}}}))
    assert any("already exists" in e for e in errors), errors


def test_duplicate_enum_value_does_not_skip_later_new_values(vault):
    composed, errors = compose(vault, ("ext:demo", {
        "extends": {"flexible": {"add": ["one", "three", "four"]}}}))
    assert errors == ["ext:demo: enum value 'flexible.one' already exists"]
    assert composed["enums"]["flexible"] == ["one", "two", "three", "four"]


def test_non_reference_field_does_not_trigger_reference_validation(vault):
    composed, errors = compose(vault, ("ext:demo", {
        "extends": {"openable": {"fields": {
            "label": {"type": "string", "to": "not-a-type", "optional": True},
            "plain": {"optional": True},
        }}}}))
    assert errors == []
    assert set(composed["types"]["openable"]["fields"]) == {"label", "plain"}


# ── field_items: one owner per field ─────────────────────────────────────────

def test_field_items_accept_a_mapping_of_item_rules(vault):
    composed, errors = compose(vault, ("ext:demo", {
        "field_items": {"evidence": {"direction": {"enum": ["supports"]}}}}))
    assert errors == [], errors
    assert "evidence" in composed["field_items"]


def test_duplicate_field_items_declarations_conflict(vault):
    frag = {"field_items": {"evidence": {"direction": {"enum": ["supports"]}}}}
    _, errors = compose(vault, ("ext:a", frag), ("ext:b", frag))
    assert any("already declared" in e and "one owner per field" in e for e in errors), errors


def test_non_mapping_field_items_are_refused(vault):
    _, errors = compose(vault, ("ext:demo", {"field_items": {"evidence": ["nope"]}}))
    assert any("must be a mapping" in e for e in errors), errors


def test_by_type_field_shapes_merge_across_owners(vault):
    """okengine#563: a by_type rule is PER-TYPE, so two owners can govern the same field name for
    DIFFERENT types without conflicting — `confidence` is a probability on the predictions
    extension's own type and a band on types the pack owns. Rejecting that dropped the extension's
    declaration and left a STALE composed artifact on the live vault, which the write path reads."""
    composed, errors = compose(
        vault,
        ("ext:a", {"field_shapes": {"confidence": {"by_type": {"assessment": "number"}}}}),
        ("ext:b", {"field_shapes": {"confidence": {"by_type": {"prediction": "number"}}}}))
    assert errors == [], errors
    assert composed["field_shapes"]["confidence"]["by_type"] == {
        "assessment": "number", "prediction": "number"}


def test_the_same_type_declared_twice_is_still_a_conflict(vault):
    """Merging by type must not hide a REAL clash: two owners governing the same (field, type)."""
    _, errors = compose(
        vault,
        ("ext:a", {"field_shapes": {"confidence": {"by_type": {"prediction": "str"}}}}),
        ("ext:b", {"field_shapes": {"confidence": {"by_type": {"prediction": "number"}}}}))
    assert errors and any("prediction" in e for e in errors), errors
