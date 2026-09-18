"""Unit coverage for schema_validator's matching/lookup helpers (okengine#462, T1).

These small pure functions decide, for every page the write path touches, whether a
field counts as present, whether a path is excluded from the contract, and which enum
rule governs a field. They are cheap to get wrong and expensive when wrong — an
over-eager `_excluded` silently exempts a whole namespace from conformance, and a
mis-resolved `_enum_rule` lets an invalid value through.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
SV_MOD = REPO / "tools" / "schema_validator.py"


def load_sv():
    spec = importlib.util.spec_from_file_location("schema_validator_units", SV_MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def sv():
    return load_sv()


# ── _present: what satisfies a required field ─────────────────────────────────

def test_present_requires_a_meaningful_value(sv):
    assert sv._present({"type": "source"}, "type")
    assert not sv._present({}, "type"), "absent"
    assert not sv._present({"type": None}, "type"), "null"
    assert not sv._present({"type": "   "}, "type"), "whitespace-only scalar"


def test_empty_list_satisfies_present_by_design(sv):
    """Documented: empty lists PASS the gate (drift-lint flags them) so a stub being
    filled in is not rejected outright. Pinning this so the leniency is deliberate."""
    assert sv._present({"sources": []}, "sources")
    assert sv._present({"count": 0}, "count"), "falsy non-string values still count"
    assert sv._present({"flag": False}, "flag")


# ── _excluded: paths outside the contract ─────────────────────────────────────

def test_excluded_matches_directory_prefixes(sv):
    schema = {"exclude": ["dashboards/"]}
    assert sv._excluded("dashboards/ops.md", schema)
    assert sv._excluded("dashboards", schema), "the bare directory name itself"
    assert not sv._excluded("entities/dashboards-vendor.md", schema), \
        "a prefix match must not leak across a name boundary"


def test_excluded_matches_globs_on_full_path_and_basename(sv):
    schema = {"exclude": ["*.tmp.md", "private/**"]}
    assert sv._excluded("entities/a/draft.tmp.md", schema), "basename glob"
    assert sv._excluded("private/notes/secret.md", schema), "path glob"
    assert not sv._excluded("entities/a/final.md", schema)


def test_excluded_is_false_without_an_exclude_list(sv):
    assert not sv._excluded("entities/a/x.md", {})
    assert not sv._excluded("entities/a/x.md", {"exclude": None})


# ── _field_values: scalar/list normalisation ──────────────────────────────────

def test_field_values_normalises_scalars_and_lists_to_strings(sv):
    assert sv._field_values("CLEAR") == ["CLEAR"]
    assert sv._field_values(["a", "b"]) == ["a", "b"]
    assert sv._field_values(7) == ["7"], "non-str scalars coerce (okengine#348 class)"
    assert sv._field_values([1, 2]) == ["1", "2"], "non-str list members coerce"


# ── _enum_rule: which enum governs a field for a type ─────────────────────────

def test_enum_rule_returns_plain_field_rule(sv):
    schema = {"field_enums": {"tlp": {"enum": "tlp"}}}
    assert sv._enum_rule(schema, "source", "tlp") == {"enum": "tlp"}


def test_enum_rule_resolves_per_type_overrides(sv):
    schema = {"field_enums": {"status": {
        "enum": "generic_status",
        "extensible": True,
        "by_type": {"source": "source_status",
                    "actor": {"enum": "actor_status", "extensible": False}},
    }}}
    # string form: keeps the parent's `extensible`
    assert sv._enum_rule(schema, "source", "status") == {"enum": "source_status",
                                                         "extensible": True}
    # dict form: merges over the parent
    actor = sv._enum_rule(schema, "actor", "status")
    assert actor["enum"] == "actor_status" and actor["extensible"] is False
    # a type with no override falls back to the parent rule
    assert sv._enum_rule(schema, "malware", "status")["enum"] == "generic_status"


def test_enum_rule_absent_for_unknown_or_malformed_fields(sv):
    assert sv._enum_rule({}, "source", "tlp") is None
    assert sv._enum_rule({"field_enums": {"tlp": "tlp"}}, "source", "tlp") is None, \
        "a non-mapping rule is ignored rather than crashing"
    assert sv._enum_rule({"field_enums": {"x": {"note": "no enum key"}}}, "source", "x") is None


# ── _load_schema: caching and failure modes ───────────────────────────────────

def test_load_schema_returns_none_for_missing_broken_or_non_mapping(sv, tmp_path):
    assert sv._load_schema(tmp_path / "absent.yaml") is None

    broken = tmp_path / "broken.yaml"
    broken.write_text("okf: [unclosed\n", encoding="utf-8")
    assert sv._load_schema(broken) is None

    listy = tmp_path / "listy.yaml"
    listy.write_text("- a\n- b\n", encoding="utf-8")
    assert sv._load_schema(listy) is None


def test_load_schema_caches_by_mtime_and_reloads_on_change(sv, tmp_path):
    path = tmp_path / "schema.yaml"
    path.write_text("types:\n  source: {}\n", encoding="utf-8")
    first = sv._load_schema(path)
    assert "source" in first["types"]
    assert sv._load_schema(path) is first, "unchanged file served from cache"

    import os
    path.write_text("types:\n  entity: {}\n", encoding="utf-8")
    os.utime(path, (0, 0))          # force a distinct mtime
    reloaded = sv._load_schema(path)
    assert "entity" in reloaded["types"], "a changed schema must not serve a stale cache"
