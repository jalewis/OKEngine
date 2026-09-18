"""Coverage for schema_validator's policy accessors and CLI (okengine#462, tranche T1).

`tools/schema_validator.py` is the OKF conformance contract and the write-guard hook.
Four of its surfaces were effectively untested:

  governing_policy()   -> the permissions/review policy the write path enforces
  reserved_files_for() -> the basenames the write path REFUSES and the gate EXEMPTS
  drift_policy()       -> field/value alias normalisation applied to agent writes
  main()               -> the pre-commit / drift-lint CLI (strict vs runtime profile)

All four are documented as *never raising* (fail-open) — a promise only a test can
keep honest, because a raise here would brick a write or a commit gate. Each test
asserts the returned policy content, not merely that the call succeeded.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
SV_MOD = REPO / "tools" / "schema_validator.py"

SCHEMA = """\
okf:
  required: [type]
  should: [id, title]
types:
  source:
    required: [type]
strict_types: false
permissions:
  create: [source]
  deny_namespaces: [private]
review:
  confidence_field: confidence
reserved_files:
  - INDEX.md
  - HEALTH.md
field_aliases:
  country: suspected_origin
value_aliases:
  suspected_origin:
    CN: China
allowed:
  source: [publisher, raw]
"""


def load_sv():
    """Fresh module instance per test — _find_schema/_load_schema memoise per directory."""
    spec = importlib.util.spec_from_file_location("schema_validator_policy", SV_MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    return tmp_path


@pytest.fixture
def sv():
    return load_sv()


# ── governing_policy ───────────────────────────────────────────────────────────

def test_governing_policy_returns_pack_permissions_and_review(sv, vault):
    page = vault / "wiki" / "sources" / "a.md"
    page.write_text("---\ntype: source\n---\n# A\n", encoding="utf-8")

    policy = sv.governing_policy(str(page))
    assert policy["permissions"]["create"] == ["source"]
    assert policy["permissions"]["deny_namespaces"] == ["private"]
    assert policy["review"]["confidence_field"] == "confidence"


def test_governing_policy_is_empty_without_a_governing_schema(sv, tmp_path):
    """No schema anywhere up the tree => no extra restrictions (documented behaviour)."""
    orphan = tmp_path / "nowhere" / "page.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\ntype: source\n---\n", encoding="utf-8")
    assert sv.governing_policy(str(orphan)) == {}


def test_governing_policy_omits_non_mapping_blocks(sv, tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "okf:\n  required: [type]\npermissions: not-a-mapping\nreview: [1, 2]\n",
        encoding="utf-8")
    page = tmp_path / "wiki" / "a.md"
    page.write_text("---\ntype: source\n---\n", encoding="utf-8")
    assert sv.governing_policy(str(page)) == {}


def test_governing_policy_never_raises_on_a_broken_schema(sv, tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("okf: [unclosed\n", encoding="utf-8")
    page = tmp_path / "wiki" / "a.md"
    page.write_text("---\ntype: source\n---\n", encoding="utf-8")
    assert sv.governing_policy(str(page)) == {}


# ── reserved_files_for ─────────────────────────────────────────────────────────

def test_pack_declared_reserved_files_extend_engine_defaults(sv, vault):
    page = vault / "wiki" / "sources" / "a.md"
    page.write_text("---\ntype: source\n---\n", encoding="utf-8")
    reserved = sv.reserved_files_for(str(page))
    assert "index.md" in reserved and "health.md" in reserved
    assert "readme.md" in reserved and "agents.md" in reserved
    assert all(name == name.lower() for name in reserved), "must be lowercased for comparison"


def test_engine_default_reserved_files_apply_without_a_pack_declaration(sv, tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("okf:\n  required: [type]\n", encoding="utf-8")
    page = tmp_path / "wiki" / "a.md"
    page.write_text("---\ntype: source\n---\n", encoding="utf-8")

    reserved = sv.reserved_files_for(str(page))
    assert reserved == frozenset(sv._OKF_RESERVED_DEFAULT)
    assert reserved, "the engine default must never be empty — it guards the always-present files"


def test_reserved_files_falls_back_to_the_engine_default_with_no_schema(sv, tmp_path):
    orphan = tmp_path / "nowhere" / "page.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\ntype: source\n---\n", encoding="utf-8")
    assert sv.reserved_files_for(str(orphan)) == frozenset(sv._OKF_RESERVED_DEFAULT)


# ── drift_policy ───────────────────────────────────────────────────────────────

def test_drift_policy_exposes_field_and_value_aliases(sv, vault):
    page = vault / "wiki" / "sources" / "a.md"
    page.write_text("---\ntype: source\n---\n", encoding="utf-8")

    policy = sv.drift_policy(str(page))
    assert policy["field_aliases"] == {"country": "suspected_origin"}
    assert policy["value_aliases"] == {"suspected_origin": {"CN": "China"}}
    assert policy["allowed"] == {"source": ["publisher", "raw"]}


def test_drift_policy_is_empty_when_unset_or_ungoverned(sv, tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("okf:\n  required: [type]\n", encoding="utf-8")
    page = tmp_path / "wiki" / "a.md"
    page.write_text("---\ntype: source\n---\n", encoding="utf-8")
    assert sv.drift_policy(str(page)) == {}

    orphan = tmp_path / "nowhere" / "p.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("---\ntype: source\n---\n", encoding="utf-8")
    assert sv.drift_policy(str(orphan)) == {}


# ── missing_should (advisory tier — must never reject) ─────────────────────────

def test_missing_should_lists_absent_advisory_fields(sv, vault, monkeypatch):
    """The engine base `okf.should` is deliberately EMPTY today (`id` was promoted to
    required), so the advisory tier is inert in-tree. Drive the mechanism directly so a
    future `should` promotion is covered the day it lands."""
    monkeypatch.setattr(sv, "_base_schema",
                        lambda: {"okf": {"required": ["type", "id"],
                                         "should": ["title", "description"]}})
    page = vault / "wiki" / "sources" / "a.md"
    page.write_text("---\ntype: source\nid: s-1\n---\n# A\n", encoding="utf-8")
    assert sorted(sv.missing_should(str(page), page.read_text())) == ["description", "title"]

    complete = "---\ntype: source\nid: s-1\ntitle: A\ndescription: d\n---\n# A\n"
    assert sv.missing_should(str(page), complete) == []


def test_missing_should_is_inert_while_the_base_should_list_is_empty(sv, vault):
    page = vault / "wiki" / "sources" / "a.md"
    content = "---\ntype: source\nid: s-1\n---\n# A\n"
    page.write_text(content, encoding="utf-8")
    assert sv.missing_should(str(page), content) == []


def test_missing_should_is_silent_without_parseable_frontmatter(sv, vault):
    page = vault / "wiki" / "sources" / "a.md"
    page.write_text("x", encoding="utf-8")
    assert sv.missing_should(str(page), "no frontmatter here") == []
    assert sv.missing_should(str(page), "---\n- a\n- b\n---\n") == []


def test_missing_should_never_rejects_a_write(sv, vault, monkeypatch):
    """Advisory only: a page missing every `should` field must still pass the gate."""
    monkeypatch.setattr(sv, "_base_schema",
                        lambda: {"okf": {"required": ["type", "id"],
                                         "should": ["title", "description"]}})
    page = vault / "wiki" / "sources" / "a.md"
    content = "---\ntype: source\nid: s-1\n---\n# A\n"
    page.write_text(content, encoding="utf-8")
    assert sv.missing_should(str(page), content)                 # advisories exist
    assert sv.schema_reject_reason(str(page), content) is None   # but do not reject


# ── main(): the pre-commit / drift-lint CLI ────────────────────────────────────

def test_cli_passes_a_conformant_page_and_reports_advisories(sv, vault, capsys, monkeypatch):
    monkeypatch.setattr(sv, "_base_schema",
                        lambda: {"okf": {"required": ["type", "id"], "should": ["title"]}})
    page = vault / "wiki" / "sources" / "ok.md"
    page.write_text("---\ntype: source\nid: s-1\n---\n# OK\n", encoding="utf-8")

    assert sv.main([str(page)]) == 0
    assert "should-warn" in capsys.readouterr().out

    # --quiet suppresses the advisory tier but still passes the gate
    assert sv.main(["--quiet", str(page)]) == 0
    assert "should-warn" not in capsys.readouterr().out


def test_cli_fails_a_nonconformant_page(sv, vault, capsys):
    page = vault / "wiki" / "sources" / "bad.md"
    page.write_text("---\ntype: source\n---\n# no id\n", encoding="utf-8")

    assert sv.main([str(page)]) == 1
    out = capsys.readouterr().out
    assert "✗" in out and "id" in out and "1 file(s) fail" in out


def test_cli_reports_unreadable_paths_as_failures(sv, tmp_path, capsys):
    missing = tmp_path / "does-not-exist.md"
    assert sv.main([str(missing)]) == 1
    out = capsys.readouterr().out
    assert "cannot read" in out and "1 file(s) fail" in out


def test_cli_strict_profile_fails_closed_where_runtime_passes(sv, tmp_path, capsys):
    """--strict is the CI/release gate: an ungoverned page is a FAILURE there,
    while the runtime profile lets the same write through (never brick a write)."""
    ungoverned = tmp_path / "loose" / "page.md"
    ungoverned.parent.mkdir(parents=True)
    ungoverned.write_text("---\ntype: source\n---\n# P\n", encoding="utf-8")

    assert sv.main([str(ungoverned)]) == 0
    capsys.readouterr()

    assert sv.main(["--strict", str(ungoverned)]) == 1
    out = capsys.readouterr().out
    assert "✗" in out and "strict conformance" in out


def test_schema_discovery_and_yaml_unavailable_edges(sv, tmp_path, monkeypatch):
    page = tmp_path / "vault" / "wiki" / "a.md"
    page.parent.mkdir(parents=True)
    composed = tmp_path / "vault" / ".okengine" / "composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text("types: {}\n")
    assert sv._find_schema(str(page)) == composed

    original_resolve = sv.Path.resolve
    monkeypatch.setattr(
        sv.Path, "resolve",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("bad path"))
        if self == page.parent else original_resolve(self, *a, **k),
    )
    sv._dir_to_schema.clear()
    assert sv._find_schema(str(page)) is None
    monkeypatch.setattr(sv, "yaml", None)
    assert sv._base_schema() == {}
    assert sv._load_schema(composed) is None


def test_enum_helpers_ignore_non_governing_shapes(sv, monkeypatch):
    monkeypatch.setattr(sv, "_base_schema", lambda: {"okf": {}})
    assert sv._base_merged({}) == {}
    assert sv.canonicalize_enum_case({"enums": []}, "source", {}) == []
    assert sv.canonicalize_enum_case({"enums": {}}, "source", []) == []
    assert sv.canonicalize_enum_case({"enums": {}, "field_enums": {"x": {}}}, "source", {"x": "A"}) == []
    assert sv.canonicalize_enum_case(
        {"enums": {}, "field_enums": {"x": {"enum": "missing"}}}, "source", {"x": "A"}) == []
    assert sv._enum_reject_reason({"enums": ["bad"], "field_enums": {}}, "source", {}) is None
    assert sv._enum_reject_reason({"enums": {}, "field_enums": {}}, "source", {}) is None
    assert sv._enum_reject_reason(
        {"enums": {}, "field_enums": {"x": {"enum": "missing"}}}, "source", {"x": "A"}) is None


def test_evaluate_all_scope_and_frontmatter_edges(sv, tmp_path, monkeypatch):
    root = tmp_path / "vault"
    schema_path = root / "schema.yaml"
    root.mkdir()
    schema = {"apply_under": ["wiki/"], "exclude": ["wiki/excluded/"], "types": {}}
    monkeypatch.setattr(sv, "_find_schema", lambda _path: schema_path)
    monkeypatch.setattr(sv, "_load_schema", lambda _path: schema)
    monkeypatch.setattr(sv, "_base_schema", lambda: {})
    assert sv._evaluate(str(tmp_path / "outside.md"), "") == ("skip", None)
    assert sv._evaluate(str(root / "other" / "a.md"), "") == ("skip", None)
    assert sv._evaluate(str(root / "wiki" / "a.txt"), "") == ("skip", None)
    assert sv._evaluate(str(root / "wiki" / "excluded" / "a.md"), "") == ("skip", None)
    assert sv._evaluate(str(root / "wiki" / "a.md"), "body")[0] == "fail"

    previous_yaml = sv.yaml
    monkeypatch.setattr(sv, "yaml", None)
    assert sv._evaluate(str(root / "wiki" / "a.md"), "---\ntype: x\n---\n")[0] == "error"
    monkeypatch.setattr(sv, "yaml", previous_yaml)
    assert sv._evaluate(str(root / "wiki" / "a.md"), "---\n- one\n---\n")[0] == "fail"
    assert sv._evaluate(str(root / "wiki" / "a.md"), "---\ntype: novel\n---\n") == ("ok", None)


def test_policy_accessors_and_should_never_raise(sv, monkeypatch):
    monkeypatch.setattr(sv, "_base_schema", lambda: {"okf": {"should": ["title"]}})
    assert sv.missing_should("page", "body") == []
    assert sv.missing_should("page", "---\n- item\n---\n") == []
    monkeypatch.setattr(sv, "_find_schema", lambda *_a: None)
    assert sv.drift_policy("page") == {}
    monkeypatch.setattr(sv, "_find_schema", lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sv.governing_policy("page") == {}
    assert sv.reserved_files_for("page") == frozenset()
    assert sv.drift_policy("page") == {}
    monkeypatch.setattr(sv, "_base_schema", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sv.missing_should("page", "---\ntype: source\n---\n") == []
