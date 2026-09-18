"""Mutation-strength assertions for the critical schema-validation boundary (#535)."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "tools" / "schema_validator.py"


def load_validator(monkeypatch, *, ttl: str = "10"):
    monkeypatch.setenv("OKENGINE_SCHEMA_FIND_TTL", ttl)
    spec = importlib.util.spec_from_file_location("schema_validator_mutation", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_structural_names_have_exact_boundaries(monkeypatch):
    sv = load_validator(monkeypatch)
    for name in ("_review.md", ".hidden.md", "INDEX.md", "INDEX-p01.md", "index-p99.md"):
        assert sv._is_generated_structural(name), name
    for name in ("review.md", "INDEX", "INDEXED.md", "xINDEX-p01.md", "index-x01.md"):
        assert not sv._is_generated_structural(name), name


def test_module_defaults_resolve_exact_locations_and_ttl(monkeypatch):
    sv = load_validator(monkeypatch)
    assert sv._FIND_TTL == 10.0
    assert sv._DEFAULT_BASE == REPO / "config" / "base-schema.yaml"


def test_base_schema_failure_and_one_sided_merges(monkeypatch, tmp_path):
    sv = load_validator(monkeypatch)
    base = tmp_path / "base.yaml"
    base.write_text("types: {base: {required: [type]}}\nenums: {base_enum: [a]}\n")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(base))
    sv._base_cache.clear()

    merged = sv._base_merged({})
    assert set(merged["types"]) == {"base"}
    assert merged["enums"] == {"base_enum": ["a"]}

    base.write_text("types: {}\nenums: {}\n")
    os.utime(base, None)
    sv._base_cache.clear()
    pack = {"types": {"pack": {}}, "enums": {"pack_enum": ["b"]}}
    assert sv._base_merged(pack) == pack

    sv._base_cache.clear()
    monkeypatch.setattr(sv.yaml, "safe_load", lambda _text: (_ for _ in ()).throw(ValueError("bad")))
    assert sv._base_schema() == {}


def test_schema_walk_cache_freshness_composed_precedence_and_root(monkeypatch, tmp_path):
    sv = load_validator(monkeypatch, ttl="10")
    vault = tmp_path / "vault"
    page = vault / "wiki" / "entities" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("x")
    raw = vault / "schema.yaml"
    raw.write_text("types: {}\n")

    assert sv._find_schema(str(page)) == raw
    key = str(page.parent.resolve())
    cached_at, cached_path = sv._dir_to_schema[key]
    assert cached_path == str(raw)

    composed = vault / ".okengine" / "composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text("types: {}\n")
    sv._dir_to_schema[key] = (cached_at, str(raw))
    assert sv._find_schema(str(page)) == raw, "a fresh positive cache must be honored"
    sv._dir_to_schema[key] = (sv.time.monotonic() - 11, str(raw))
    assert sv._find_schema(str(page)) == composed, "an expired cache must re-walk"

    lone = tmp_path / "lone" / "page.md"
    lone.parent.mkdir()
    lone.write_text("x")
    assert sv._find_schema(str(lone)) is None, "the walk must terminate at the filesystem root"


def test_enum_case_walks_all_fields_and_distinguishes_ambiguous_values(monkeypatch):
    sv = load_validator(monkeypatch)
    schema = {
        "enums": {"one": ["A"], "amb": ["X", "x"], "two": ["B"]},
        "field_enums": {
            "missing": {"enum": "one"},
            "no_rule": {"note": "none"},
            "bad_allowed": {"enum": "absent"},
            "ambiguous": {"enum": "amb"},
            "values": {"enum": "two"},
        },
    }
    fm = {"no_rule": "a", "bad_allowed": "a", "ambiguous": "x", "values": ["b", "B"]}
    changes = sv.canonicalize_enum_case(schema, "entity", fm)
    assert fm == {"no_rule": "a", "bad_allowed": "a", "ambiguous": "x", "values": ["B", "B"]}
    assert changes == ["values: 'b' -> 'B'"]

    assert sv.canonicalize_enum_case([], "entity", fm) == []
    assert sv.canonicalize_enum_case(schema, "entity", []) == []


def test_closed_enum_walks_past_irrelevant_fields_and_reports_exact_value(monkeypatch):
    sv = load_validator(monkeypatch)
    schema = {
        "enums": {"closed": ["A", "B"]},
        "field_enums": {
            "missing": {"enum": "closed"},
            "open": {"enum": "closed", "extensible": True},
            "malformed": {"enum": "absent"},
            "value": {"enum": "closed"},
        },
    }
    fm = {"open": "novel", "malformed": "x", "value": ["A", "bad"]}
    reason = sv._enum_reject_reason(schema, "entity", fm)
    assert reason == "value='bad' not in enum 'closed' (A, B)"
    fm["value"] = ["A", "B"]
    assert sv._enum_reject_reason(schema, "entity", fm) is None


def test_evaluate_composed_root_reserved_and_exception_boundaries(monkeypatch, tmp_path):
    sv = load_validator(monkeypatch)
    vault = tmp_path / "vault"
    page = vault / "wiki" / "entities" / "a.md"
    page.parent.mkdir(parents=True)
    composed = vault / ".okengine" / "composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text(
        "apply_under: [wiki/]\nreserved_files: [special.md]\n"
        "okf: {required: [type]}\ntypes: {entity: {required: [type]}}\n"
    )
    good = "---\ntype: entity\nid: entity:a\n---\nx\n"
    assert sv._evaluate(str(page), good) == ("ok", None)
    assert sv._evaluate(str(page.with_name("special.md")), "not a page") == ("skip", None)
    assert sv._evaluate(str(vault / "outside.md"), good) == ("skip", None)

    monkeypatch.setattr(sv.yaml, "safe_load", lambda _text: (_ for _ in ()).throw(ValueError("z" * 200)))
    kind, reason = sv._evaluate(str(page), good)
    assert kind == "fail"
    assert reason is not None and len(reason.removeprefix("frontmatter is not valid YAML: ")) == 120


def test_profiles_missing_should_and_reserved_fallbacks_are_exact(monkeypatch, tmp_path):
    sv = load_validator(monkeypatch)
    monkeypatch.setattr(sv, "_evaluate", lambda *_args: ("fail", "bad"))
    assert sv.schema_reject_reason("x", "x") == "bad"
    assert sv.conformance_reject_reason("x", "x") == "bad"
    monkeypatch.setattr(sv, "_evaluate", lambda *_args: ("error", "infra"))
    assert sv.schema_reject_reason("x", "x") is None
    assert sv.conformance_reject_reason("x", "x") == "infra"

    monkeypatch.setattr(sv, "_base_schema", lambda: {"okf": {"should": ["id"]}})
    assert sv.missing_should("x", "plain") == []
    assert sv.missing_should("x", "---\ntype: entity\n---\n") == ["id"]

    monkeypatch.setattr(sv, "_find_schema", lambda _path: None)
    assert sv.reserved_files_for("x") == frozenset(sv._OKF_RESERVED_DEFAULT)
    monkeypatch.setattr(sv, "_find_schema", lambda _path: tmp_path / "schema.yaml")
    monkeypatch.setattr(sv, "_load_schema", lambda _path: {"reserved_files": ["Only.md"]})
    assert sv.reserved_files_for("x") == (
        frozenset(sv._OKF_RESERVED_DEFAULT) | frozenset({"only.md"})
    )


def test_main_continues_after_unreadable_input(monkeypatch, tmp_path, capsys):
    sv = load_validator(monkeypatch)
    good = tmp_path / "good.md"
    good.write_text("x")
    monkeypatch.setattr(sv, "schema_reject_reason", lambda _path, _content: "bad")
    assert sv.main([str(tmp_path / "missing.md"), str(good)]) == 1
    output = capsys.readouterr().out
    assert "cannot read" in output and f"✗ {good}" in output
    assert "2 file(s) fail schema conformance" in output
