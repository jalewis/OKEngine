"""okengine#965 (group C): pin the read-MCP path canonicalization and graph/list tool contracts.

These tests close surviving mutants in `okengine-mcp/server.py` for `_safe`, `_resolve_key`,
`_fmt_refs`, `_artifact_backlinks`, `find_references`, `retrieve_context`, `graph_stats` and
`list_pages`. Each test pins one observable contract: the path `_safe` resolves to (including
symlinked wiki roots, sharded entity seats and containment), artifact freshness, the exact
output caps, and which pages `list_pages` lists.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("mcp")

SRV = Path(__file__).resolve().parent.parent / "okengine-mcp" / "server.py"


def _load(monkeypatch, vault: Path):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    monkeypatch.delenv("OKENGINE_BACKLINKS_MAX_AGE", raising=False)
    monkeypatch.delenv("OKENGINE_MCP_LIMIT_MAX", raising=False)
    name = "okengine_server_mutants_c"
    spec = importlib.util.spec_from_file_location(name, SRV)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _touch(path: Path, text: str = "---\ntitle: t\n---\nbody\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "wiki").mkdir()
    return tmp_path


@pytest.fixture
def srv(vault, monkeypatch):
    return _load(monkeypatch, vault)


def _wiki(vault: Path) -> Path:
    return (vault / "wiki").resolve()


# ── _safe: root-prefix stripping ─────────────────────────────────────────────────────────────


@pytest.fixture
def symlinked(tmp_path, monkeypatch):
    """A vault whose wiki/ is a symlink to a directory NOT named `wiki` (real/store)."""
    store = tmp_path / "real" / "store"
    _touch(store / "concepts" / "a.md")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "wiki").symlink_to(store, target_is_directory=True)
    return _load(monkeypatch, vault), store.resolve()


def test_safe_strips_a_symlinked_wiki_root_before_its_parent(symlinked):
    m, store = symlinked
    got = m._safe(str(store) + "/concepts/a")
    assert got == store / "concepts" / "a.md", (
        f"an absolute path under the resolved wiki root must strip that root, not its parent: {got}")


def test_safe_strips_a_repeated_symlinked_wiki_root(symlinked):
    m, store = symlinked
    doubled = str(store) + "/" + str(store).lstrip("/") + "/concepts/a"
    got = m._safe(doubled)
    assert got == store / "concepts" / "a.md", (
        f"a doubly-joined wiki root must strip one whole root per pass: {got}")


def test_safe_refuses_the_wiki_root_itself(vault, srv):
    assert srv._safe(str(_wiki(vault))) is None, "the wiki root names no page and must be refused"


def test_safe_refuses_the_vault_root_itself(vault, srv):
    assert srv._safe(str(_wiki(vault).parent)) is None, "the vault root names no page and must be refused"


def test_safe_refuses_a_bare_wiki_segment(srv):
    assert srv._safe("/wiki") is None, "'/wiki' strips to the root and must be refused"


def test_safe_keeps_a_slug_that_sorts_after_wiki(vault, srv):
    got = srv._safe("zeta")
    assert got == _wiki(vault) / "zeta.md", f"'zeta' is not a wiki prefix and must survive: {got}"


# ── _safe: suffix handling ───────────────────────────────────────────────────────────────────


def test_safe_appends_md_to_a_dotted_slug(vault, srv):
    got = srv._safe("concepts/v1.zz")
    assert got == _wiki(vault) / "concepts" / "v1.zz.md", f"dotted slug must get .md appended: {got}"


def test_safe_does_not_follow_a_dotted_symlink_without_md(vault, srv):
    wiki = _wiki(vault)
    real = _touch(wiki / "concepts" / "real.md")
    (wiki / "concepts" / "link.zz").symlink_to(real)
    got = srv._safe("concepts/link.zz")
    assert got == wiki / "concepts" / "link.zz.md", (
        f"the read path must look up 'link.zz.md', never the extensionless symlink: {got}")


# ── _safe: entity shape and shard seats ──────────────────────────────────────────────────────


def test_safe_serves_a_flat_entity_index_over_a_sharded_slug(vault, srv):
    wiki = _wiki(vault)
    flat = _touch(wiki / "entities" / "index.md")
    _touch(wiki / "entities" / "i" / "index.md")
    got = srv._safe("entities/index")
    assert got == flat, f"entities/index is a special file, never an entity slug: {got}"


def test_safe_does_not_shard_a_non_entity_namespace(vault, srv):
    wiki = _wiki(vault)
    page = _touch(wiki / "people" / "foo.md")
    _touch(wiki / "entities" / "f" / "foo.md")
    got = srv._safe("people/foo")
    assert got == page, f"only the 'entities' namespace is sharded: {got}"


def test_safe_treats_the_bare_entities_namespace_as_flat(vault, srv):
    wiki = _wiki(vault)
    _touch(wiki / "entities" / "e" / "entities.md")
    got = srv._safe("entities")
    assert got == wiki / "entities.md", f"a one-segment path is not an entity slug: {got}"


def test_safe_reshelves_a_three_part_entity_path_to_its_shard(vault, srv):
    wiki = _wiki(vault)
    _touch(wiki / "entities" / "x" / "foo.md")
    seat = _touch(wiki / "entities" / "f" / "o" / "foo.md")
    got = srv._safe("entities/x/foo")
    assert got == seat, f"a 3-part entity path must resolve to the existing two-level seat: {got}"


def test_safe_reshelves_a_four_part_entity_path_to_its_shard(vault, srv):
    wiki = _wiki(vault)
    _touch(wiki / "entities" / "a" / "b" / "foo.md")
    seat = _touch(wiki / "entities" / "f" / "o" / "foo.md")
    got = srv._safe("entities/a/b/foo")
    assert got == seat, f"a 4-part entity path must resolve to the existing two-level seat: {got}"


def test_safe_does_not_treat_an_empty_segment_as_a_shard(vault, srv):
    wiki = _wiki(vault)
    flat = _touch(wiki / "entities" / "foo.md")
    _touch(wiki / "entities" / "f" / "o" / "foo.md")
    got = srv._safe("entities//foo")
    assert got == flat, f"an empty middle segment is not a one-character shard: {got}"


def test_safe_strips_md_from_the_last_segment_of_a_sharded_path(vault, srv):
    wiki = _wiki(vault)
    seat = _touch(wiki / "entities" / "f" / "o" / "foobar.md")
    got = srv._safe("entities/f/foobar.md")
    assert got == seat, f"the stem comes from the LAST segment minus '.md': {got}"


def test_safe_handles_a_one_character_entity_stem(vault, srv):
    got = srv._safe("entities/x")
    assert got == _wiki(vault) / "entities" / "x.md", f"a 1-character stem must not crash: {got}"


def test_safe_uses_the_second_character_for_a_two_character_stem(vault, srv):
    seat = _touch(_wiki(vault) / "entities" / "a" / "b" / "ab.md")
    got = srv._safe("entities/ab")
    assert got == seat, f"'ab' shards under entities/a/b/: {got}"


@pytest.fixture
def escaping_entity(tmp_path, monkeypatch):
    """entities/x is a symlink to a directory outside the wiki."""
    outside = tmp_path / "outside"
    _touch(outside / "foo.md")
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    (wiki / "entities" / "x").symlink_to(outside, target_is_directory=True)
    return _load(monkeypatch, tmp_path), wiki


def test_safe_contains_an_escaping_entity_path_when_a_shard_tree_exists(escaping_entity):
    m, wiki = escaping_entity
    (wiki / "entities" / "f" / "z").mkdir(parents=True)
    assert m._safe("entities/x/foo") is None, "a path escaping the wiki must be refused"


def test_safe_contains_an_escaping_entity_path_without_a_shard_tree(escaping_entity):
    m, _wiki_dir = escaping_entity
    assert m._safe("entities/x/foo") is None, "a path escaping the wiki must be refused"


# ── backlink artifact freshness ──────────────────────────────────────────────────────────────


def _artifact(vault: Path, doc: dict) -> Path:
    path = vault / "wiki" / ".backlinks.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_artifact_backlinks_rejects_an_artifact_older_than_max_age(vault, srv):
    path = _artifact(vault, {"backlinks": {"a": []}})
    old = os.stat(path).st_mtime - srv._BL_ARTIFACT_MAX_AGE - 1000
    os.utime(path, (old, old))
    assert srv._artifact_backlinks() is None, "a stale artifact must be refused"


def test_artifact_backlinks_rejects_an_epoch_era_artifact(vault, srv):
    path = _artifact(vault, {"backlinks": {"a": []}})
    os.utime(path, (1, 1))
    assert srv._artifact_backlinks() is None, "an artifact from 1970 must be refused as stale"


def test_artifact_backlinks_accepts_an_artifact_exactly_max_age_old(vault, srv, monkeypatch):
    path = _artifact(vault, {"backlinks": {"a": []}})
    built = 1_700_000_000
    os.utime(path, ns=(built * 10**9, built * 10**9))
    clock = types.SimpleNamespace(time=lambda: float(built + srv._BL_ARTIFACT_MAX_AGE))
    monkeypatch.setattr(srv, "time", clock)
    assert srv._artifact_backlinks() == {"a": []}, "the max age itself is still fresh"


# ── _resolve_key / _fmt_refs ─────────────────────────────────────────────────────────────────


def test_find_references_resolves_a_bare_name_by_its_last_segment(vault, srv):
    _artifact(vault, {"backlinks": {"concepts/foo": [{"key": "notes/r", "title": "R"}]}})
    out = srv.find_references("a/b/foo")
    assert out.startswith("# concepts/foo\n"), f"'a/b/foo' must resolve by basename 'foo': {out!r}"


def test_find_references_refuses_a_basename_that_matches_only_by_ordering(vault, srv):
    _artifact(vault, {"backlinks": {"concepts/alpha": [], "concepts/beta": []}})
    out = srv.find_references("q/beta")
    assert out.startswith("# concepts/beta\n"), f"only an EQUAL basename may match: {out!r}"


def test_find_references_matches_a_referrer_basename_only_by_equality(vault, srv):
    refs = [{"key": "notes/alpha", "title": "A"}, {"key": "notes/beta", "title": "B"}]
    _artifact(vault, {"backlinks": {"concepts/x": refs}})
    out = srv.find_references("q/beta")
    assert out.startswith("# notes/beta\n"), f"only an EQUAL referrer basename may match: {out!r}"


@pytest.mark.parametrize("count", [0, 3, 5])
def test_fmt_refs_adds_no_overflow_line_within_the_cap(srv, count):
    items = [f"k{i}" for i in range(count)]
    got = srv._fmt_refs("H", items, 5)
    assert got == [f"## H ({count})"] + [f"- [[k{i}]]" for i in range(count)], (
        f"no '… and N more' line when {count} items fit a cap of 5: {got}")


# ── find_references caps ────────────────────────────────────────────────────────────────────


def _section(out: str, head: str) -> list[str]:
    lines = out.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(f"## {head} ("))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("- "):
            break
        body.append(line)
    return body


def test_find_references_lists_exactly_fifty_backlinks(vault, srv):
    refs = [{"key": f"notes/r{i:02d}", "title": "T"} for i in range(51)]
    _artifact(vault, {"backlinks": {"concepts/hub": refs}})
    body = _section(srv.find_references("concepts/hub"), "Referenced by")
    assert len(body) == 51 and body[-1] == "- … and 1 more", (
        f"50 listed backlinks plus one overflow line expected, got {len(body)}: {body[-2:]}")


def test_find_references_lists_exactly_fifty_forward_links(vault, srv):
    _touch(vault / "wiki" / "concepts" / "hub.md", "".join(f"[[f{i:02d}]]\n" for i in range(51)))
    _artifact(vault, {"backlinks": {"concepts/hub": []}})
    body = _section(srv.find_references("concepts/hub"), "References")
    assert len(body) == 51 and body[-1] == "- … and 1 more", (
        f"50 listed forward links plus one overflow line expected, got {len(body)}: {body[-2:]}")


def test_find_references_caps_output_at_8000_characters(vault, srv):
    refs = [{"key": f"notes/r{i:02d}", "title": "T" * 300} for i in range(50)]
    _artifact(vault, {"backlinks": {"concepts/hub": refs}})
    out = srv.find_references("concepts/hub")
    assert len(out) == 8000, f"find_references output must be cut at 8000 chars, got {len(out)}"


# ── retrieve_context caps ───────────────────────────────────────────────────────────────────


def test_retrieve_context_cuts_the_page_body_at_12000_characters(vault, srv):
    _touch(vault / "wiki" / "concepts" / "big.md", "a" * 13000)
    _artifact(vault, {"backlinks": {"concepts/big": []}})
    out = srv.retrieve_context("concepts/big")
    assert out.index("\n") == 12000, f"body must be cut at 12000 chars, got {out.index(chr(10))}"


def test_retrieve_context_lists_exactly_thirty_backlinks(vault, srv):
    _touch(vault / "wiki" / "concepts" / "hub.md", "body\n")
    refs = [{"key": f"notes/r{i:02d}", "title": "T"} for i in range(31)]
    _artifact(vault, {"backlinks": {"concepts/hub": refs}})
    body = _section(srv.retrieve_context("concepts/hub"), "Incoming backlinks")
    assert len(body) == 31 and body[-1] == "- … and 1 more", (
        f"30 listed backlinks plus one overflow line expected, got {len(body)}: {body[-2:]}")


def test_retrieve_context_lists_exactly_thirty_forward_links(vault, srv):
    _touch(vault / "wiki" / "concepts" / "hub.md", "".join(f"[[f{i:02d}]]\n" for i in range(31)))
    _artifact(vault, {"backlinks": {"concepts/hub": []}})
    body = _section(srv.retrieve_context("concepts/hub"), "Outbound references")
    assert len(body) == 31 and body[-1] == "- … and 1 more", (
        f"30 listed forward links plus one overflow line expected, got {len(body)}: {body[-2:]}")


def test_retrieve_context_caps_output_at_16000_characters(vault, srv):
    _touch(vault / "wiki" / "concepts" / "big.md", "a" * 13000)
    refs = [{"key": f"notes/r{i:02d}", "title": "T" * 300} for i in range(30)]
    _artifact(vault, {"backlinks": {"concepts/big": refs}})
    out = srv.retrieve_context("concepts/big")
    assert len(out) == 16000, f"retrieve_context output must be cut at 16000 chars, got {len(out)}"


# ── graph_stats ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pages,targets", [(5, None), (None, 2)])
def test_graph_stats_requires_every_count_to_be_an_int(vault, srv, pages, targets):
    _artifact(vault, {"backlinks": {"a": []}, "pages": pages, "targets": targets, "edges": 3})
    assert srv.graph_stats() == srv._graph_unavailable(), (
        f"pages={pages!r} targets={targets!r} is not a usable artifact")


def _stats_doc(**extra) -> dict:
    doc = {"backlinks": {"a": [{"key": "b"}]}, "pages": 4, "targets": 1, "edges": 1}
    doc.update(extra)
    return doc


def test_graph_stats_formats_a_numeric_build_time(vault, srv):
    _artifact(vault, _stats_doc(built_at=0))
    out = srv.graph_stats()
    assert "built 1970-01-01 00:00 UTC" in out, f"numeric built_at must be formatted: {out!r}"


def test_graph_stats_names_the_excluded_namespaces(vault, srv):
    _artifact(vault, _stats_doc(excluded_namespaces=["alpha", "beta"]))
    out = srv.graph_stats()
    assert "(namespaces excluded from the graph: alpha, beta)" in out, (
        f"declared exclusions must be listed: {out!r}")


def test_graph_stats_lists_exactly_fifteen_hubs(vault, srv):
    bl = {f"hub{i:02d}": [{"key": f"r{j}"} for j in range(i + 1)] for i in range(17)}
    _artifact(vault, _stats_doc(backlinks=bl))
    hubs = [line for line in srv.graph_stats().split("\n") if re.match(r"^\s+\d+\s+hub\d\d$", line)]
    assert len(hubs) == 15, f"exactly 15 hubs expected, got {len(hubs)}"


def test_graph_stats_caps_output_at_8000_characters(vault, srv):
    bl = {f"{i:02d}" + "k" * 700: [{"key": "r"}] for i in range(15)}
    _artifact(vault, _stats_doc(backlinks=bl))
    out = srv.graph_stats()
    assert len(out) == 8000, f"graph_stats output must be cut at 8000 chars, got {len(out)}"


# ── list_pages ──────────────────────────────────────────────────────────────────────────────


def _page(vault: Path, rel: str, title: str, **fm) -> None:
    lines = [f"title: {title}"] + [f"{k}: {v}" for k, v in fm.items()]
    _touch(vault / "wiki" / f"{rel}.md", "---\n" + "\n".join(lines) + "\n---\nbody\n")


def _rows(out: str) -> list[str]:
    return [line for line in out.split("\n") if line.startswith("- ")]


@pytest.fixture
def many(vault, srv):
    for i in range(45):
        _page(vault, f"notes/p{i:02d}", f"P{i:02d}", updated=f"2026-01-{i % 28 + 1:02d}")
    return srv


def test_list_pages_defaults_to_forty_rows(many):
    rows = _rows(many.list_pages("notes"))
    assert len(rows) == 40, f"default limit is 40, got {len(rows)}"


def test_list_pages_falls_back_to_forty_rows_for_a_bad_limit(many):
    rows = _rows(many.list_pages("notes", limit="many"))
    assert len(rows) == 40, f"a non-int limit falls back to 40, got {len(rows)}"


def test_list_pages_skips_index_and_underscore_files_only(vault, srv):
    _page(vault, "notes/INDEX", "Index")
    _page(vault, "notes/_draft", "Draft")
    _page(vault, "notes/Alpha", "Alpha")
    _page(vault, "notes/zulu", "Zulu")
    rows = _rows(srv.list_pages("notes"))
    assert sorted(r.split(" — ")[-1] for r in rows) == ["notes/Alpha", "notes/zulu"], rows


def test_list_pages_parses_frontmatter_closing_exactly_at_the_read_window(vault, srv):
    def fm_ending_at(n: int) -> str:
        head = "---\ntitle: T\npad: "
        tail = "\n---"
        return head + "x" * (n - len(head) - len(tail)) + tail + "\nbody\n"

    _touch(vault / "wiki" / "notes" / "inside.md", fm_ending_at(3000))
    _touch(vault / "wiki" / "notes" / "outside.md", fm_ending_at(3001))
    rows = _rows(srv.list_pages("notes"))
    assert [r.split(" — ")[-1] for r in rows] == ["notes/inside"], (
        f"only frontmatter closed within the first 3000 chars is parsed: {rows}")


def test_list_pages_keeps_scanning_after_an_unreadable_entry(vault, srv):
    (vault / "wiki" / "notes" / "aaa.md").mkdir(parents=True)
    _page(vault, "notes/deep/kept", "Kept")
    rows = _rows(srv.list_pages("notes"))
    assert [r.split(" — ")[-1] for r in rows] == ["notes/deep/kept"], rows


def test_list_pages_keeps_scanning_after_an_out_of_scope_row(vault, srv):
    _page(vault, "notes/a-top", "Top")   # sorts before deep/kept: a `break` would drop it
    _page(vault, "notes/deep/kept", "Kept")
    token = srv._caller_var.set({"kind": "extension", "read_scopes": ["notes/deep/**"]})
    try:
        rows = _rows(srv.list_pages("notes"))
    finally:
        srv._caller_var.reset(token)
    assert [r.split(" — ")[-1] for r in rows] == ["notes/deep/kept"], rows


def test_list_pages_keeps_scanning_after_every_skipped_entry(vault, srv):
    # Each skip sorts BEFORE the kept page, so a `continue` turned into `break` drops it.
    notes = vault / "wiki" / "notes"
    _touch(notes / "a1-no-frontmatter.md", "no frontmatter here\n")
    _touch(notes / "a2-bad-yaml.md", "---\ntitle: [unclosed\n---\nbody\n")
    _touch(notes / "a3-not-a-mapping.md", "---\n- just\n- a list\n---\nbody\n")
    _page(vault, "notes/a4-wrong-type", "Wrong type", type="other", status="open")
    _page(vault, "notes/a5-wrong-status", "Wrong status", type="memo", status="closed")
    _page(vault, "notes/z-kept", "Kept", type="memo", status="open")
    rows = _rows(srv.list_pages("notes", type="memo", status="open"))
    assert [r.split(" — ")[-1] for r in rows] == ["notes/z-kept"], (
        f"every skipped entry must be passed over, not end the scan: {rows}")


def test_list_pages_header_names_only_the_active_filters(vault, srv):
    _page(vault, "notes/one", "One", type="memo", status="open")
    assert srv.list_pages("notes").split("\n")[0] == "# notes — 1"
    filtered = srv.list_pages("notes", type="memo", status="open").split("\n")[0]
    assert filtered == "# notes · type=memo · status=open — 1", filtered
