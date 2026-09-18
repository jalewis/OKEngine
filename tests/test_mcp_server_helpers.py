"""Unit coverage for the read-MCP's path/graph helpers (okengine#462, tranche T1).

`okengine-mcp/server.py` is what serves pages, search and the knowledge graph to
agents. Its small helpers carry real safety and correctness duties that the tool
functions above them assume:

  _safe()          refuses path escapes outside the vault wiki/ (containment)
  _clamp_limit()   stops a caller pulling an unbounded result set (okengine#51)
  _resolve_key()   maps an agent-supplied name/path onto a canonical graph key
  _forward_links() parses a page's outbound wikilinks
  _fmt_refs()      renders reference lists with a cap

These are asserted for behaviour — especially the refusals, which are the ones
that matter when an agent supplies a hostile or sloppy argument.
"""
from __future__ import annotations

import importlib.util
import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp/server.py"


def load(monkeypatch, vault):
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    sys.modules.pop("okengine_server_helpers", None)
    spec = importlib.util.spec_from_file_location("okengine_server_helpers", SRV)
    module = importlib.util.module_from_spec(spec)
    sys.modules["okengine_server_helpers"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def srv(tmp_path, monkeypatch):
    return load(monkeypatch, tmp_path), tmp_path


def test_projection_tools_require_full_scope_and_delegate(srv, monkeypatch):
    module, _ = srv
    token = module._caller_var.set({"kind": "extension", "read_scopes": ["entities/**"]})
    try:
        with pytest.raises(PermissionError, match="full-vault"):
            module._authorize_projection_query()
    finally:
        module._caller_var.reset(token)

    token = module._caller_var.set({"kind": "extension", "read_scopes": ["wiki/**"]})
    try:
        module._authorize_projection_query()
    finally:
        module._caller_var.reset(token)
    module._authorize_projection_query()  # trusted local/admin

    calls = []
    async def status(): calls.append("status"); return {"ok": True}
    async def count(*args, **kwargs): calls.append(("count", args, kwargs)); return {"count": 1}
    async def find(*args, **kwargs): calls.append(("find", args, kwargs)); return {"results": []}
    async def meta(*args): calls.append(("meta", args)); return {"found": True}
    async def links(*args): calls.append(("links", args)); return {"results": []}
    monkeypatch.setattr(module._projection, "projection_status", status)
    monkeypatch.setattr(module._projection, "count_pages", count)
    monkeypatch.setattr(module._projection, "find_pages", find)
    monkeypatch.setattr(module._projection, "get_page_meta", meta)
    monkeypatch.setattr(module._projection, "find_links", links)

    async def invoke():
        assert (await module.projection_status())["ok"] is True
        assert (await module.count_pages(
            "entities", published_after="2026-01-01", updated_after="2026-07-01"))["count"] == 1
        await module.find_projected_pages(
            "entities", published_before="2026-12-31", updated_before="2026-08-10",
            order="updated_desc")
        await module.get_projected_page_meta("entity-1")
        await module.find_projected_links("entities/a", resolution="slug")
    asyncio.run(invoke())
    assert [call if isinstance(call, str) else call[0] for call in calls] == [
        "status", "count", "find", "meta", "links"]
    count_call = next(call for call in calls if not isinstance(call, str) and call[0] == "count")
    assert count_call[1] == ("entities", "", "", False)
    assert count_call[2] == {"published_after": "2026-01-01", "published_before": "",
                             "updated_after": "2026-07-01", "updated_before": ""}
    find_call = next(call for call in calls if not isinstance(call, str) and call[0] == "find")
    assert find_call[1] == ("entities", "", "", False)
    assert find_call[2] == {"published_after": "", "published_before": "2026-12-31",
                            "updated_after": "", "updated_before": "2026-08-10",
                            "order": "updated_desc", "limit": 40}
    assert next(call for call in calls if not isinstance(call, str) and call[0] == "links")[1] == (
        "entities/a", "", "slug", 40)


# ── _safe: vault containment ───────────────────────────────────────────────────

def test_safe_resolves_inside_the_vault_and_appends_md(srv):
    mod, vault = srv
    (vault / "wiki" / "entities").mkdir(parents=True)
    resolved = mod._safe("entities/acme")
    assert resolved is not None
    assert resolved.name == "acme.md"
    assert resolved.is_relative_to((vault / "wiki").resolve())

    # a leading slash is tolerated, not treated as filesystem-absolute
    assert mod._safe("/entities/acme") == resolved


def test_safe_appends_rather_than_replacing_a_dotted_slug(srv):
    """with_suffix() would truncate 'openssl-3.0.7-advisory' to 'openssl-3.0' and 404
    the page, desyncing the read path from the write path. Pinned deliberately."""
    mod, _ = srv
    assert mod._safe("cves/openssl-3.0.7-advisory").name == "openssl-3.0.7-advisory.md"


def test_safe_matches_write_path_prefix_and_entity_normalization(srv):
    mod, vault = srv
    canonical = vault / "wiki" / "entities" / "a" / "acme-corp.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# Acme\n")

    assert mod._safe("entities/acme-corp") == canonical.resolve()
    assert mod._safe("wiki/entities/acme-corp") == canonical.resolve()
    assert mod._safe(str(vault / "wiki" / "entities" / "acme-corp")) == canonical.resolve()


def test_safe_preserves_existing_two_level_entity_reshard(srv):
    mod, vault = srv
    canonical = vault / "wiki" / "entities" / "a" / "c" / "acme-corp.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("# Acme\n")

    assert mod._safe("entities/acme-corp") == canonical.resolve()


def test_safe_prefers_canonical_entity_over_stale_flat_or_one_level_duplicate(srv):
    mod, vault = srv
    flat = vault / "wiki/entities/acme-corp.md"
    one = vault / "wiki/entities/a/acme-corp.md"
    two = vault / "wiki/entities/a/c/acme-corp.md"
    two.parent.mkdir(parents=True)
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text("stale flat")
    one.write_text("stale one-level")
    two.write_text("canonical")

    assert mod._safe("entities/acme-corp") == two.resolve()
    assert mod._safe("entities/a/acme-corp") == two.resolve()


def test_safe_preserves_flat_legacy_and_special_entity_files_without_canonical(srv):
    mod, vault = srv
    entities = vault / "wiki/entities"
    entities.mkdir(parents=True)
    flat = entities / "legacy.md"
    index = entities / "INDEX.md"
    flat.write_text("legacy")
    index.write_text("index")

    assert mod._safe("entities/legacy") == flat.resolve()
    assert mod._safe("entities/INDEX") == index.resolve()


def test_safe_strips_repeated_absolute_vault_prefixes(srv):
    mod, vault = srv
    page = vault / "wiki/entities/a/acme.md"
    page.parent.mkdir(parents=True)
    page.write_text("acme")
    doubled = str(vault / "wiki") + str(vault / "wiki/entities/acme")

    assert mod._safe(doubled) == page.resolve()


def test_safe_refuses_escapes_outside_the_vault(srv):
    mod, _ = srv
    for escape in ("../../etc/passwd", "entities/../../../../etc/hosts", "../outside"):
        assert mod._safe(escape) is None, f"{escape!r} must be refused"


def test_safe_handles_resolution_and_entity_probe_errors(srv, monkeypatch):
    mod, vault = srv
    wiki = vault / "wiki"
    original_resolve = Path.resolve
    original_is_file = Path.is_file
    wiki_resolves = 0

    def fail_first_wiki_resolve(path, *args, **kwargs):
        nonlocal wiki_resolves
        if path == wiki:
            wiki_resolves += 1
            if wiki_resolves == 1:
                raise OSError("race")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_first_wiki_resolve)
    assert mod._safe("concepts/example") is not None

    monkeypatch.setattr(Path, "resolve", original_resolve)
    (wiki / "entities/a/c").mkdir(parents=True)
    two = wiki / "entities/a/c/acme.md"
    monkeypatch.setattr(
        Path, "is_file",
        lambda path: (_ for _ in ()).throw(OSError("race"))
        if path == two else original_is_file(path),
    )
    assert mod._safe("entities/acme") == (wiki / "entities/a/acme.md").resolve()
    assert mod._safe("entities/.md") == (wiki / "entities/.md.md").resolve()


# ── _clamp_limit: bounded result sets (okengine#51) ────────────────────────────

def test_clamp_limit_bounds_and_coerces(srv):
    mod, _ = srv
    assert mod._clamp_limit(5, 8) == 5
    assert mod._clamp_limit(0, 8) == 1, "floor is 1"
    assert mod._clamp_limit(-3, 8) == 1
    assert mod._clamp_limit(10 ** 9, 8) == mod._LIMIT_MAX, "ceiling is _LIMIT_MAX"
    assert mod._clamp_limit("12", 8) == 12, "numeric strings coerce"


def test_clamp_limit_falls_back_to_default_for_junk(srv):
    mod, _ = srv
    for junk in (None, "many", [], {}, object()):
        assert mod._clamp_limit(junk, 8) == 8


# ── _resolve_key: name/path -> canonical graph key ─────────────────────────────

def test_resolve_key_prefers_an_exact_artifact_key(srv):
    mod, _ = srv
    bl = {"entities/a/acme": [{"key": "sources/2026/x", "title": "X"}]}
    assert mod._resolve_key("entities/a/acme", bl) == "entities/a/acme"
    assert mod._resolve_key("/entities/a/acme.md", bl) == "entities/a/acme", \
        "slashes and the .md suffix are normalised away"


def test_resolve_key_falls_back_to_a_page_on_disk(srv):
    mod, vault = srv
    (vault / "wiki" / "concepts").mkdir(parents=True)
    (vault / "wiki" / "concepts" / "phishing.md").write_text("# P", encoding="utf-8")
    assert mod._resolve_key("concepts/phishing", {}) == "concepts/phishing"


def test_resolve_key_accepts_a_unique_bare_name(srv):
    mod, _ = srv
    bl = {"entities/a/acme": [], "entities/b/other": []}
    assert mod._resolve_key("acme", bl) == "entities/a/acme"
    assert mod._resolve_key("ACME", bl) == "entities/a/acme", "case-insensitive slug match"


def test_resolve_key_refuses_an_ambiguous_bare_name(srv):
    """Two shards can hold the same slug; guessing one would attach the graph to the
    wrong page, so ambiguity must resolve to None rather than pick arbitrarily."""
    mod, _ = srv
    bl = {"entities/a/acme": [], "sources/2026/acme": []}
    assert mod._resolve_key("acme", bl) is None


def test_resolve_key_scans_referrer_keys_for_pages_with_no_inbound_links(srv):
    mod, _ = srv
    bl = {"entities/a/acme": [{"key": "sources/2026/lonely", "title": "L"}]}
    assert mod._resolve_key("lonely", bl) == "sources/2026/lonely"


def test_resolve_key_returns_none_when_nothing_matches(srv):
    mod, _ = srv
    assert mod._resolve_key("nonexistent", {"entities/a/acme": []}) is None


# ── _forward_links / _fmt_refs ─────────────────────────────────────────────────

def test_forward_links_parses_unique_outbound_wikilinks_in_order(srv):
    mod, vault = srv
    (vault / "wiki" / "entities").mkdir(parents=True)
    (vault / "wiki" / "entities" / "acme.md").write_text(
        "sees [[concepts/phishing]] and [[sources/2026/x]] and [[concepts/phishing]] again",
        encoding="utf-8")
    assert mod._forward_links("entities/acme") == ["concepts/phishing", "sources/2026/x"]


def test_forward_links_is_empty_for_an_unreadable_page(srv):
    mod, _ = srv
    assert mod._forward_links("entities/does-not-exist") == []


def test_fmt_refs_renders_strings_and_records_and_caps_with_a_tail_count(srv):
    mod, _ = srv
    lines = mod._fmt_refs("Referenced by", [{"key": "a/b", "title": "T"}], 50)
    assert lines[0] == "## Referenced by (1)"
    assert lines[1] == "- [[a/b]] — T"

    assert mod._fmt_refs("References", ["a/b"], 50)[1] == "- [[a/b]]"

    capped = mod._fmt_refs("Referenced by", [f"n/{i}" for i in range(5)], 2)
    assert capped[0] == "## Referenced by (5)"
    assert capped[-1] == "- … and 3 more"
    assert len(capped) == 4, "header + cap + tail line"
