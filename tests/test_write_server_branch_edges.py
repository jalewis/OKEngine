"""Branch-focused tests for deterministic write-boundary helpers."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"


def _load(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {entities: {}, concepts: {}}}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    name = "write_server_branch_edges"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_item_shape_every_declared_shape_and_coercion(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    rules = {
        "items": {
            "_item": {"shape": "dict", "required": {"required"}},
            "enum": {"enum": {"UP", "DOWN"}},
            "number": {"shape": "number"},
            "date": {"shape": "date"},
            "text": {"shape": "str"},
            "flag": {"shape": "bool"},
            "values": {"shape": "list"},
            "mapping": {"shape": "dict"},
        }
    }
    monkeypatch.setattr(m, "_item_rules_for", lambda _p: rules)
    assert m._item_shape_reject(None, []) is None
    assert "must be an object" in m._item_shape_reject(None, {"items": ["bad"]})
    assert "missing required" in m._item_shape_reject(None, {"items": [{}]})

    base = {"required": "yes"}
    cases = [
        ({"enum": "sideways"}, "sanctioned vocabulary"),
        ({"number": "bad"}, "must be a number"),
        ({"date": "today"}, "must be an ISO date"),
        ({"text": 3}, "must be a string"),
        ({"flag": "true"}, "must be a boolean"),
        ({"values": "x"}, "must be a list"),
        ({"mapping": []}, "must be an object"),
    ]
    for extra, expected in cases:
        assert expected in m._item_shape_reject(
            None, {"items": [{**base, **extra}]})

    item = {**base, "enum": "up", "number": "2.5", "date": "2026-01-01",
            "text": "ok", "flag": True, "values": [], "mapping": {}}
    assert m._item_shape_reject(None, {"items": [item]}) is None
    assert item["enum"] == "UP" and item["number"] == 2.5


def test_validation_helpers_pin_schema_union_and_exact_item_contracts(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki" / "entities" / "x.md"
    governing_calls = []
    monkeypatch.setattr(m, "_base_list_fields", lambda: frozenset({"base-list"}))
    monkeypatch.setattr(m, "_base_int_fields", lambda: frozenset({"base-int"}))
    monkeypatch.setattr(m, "_base_item_rules", lambda: {"base-items": {}})
    monkeypatch.setattr(m, "_governing", lambda p: governing_calls.append(p) or {"domain": True})
    monkeypatch.setattr(m.schema_lib, "list_fields", lambda schema: {"domain-list"})
    monkeypatch.setattr(m.schema_lib, "int_fields", lambda schema: {"domain-int"})
    monkeypatch.setattr(m.schema_lib, "item_rules", lambda schema: {"domain-items": {}})

    assert m._list_fields_for(None) == {"base-list"}
    assert m._int_fields_for(None) == {"base-int"}
    assert m._item_rules_for(None) == {"base-items": {}}
    assert governing_calls == []
    assert m._list_fields_for(page) == {"base-list", "domain-list"}
    assert m._int_fields_for(page) == {"base-int", "domain-int"}
    assert m._item_rules_for(page) == {"base-items": {}, "domain-items": {}}
    assert governing_calls == [page, page, page]

    enum_calls = []
    monkeypatch.setattr(
        m, "canonicalize_enum_case",
        lambda schema, kind, fm: enum_calls.append((schema, kind, fm)) or fm.update(level="HIGH"),
    )
    fm = {"type": "finding", "level": "high"}
    m._enum_case_coerce(page, fm)
    assert fm == {"type": "finding", "level": "HIGH"}
    assert enum_calls == [({"domain": True}, "finding", fm)]
    m._enum_case_coerce(page, [])
    assert len(enum_calls) == 1

    dynamic = lambda value: "".join((value[:1], value[1:]))
    rules = {
        "items": {
            dynamic("_item"): {"shape": dynamic("dict"), "required": {"needed"}},
            "choice": {"enum": {"UP", "DOWN"}},
            "number": {"shape": dynamic("number")},
            "date": {"shape": dynamic("date")},
            "text": {"shape": dynamic("str")},
            "flag": {"shape": dynamic("bool")},
            "values": {"shape": dynamic("list")},
            "mapping": {"shape": dynamic("dict")},
        }
    }
    monkeypatch.setattr(m, "_item_rules_for", lambda _p: rules)
    long = "x" * 75
    assert m._item_shape_reject(page, {"items": [long]}) == (
        "`items[0]` must be an object — got str: " + repr(long[:60])
    )
    assert m._item_shape_reject(page, {"items": [{"needed": "   "}]}) == (
        "`items[0]` is missing required item field(s): needed"
    )
    # With no dict-only item contract, a legacy scalar and absent optional keys
    # continue to the later valid item.
    valid = {"needed": "yes", "choice": "up", "number": "2.5",
             "date": "2026-08-28", "text": "ok", "flag": True,
             "values": [], "mapping": {}}
    legacy_rules = {"items": {**rules["items"], "_item": {"required": {"needed"}}}}
    monkeypatch.setattr(m, "_item_rules_for", lambda _p: legacy_rules)
    assert m._item_shape_reject(page, {"items": ["legacy", valid]}) is None
    assert valid["choice"] == "UP" and valid["number"] == 2.5
    monkeypatch.setattr(m, "_item_rules_for", lambda _p: rules)
    invalid_enum = {"needed": "yes", "choice": long}
    assert m._item_shape_reject(page, {"items": [invalid_enum]}) == (
        f"`items[0].choice` = {long[:60]!r} is not in the sanctioned vocabulary "
        "(DOWN, UP). Resubmit the complete list using only those values."
    )
    for value, suffix in (
        ({"number": long}, f"must be a number — got str: {long[:60]!r}"),
        ({"date": long}, f"must be an ISO date (YYYY-MM-DD) — got {long[:60]!r}"),
        ({"text": 3}, "must be a string — got int"),
        ({"flag": 1}, "must be a boolean — got int"),
        ({"values": {}}, "must be a list — got dict"),
        ({"mapping": []}, "must be an object — got list"),
    ):
        result = m._item_shape_reject(page, {"items": [{"needed": "yes", **value}]})
        assert result == f"`items[0].{next(iter(value))}` {suffix}"

    monkeypatch.setattr(m, "_int_fields_for", lambda _p: {"count"})
    assert m._int_shape_reject(page, {"count": None}) is None
    assert m._int_shape_reject(page, {"count": 4}) is None
    invalid = "n" * 100
    assert m._int_shape_reject(page, {"count": invalid}) == (
        "field `count` must be an integer count (it is machine-computed by a metrics lane) "
        f"— got str: {invalid[:80]!r}. Drop the field; do not hand-author it."
    )


def test_compose_and_partition_short_circuit_are_exact(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m._compose({"z": "café", "a": 1}, None) == (
        "---\nz: café\na: 1\n---\n"
    )
    page = tmp_path / "wiki" / "concepts" / "x.md"
    monkeypatch.setattr(m, "_CONVERGE_OK", True)
    monkeypatch.setattr(m, "_qualified_namespace", lambda _p: "")
    called = []
    monkeypatch.setattr(m.okf_migrate, "is_partitioned", lambda *_a: called.append(True) or True)
    assert m._partitioned_create_path(page, {}) == page
    assert called == []
    monkeypatch.setattr(m, "_qualified_namespace", lambda _p: "concepts")
    monkeypatch.setattr(m.okf_migrate, "is_partitioned", lambda *_a: False)
    assert m._partitioned_create_path(page, {}) == page


def test_int_reference_and_frontmatter_coercion_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_int_fields_for", lambda _p: {"count"})
    assert m._int_shape_reject(None, []) is None
    fm = {"count": " 42 "}
    assert m._int_shape_reject(None, fm) is None and fm["count"] == 42
    assert "integer count" in m._int_shape_reject(None, {"count": True})
    assert m._normalize_refs("scalar") == "scalar"
    assert m._normalize_refs({
        "aliases": "a, b", "link": "[[concepts/x|X]]",
        "refs": [["entities/a"], "[[concepts/y]]", ""],
    }, {"aliases"}) == {
        "aliases": ["a", "b"], "link": "concepts/x",
        "refs": ["entities/a", "concepts/y"],
    }
    assert m._coerce_fm(None) == {}
    assert m._coerce_fm("null") == {}
    assert m._coerce_fm("- scalar") is None
    assert m._coerce_fm("[broken") is None


def test_read_page_review_reason_and_record_load_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/a.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("plain")
    assert m._read_page(page) == ({}, "plain")
    page.write_text("---\n- scalar\n---\nbody")
    assert m._read_page(page) == ({}, "body")
    page.write_text("---\na: [broken\n---\nbody")
    assert m._read_page(page) == ({}, "body")

    reasons = m._structured_review_reasons(
        {"conflicts": [{"field": "origin"}, "ignore"]},
        "## Grounding check\nunsupported claim",
        ["categorical certainty", "changed after approval", "degenerate draft", "other"],
    )
    assert {row["code"] for row in reasons} >= {
        "categorical-confidence", "changed-after-approval", "agent-draft",
        "manual", "conflict", "grounding"}
    assert m._structured_review_reasons({}, "")[0]["code"] == "legacy-unspecified"

    assert m._load_review_record("missing") == (None, None)
    rp = m._review_record_path("bad")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("[broken")
    assert m._load_review_record("bad") == (None, None)
    rp = m._review_record_path("scalar")
    rp.write_text("- x\n")
    assert m._load_review_record("scalar") == (None, None)


def test_review_apis_exercise_early_rejection_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_safe", lambda _path: None)
    bad_hash = "x"
    assert m._assign_review("p", "", 1, "0" * 64)["status"] == 400
    assert m._assign_review("p", "me", "bad", "0" * 64)["status"] == 400
    assert m._assign_review("p", "me", 1, bad_hash)["status"] == 400
    assert m._assign_review("p", "me", 1, "0" * 64)["status"] == 404

    assert m._resolve_review("p", "bogus", "me", "", 1, "0" * 64)["status"] == 400
    assert m._resolve_review("p", "approve", "", "", 1, "0" * 64)["status"] == 400
    assert m._resolve_review("p", "approve", "me", "", "bad", "0" * 64)["status"] == 400
    assert m._resolve_review("p", "approve", "me", "", 1, bad_hash)["status"] == 400
    assert m._resolve_review("p", "reject", "me", "", 1, "0" * 64)["status"] == 400
    assert m._resolve_review("p", "approve", "me", "", 1, "0" * 64)["status"] == 404

    assert m._record_machine_review("p", "bot", "bad")["status"] == 400
    assert m._record_machine_review("p", "bot", "supported")["status"] == 404


def test_frontmatter_namespace_policy_and_alias_helper_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/a.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("plain")
    assert m._frontmatter_error(page) is None
    page.write_text("---\ntype: x")
    assert "delimiters" in m._frontmatter_error(page)
    page.write_text("---\na: [broken\n---\n")
    assert "invalid frontmatter" in m._frontmatter_error(page)
    page.write_text("---\n- scalar\n---\n")
    assert "non-mapping" in m._frontmatter_error(page)

    assert m._qualified_namespace(tmp_path / "outside.md") == ""
    assert m._entities_scope("concepts/x") == ""
    monkeypatch.setattr(m, "_namespace", lambda _p: "")
    assert m._namespace_reject(page) is None
    assert m._type_namespace_reject(page, {}) is None
    assert m._type_namespace_reject(page, {"type": "actor"}) is None

    assert m._alias_matches(
        {"name": "One", "aliases": "Two, Three"}, "one", "two", set())
    assert not m._alias_matches(
        {"name": "One", "aliases": 3}, "one", "two", set())


def test_partition_enum_and_schema_cache_failure_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    monkeypatch.setattr(m, "_CONVERGE_OK", False)
    assert m._partitioned_create_path(page, {}) == page
    monkeypatch.setattr(m, "_CONVERGE_OK", True)
    monkeypatch.setattr(m, "_qualified_namespace", lambda _p: "")
    assert m._partitioned_create_path(page, {}) == page

    assert m._enum_case_coerce(page, []) is None
    monkeypatch.setattr(
        m, "canonicalize_enum_case",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("bad schema")))
    assert m._enum_case_coerce(page, {}) is None

    m._base_int_fields_cache = None
    monkeypatch.setattr(
        m.schema_lib, "int_fields",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("bad schema")))
    assert m._base_int_fields() == frozenset()
    m._base_item_rules_cache = None
    monkeypatch.setattr(
        m.schema_lib, "item_rules",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("bad schema")))
    assert m._base_item_rules() == {}


def test_time_caller_and_per_page_schema_helper_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-01-02")
    assert m._today() == "2026-01-02" and m._now() == "2026-01-02"
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "now")
    assert m._now() == "now"
    token=m._caller_var.set({"kind":"test","actor":"x"})
    try:assert m._caller()["actor"]=="x"
    finally:m._caller_var.reset(token)
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR"," cron:test ")
    assert m._caller()["kind"]=="job" and m._caller()["actor"]=="cron:test"
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR")
    assert m._caller()["kind"]=="admin"
    monkeypatch.setattr(m.schema_lib,"int_fields",lambda _s:{"local"})
    monkeypatch.setattr(m,"_governing",lambda _p:{})
    assert "local" in m._int_fields_for(tmp_path/"x")
    monkeypatch.setattr(m.schema_lib,"int_fields",lambda _s:(_ for _ in ()).throw(RuntimeError()))
    assert m._int_fields_for(tmp_path/"x")==set(m._base_int_fields())
    monkeypatch.setattr(m.schema_lib,"item_rules",lambda _s:{"local":{}})
    assert "local" in m._item_rules_for(tmp_path/"x")
    monkeypatch.setattr(m.schema_lib,"item_rules",lambda _s:(_ for _ in ()).throw(RuntimeError()))
    assert m._item_rules_for(tmp_path/"x")==m._base_item_rules()


def test_entity_backfill_normalization_and_rejections(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m,"_safe",lambda _p:tmp_path/"wiki/entities/x.md")
    monkeypatch.setattr(m,"_governing",lambda _p:{"types":{"actor":{},"publisher":{}}})
    monkeypatch.setattr(m.schema_lib,"canonical_types",lambda s:set(s["types"]))
    monkeypatch.setattr(m,"_type_namespace_reject",lambda *_:None)
    value,error=m._entity_backfill_frontmatter("entities/x","[broken")
    assert value is None and "invalid frontmatter" in error
    value,error=m._entity_backfill_frontmatter("entities/x","- scalar")
    assert value is None and "mapping" in error
    value,error=m._entity_backfill_frontmatter(
        "entities/x","id: sources:bad\ntype: publisher\ntitle: Example Corp")
    assert error is None
    fm=__import__("yaml").safe_load(value)
    assert "id" not in fm and fm["name"]=="Example Corp"
    value,error=m._entity_backfill_frontmatter(
        "entities/x","type: actor\nname: Threat Actor\nactor_type: unknown")
    assert value is None and "generic class labels" in error
    value,error=m._entity_backfill_frontmatter("entities/x","type: actor\nname: Named")
    assert value is None and "requires explicit actor_type" in error
    monkeypatch.setattr(m,"_type_namespace_reject",lambda *_:"wrong")
    value,error=m._entity_backfill_frontmatter("entities/x","type: actor\ntitle: Lab")
    assert error is None and __import__("yaml").safe_load(value)["type"]=="publisher"


def test_raw_backfill_and_selected_url_edges(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    fm,error=m._raw_backfill_frontmatter({"source_kind":"news"})
    assert error is None and fm["type"]=="source" and fm["source_kind"]=="news"
    fm,error=m._raw_backfill_frontmatter("source_kind: qualification")
    assert error is None and fm["source_kind"]=="report"
    fm,error=m._raw_backfill_frontmatter("[broken")
    assert fm is None and "invalid frontmatter" in error
    fm,error=m._raw_backfill_frontmatter("- scalar")
    assert fm is None and "mapping" in error
    assert m._selected_raw_url([])=="" and m._selected_raw_url(["x","y"])==""
    assert m._selected_raw_url(["sources/x"])==""
    monkeypatch.setenv("WIKI_PATH",str(tmp_path))
    assert m._selected_raw_url(["raw/missing.md"])==""
    raw=tmp_path/"raw/x.md";raw.parent.mkdir();raw.write_text("source_url: https://example.test/a\n")
    assert m._selected_raw_url(["/raw/x.md"])=="https://example.test/a"
    raw.write_text("no url")
    assert m._selected_raw_url(["raw/x.md"])==""
    monkeypatch.setattr(m, "_selected_raw_ref", lambda _selected: "raw/vanished.md")
    assert m._selected_raw_url(["raw/vanished.md"]) == ""


def test_queue_spacing_and_same_url_read_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    assert m._prepend_queue_row("header", "- row\n") == "header\n\n- row\n"
    existing = tmp_path / "wiki" / "sources" / "x.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("---\nurl: https://example.test/x\n---\n")
    monkeypatch.setattr(m, "_read_page", lambda _p: (_ for _ in ()).throw(OSError("race")))
    assert m._same_source_url({"url": "https://example.test/x"}, existing) is False
    monkeypatch.setattr(m, "_read_page", lambda _p: (_ for _ in ()).throw(ValueError("bad")))
    assert m._same_source_url({"url": "https://example.test/x"}, existing) is False
