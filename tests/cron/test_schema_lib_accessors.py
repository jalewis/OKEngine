"""Schema accessor coverage for the baked write-path lib (okengine#462, tranche T1).

`scripts/cron/schema_lib.py` is imported by the ENFORCED write path (write_server
loads it from the baked image), so a wrong answer here is a wrong write. These
accessors are small, pure and heavily depended upon:

  type_aliases / canonical_type   resolve a pack alias to the canonical taxonomy
  type_home_namespace             which namespace a type belongs in -- the guard
                                  that rejects `type: source` created under concepts/
  is_page_ref                     page-path vs prose discriminator for ref_fields
  _is_extensible_enum             whether an enum admits pack-supplied values
  excluded_dirs                   namespaces excluded from conformance/indexing

Each is asserted for its default (an absent declaration must be a NO-OP, never a
vacuous reject) as well as its configured behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

MOD = Path(__file__).resolve().parents[2] / "scripts" / "cron" / "schema_lib.py"
spec = importlib.util.spec_from_file_location("schema_lib_accessors", MOD)
sl = importlib.util.module_from_spec(spec)
sys.modules["schema_lib_accessors"] = sl
spec.loader.exec_module(sl)


# ── type aliases ──────────────────────────────────────────────────────────────

def test_type_aliases_default_to_no_remapping():
    assert sl.type_aliases({}) == {}
    assert sl.type_aliases({"type_aliases": None}) == {}
    assert sl.type_aliases({"type_aliases": ["not", "a", "map"]}) == {}


def test_type_aliases_coerce_keys_and_values_to_strings():
    assert sl.type_aliases({"type_aliases": {"apt": "actor", 7: 8}}) == \
        {"apt": "actor", "7": "8"}


def test_canonical_type_resolves_aliases_and_passes_through_unknowns():
    schema = {"type_aliases": {"apt": "actor", "intrusion-set": "actor"}}
    assert sl.canonical_type(schema, "apt") == "actor"
    assert sl.canonical_type(schema, "intrusion-set") == "actor"
    assert sl.canonical_type(schema, "actor") == "actor", "already canonical"
    assert sl.canonical_type(schema, "malware") == "malware", "unknown passes through"


def test_canonical_type_normalises_whitespace_and_empty_values():
    schema = {"type_aliases": {"apt": "actor"}}
    assert sl.canonical_type(schema, "  apt  ") == "actor"
    assert sl.canonical_type(schema, None) == ""
    assert sl.canonical_type(schema, "") == ""


# ── type_home_namespace: the wrong-namespace guard (okengine#276) ─────────────

def test_declared_type_namespaces_win_over_the_core_convention():
    schema = {"type_namespaces": {"source": "raw-sources"}}
    assert sl.type_home_namespace(schema, "source") == "raw-sources"


def test_core_convention_applies_without_a_declaration():
    assert sl.type_home_namespace({}, "source") == "sources"


def test_every_engine_core_type_has_a_home_namespace_guard():
    """A new base type must not silently disable both write-time and audit anti-fork checks."""
    core_types = set((sl.base_schema().get("types") or {}).keys())
    assert core_types == set(sl._CORE_TYPE_HOME), (
        "config/base-schema.yaml types and _CORE_TYPE_HOME must change together"
    )


def test_undeterminable_type_returns_none_so_the_guard_is_a_no_op():
    """None means "no rule" -- the write path must not reject a type it has no home for."""
    assert sl.type_home_namespace({}, "totally-domain-specific-type") is None
    assert sl.type_home_namespace({"type_namespaces": {"x": ""}}, "x") is None, \
        "an empty declaration is not a rule"
    assert sl.type_home_namespace({"type_namespaces": "nope"}, "source") == "sources", \
        "a malformed declaration falls back to the convention rather than crashing"


# ── is_page_ref: graph edge vs prose ─────────────────────────────────────────

def test_page_paths_are_recognised_as_refs():
    for ref in ("sources/2026/06/x", "[[entities/a/foo]]", "concepts/phishing.md",
                "  entities/a/b  "):
        assert sl.is_page_ref(ref), ref


def test_prose_is_not_a_ref():
    for prose in ("Cisco Talos disclosure", "MITRE ATT&CK", "internal telemetry", ""):
        assert not sl.is_page_ref(prose), prose


def test_is_page_ref_tolerates_non_string_entries():
    """A non-str entry must not crash the ref_fields rule (the okengine#348 class)."""
    assert not sl.is_page_ref(7)
    assert not sl.is_page_ref(None)


# ── _is_extensible_enum ──────────────────────────────────────────────────────

def test_enum_is_extensible_when_a_field_marks_it_so():
    schema = {"field_enums": {"status": {"enum": "status_vals", "extensible": True}}}
    assert sl._is_extensible_enum(schema, "status_vals")


def test_enum_is_extensible_through_a_by_type_mapping():
    schema = {"field_enums": {"status": {"enum": "generic", "extensible": True,
                                         "by_type": {"actor": "actor_status"}}}}
    assert sl._is_extensible_enum(schema, "actor_status")


def test_enum_is_closed_by_default_and_for_malformed_specs():
    assert not sl._is_extensible_enum({}, "anything")
    assert not sl._is_extensible_enum(
        {"field_enums": {"status": {"enum": "status_vals"}}}, "status_vals")
    assert not sl._is_extensible_enum({"field_enums": "not-a-map"}, "x")
    assert not sl._is_extensible_enum({"field_enums": {"status": "not-a-map"}}, "x")


# ── excluded_dirs ────────────────────────────────────────────────────────────

def test_excluded_dirs_default_empty():
    assert sl.excluded_dirs({}) == set()
    assert sl.excluded_dirs({"exclude": None}) == set()


def test_excluded_dirs_derives_namespace_names_from_exclude_paths():
    got = sl.excluded_dirs({"exclude": ["wiki/operational/", "dashboards/"]})
    assert "operational" in got and "dashboards" in got
