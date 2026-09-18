"""Re-identifying sources by URL, and the schema-resolution bug that hid the need for it
(okengine#515 items 3 and 4).

Two fixes, tested together because the second is what made the first necessary:

**Resolution (item 3).** `okf_migrate._governing_schema` walked up to the pack's raw
`schema.yaml`. On a composed pack that file declared partitioning for four namespaces and
omitted `sources`, so `_partition_cfg` fell back to its `flat` default, `_new_key` returned
None, and the mover reported "0 files to move" — truthfully — while source pages accumulated
across five partition shapes with nothing normalizing them. The effective schema
(`.okengine/composed-schema.yaml`, which the write path uses) declares `sources: by-date`.
With the fix the same vault reports 986 moves.

**Re-id (item 4).** Rewrite the `id:` line to the URL-derived form, but only when
unambiguous. The refusals matter more than the rewrites: a page with no url is undecidable,
and two pages sharing a url are a genuine duplicate that must be REPORTED, never re-identified
to a single id — that would manufacture an index collision, which is the bug rather than the
fix. On one live vault this surfaced 16,064 such pairs.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
REID = REPO / "scripts" / "reid_sources_by_url.py"
MIGRATE = REPO / "scripts" / "cron" / "okf_migrate.py"


def _load(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _page(vault: Path, rel: str, *, url=None, pid=None, ptype="source", title="A Title"):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"type: {ptype}", f"title: {title}"]
    if pid:
        lines.append(f"id: {pid}")
    if url:
        lines.append(f"url: {url}")
    lines += ["published: 2026-06-01", "---", "Body.", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# --------------------------------------------------------------- item 4: re-id


def test_rewrites_only_the_id_line(tmp_path):
    m = _load("reid", REID)
    p = _page(tmp_path, "sources/2026/06/a.md", url="https://example.test/a",
              pid="sources:a-title")
    before = p.read_text()
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    after = p.read_text()
    assert m.source_url_id("https://example.test/a") in after
    assert "sources:a-title" not in after
    # everything except the id line is untouched
    assert [ln for ln in before.splitlines() if not ln.startswith("id:")] == \
           [ln for ln in after.splitlines() if not ln.startswith("id:")]


def test_dry_run_writes_nothing(tmp_path):
    m = _load("reid", REID)
    p = _page(tmp_path, "sources/2026/06/a.md", url="https://example.test/a",
              pid="sources:a-title")
    before = p.read_text()
    assert m.main(["--vault", str(tmp_path)]) == 0
    assert p.read_text() == before


def test_a_page_with_no_url_is_left_alone(tmp_path):
    """Identity is undecidable without a url; guessing would fuse records."""
    m = _load("reid", REID)
    p = _page(tmp_path, "sources/2026/06/no-url.md", pid="sources:no-url-title")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert "sources:no-url-title" in p.read_text()
    assert m.plan(tmp_path)["skipped"]["no_url"] == 1


def test_two_pages_sharing_a_url_are_reported_not_reidentified(tmp_path):
    """The important refusal: re-iding both would create two live pages with one id."""
    m = _load("reid", REID)
    url = "https://example.test/same"
    a = _page(tmp_path, "sources/2026/06/story.md", url=url, pid="sources:story")
    b = _page(tmp_path, "sources/2026/06/story-link.md", url=url, pid="sources:story-link")
    result = m.plan(tmp_path)
    assert len(result["dup_pairs"]) == 1, result["dup_pairs"]
    assert len(result["rewrite"]) == 1, "exactly one of the pair may take the id"
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    ids = {a.read_text().count(m.source_url_id(url)), b.read_text().count(m.source_url_id(url))}
    assert ids == {0, 1}, "the url id must end up on exactly one of the two pages"


def test_already_strong_ids_are_idempotent(tmp_path):
    """Re-running must be a no-op, so this is safe to schedule or repeat."""
    m = _load("reid", REID)
    url = "https://example.test/strong"
    p = _page(tmp_path, "sources/2026/06/s.md", url=url, pid=m.source_url_id(url))
    before = p.read_text()
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert p.read_text() == before
    assert m.plan(tmp_path)["skipped"]["already_strong"] == 1


def test_non_source_types_are_untouched(tmp_path):
    m = _load("reid", REID)
    p = _page(tmp_path, "sources/2026/06/dash.md", url="https://example.test/d",
              pid="sources:dash", ptype="dashboard")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert "sources:dash" in p.read_text()


def test_check_references_rewrites_structured_pointer_with_target(tmp_path, capsys):
    """A live frontmatter pointer moves in the same apply as its source identity."""
    m = _load("reid", REID)
    _page(tmp_path, "sources/2026/06/a.md", url="https://example.test/a", pid="sources:a-title")
    ref = tmp_path / "wiki" / "concepts" / "c.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("---\ntype: concept\nid: concepts:c\n"
                   "derived_from: sources:a-title\n---\nBody sources:a-title.\n",
                   encoding="utf-8")
    assert m.main(["--vault", str(tmp_path), "--apply", "--check-references"]) == 0
    output = capsys.readouterr().out
    assert "structured refs  : 1 across 1 page(s)" in output
    text = ref.read_text()
    assert f"derived_from: {m.source_url_id('https://example.test/a')}" in text
    assert "Body sources:a-title." in text, "historical/narrative bodies are not rewritten"


def test_duplicate_legacy_id_with_distinct_urls_is_safe_when_unreferenced(tmp_path):
    """The re-id exists to resolve this collision; ambiguity matters only to live pointers."""
    m = _load("reid_duplicate_old", REID)
    a = _page(tmp_path, "sources/a.md", url="https://example.test/a", pid="sources:old")
    b = _page(tmp_path, "sources/b.md", url="https://example.test/b", pid="sources:old")
    assert m.main(["--vault", str(tmp_path), "--apply", "--check-references"]) == 0
    assert m.source_url_id("https://example.test/a") in a.read_text()
    assert m.source_url_id("https://example.test/b") in b.read_text()


def test_reference_to_ambiguous_legacy_id_refuses(tmp_path, capsys):
    m = _load("reid_ambiguous_ref", REID)
    _page(tmp_path, "sources/a.md", url="https://example.test/a", pid="sources:legacy")
    _page(tmp_path, "sources/b.md", url="https://example.test/b", pid="sources:legacy")
    ref = tmp_path / "wiki" / "concepts" / "c.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("---\ntype: concept\nsource: sources:legacy\n---\n", encoding="utf-8")
    assert m.main(["--vault", str(tmp_path), "--apply", "--check-references"]) == 1
    assert "multiple URL identities" in capsys.readouterr().err
    assert "id: sources:legacy" in (tmp_path / "wiki" / "sources" / "a.md").read_text()


def test_limit_does_not_hide_an_ambiguous_legacy_reference(tmp_path, capsys):
    m = _load("reid_ambiguous_limited", REID)
    first = _page(tmp_path, "sources/a.md", url="https://example.test/a",
                  pid="sources:legacy")
    second = _page(tmp_path, "sources/b.md", url="https://example.test/b",
                   pid="sources:legacy")
    ref = tmp_path / "wiki" / "concepts" / "c.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("---\ntype: concept\nsource: sources:legacy\n---\n", encoding="utf-8")

    assert m.main(["--vault", str(tmp_path), "--apply", "--limit", "1"]) == 1
    assert "multiple URL identities" in capsys.readouterr().err
    assert "id: sources:legacy" in first.read_text()
    assert "id: sources:legacy" in second.read_text()


def test_limit_does_not_move_references_for_an_unselected_unique_identity(tmp_path):
    m = _load("reid_unselected_reference", REID)
    _page(tmp_path, "sources/a.md", url="https://example.test/a", pid="sources:first")
    second = _page(tmp_path, "sources/b.md", url="https://example.test/b", pid="sources:second")
    ref = tmp_path / "wiki" / "concepts" / "c.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("---\ntype: concept\nsource: sources:second\n---\n", encoding="utf-8")
    assert m.main(["--vault", str(tmp_path), "--apply", "--limit", "1"]) == 0
    assert "source: sources:second" in ref.read_text()
    assert "id: sources:second" in second.read_text()


def test_logs_are_not_treated_as_references(tmp_path):
    """`log.md` / `_review-queue.md` are append-only history; rewriting them would falsify it."""
    m = _load("reid", REID)
    _page(tmp_path, "sources/2026/06/a.md", url="https://example.test/a", pid="sources:a-title")
    log = tmp_path / "wiki" / "log.md"
    log.write_text("- collision on create: sources:a-title already used by x\n", encoding="utf-8")
    assert m.structured_reference_rewrites(
        tmp_path, {"sources:a-title": "sources:url-00000000000000000000"}) == []


# ------------------------------------------------- item 3: schema resolution


def test_composed_schema_wins_over_the_packs_own(tmp_path):
    """The bug: partitioning declared only in the composition was invisible, so a namespace
    silently defaulted to flat and the mover went idle."""
    m = _load("okf_migrate", MIGRATE)
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / ".okengine").mkdir()
    # pack's own schema omits `sources` entirely — this is the live shape that bit
    (tmp_path / "schema.yaml").write_text(
        "types: {source: {required: [type]}}\n"
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n")
    (tmp_path / ".okengine" / "composed-schema.yaml").write_text(
        "types: {source: {required: [type]}}\n"
        "partitioning:\n  namespaces:\n"
        "    sources: {strategy: by-date, date_field: published, reshard_by: day}\n")
    m._SCHEMA_CACHE.clear()
    cfg = m._partition_cfg(m._governing_schema(tmp_path, "sources"), "sources")
    assert cfg.get("strategy") == "by-date", (
        "the composed schema is what the write path enforces and must govern placement too")


def test_an_undeclared_namespace_warns_instead_of_going_quietly_flat(tmp_path, capsys):
    m = _load("okf_migrate", MIGRATE)
    m._SCHEMA_CACHE.clear()
    m._UNDECLARED_WARNED.clear()
    schema = {"partitioning": {"namespaces": {"entities": {"strategy": "by-letter"}}}}
    cfg = m._partition_cfg(schema, "sources")
    assert cfg == {"strategy": "flat"}, "still flat — changing placement blindly would be worse"
    err = capsys.readouterr().err
    assert "no `partitioning.namespaces` entry" in err and "sources" in err, (
        "a missing declaration must be visible, not silently indistinguishable from "
        "'already correctly placed'")


def test_a_subdomain_schema_still_wins_for_its_own_subtree(tmp_path):
    m = _load("okf_migrate", MIGRATE)
    (tmp_path / "wiki" / "sub" / "sources").mkdir(parents=True)
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "composed-schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    sources: {strategy: by-date, date_field: published}\n")
    (tmp_path / "wiki" / "sub" / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    sources: {strategy: flat}\n")
    m._SCHEMA_CACHE.clear()
    cfg = m._partition_cfg(m._governing_schema(tmp_path, "sub/sources"), "sub/sources")
    assert cfg.get("strategy") == "flat", "a sub-domain's own schema governs its subtree"


def test_a_tombstoned_duplicate_is_not_reported_as_an_unresolved_pair(tmp_path):
    """The two tools must agree on what is LIVE (okengine#516).

    A retired duplicate keeps its `url`, so without this skip it still maps to the survivor's
    id and reid reports it as an unresolved duplicate pair FOREVER — after the dedup has
    already resolved it. On a real corpus that read as 16,031 outstanding duplicates when the
    true figure was zero.
    """
    m = _load("reid", REID)
    url = "https://example.test/retired"
    _page(tmp_path, "sources/2026/06/keep.md", url=url, pid="sources:keep")
    dead = _page(tmp_path, "sources/2026/06/dead.md", url=url, pid="sources:dead")
    dead.write_text(dead.read_text().replace("published:", "status: tombstoned\npublished:"),
                    encoding="utf-8")
    result = m.plan(tmp_path)
    assert result["dup_pairs"] == [], "a tombstoned page must not count as a live duplicate"
    assert result["skipped"]["tombstoned"] == 1
    assert len(result["rewrite"]) == 1, "the survivor should still be re-identified"


def test_read_and_identity_edge_cases(tmp_path, monkeypatch):
    m = _load("reid_edges", REID)
    assert m.source_url_id("") == ""
    assert m.read_fm(tmp_path / "missing.md") == ({}, "")
    plain = tmp_path / "plain.md"
    plain.write_text("body", encoding="utf-8")
    assert m.read_fm(plain) == ({}, "body")
    bad = tmp_path / "bad.md"
    bad.write_text("---\nx: [\n---\nbody", encoding="utf-8")
    assert m.read_fm(bad)[0] == {}
    scalar = tmp_path / "scalar.md"
    scalar.write_text("---\nscalar\n---\n", encoding="utf-8")
    assert m.read_fm(scalar)[0] == {}
    assert m.plan(tmp_path)["total"] == 0
    raw = tmp_path / "wiki" / "sources" / "raw.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("body", encoding="utf-8")
    assert m.plan(tmp_path)["skipped"]["no_frontmatter"] == 1


def test_rel_outside_vault_and_reference_scan_edges(tmp_path):
    m = _load("reid_refs", REID)
    assert m._rel(tmp_path, Path("/outside/page.md")) == "/outside/page.md"
    assert m.structured_reference_rewrites(tmp_path, {}) == []
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "plain.md").write_text("body", encoding="utf-8")
    own = wiki / "own.md"
    own.write_text("---\nid: sources:self\nnote: sources:self\nother: no-token\n---\n",
                   encoding="utf-8")
    rewrites = m.structured_reference_rewrites(
        tmp_path, {"sources:self": "sources:url-00000000000000000000"})
    assert rewrites == [(own, {"sources:self": "sources:url-00000000000000000000"})]


def test_reference_scan_continues_past_earlier_non_frontmatter_pages(tmp_path):
    m = _load("reid_refs_order", REID)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text("not frontmatter", encoding="utf-8")
    target = wiki / "z.md"
    target.write_text("---\ntype: concept\nsource: sources:oldid\n---\n", encoding="utf-8")
    assert m.structured_reference_rewrites(
        tmp_path, {"sources:oldid": "sources:url-new"}) == [
        (target, {"sources:oldid": "sources:url-new"})]


def test_reference_scan_defensively_skips_inconsistent_parser_result(tmp_path, monkeypatch):
    """A future/custom parser may return metadata without a matching raw YAML envelope."""
    m = _load("reid_refs_inconsistent_parser", REID)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("body only", encoding="utf-8")
    monkeypatch.setattr(m, "read_fm", lambda _path: ({"source": "sources:old"}, "body only"))
    assert m.structured_reference_rewrites(
        tmp_path, {"sources:old": "sources:url-new"}) == []


def test_reference_scan_never_treats_duplicate_identity_lines_as_pointers(tmp_path):
    m = _load("reid_duplicate_identity_lines", REID)
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    malformed = wiki / "duplicate-id.md"
    malformed.write_text(
        "---\nid: sources:first\nid: sources:second\ntype: source\n---\n",
        encoding="utf-8",
    )
    assert m.structured_reference_rewrites(
        tmp_path,
        {"sources:first": "sources:url-first", "sources:second": "sources:url-second"},
    ) == []
    assert m.apply_rewrites(
        [], [(malformed, {"sources:first": "sources:url-first"})]) == 0
    assert m.apply_rewrites(
        [], [(malformed, {"sources:second": "sources:url-second"})]) == 0


def test_low_level_rewriters_preserve_boundaries_and_only_first_identity():
    m = _load("reid_low_level", REID)
    text = ("---\nid: sources:oldid\nnote: sources:oldid\nid: sources:historical\n---\n"
            "body sources:oldid\n")
    assert m._replace_id(text, "sources:new") == (
        "---\nid: sources:new\nnote: sources:oldid\nid: sources:historical\n---\n"
        "body sources:oldid\n")
    assert m._replace_frontmatter_refs(text, {"sources:oldid": "sources:new"}) == (
        "---\nid: sources:oldid\nnote: sources:new\nid: sources:historical\n---\n"
        "body sources:oldid\n")
    assert m._replace_id("body only", "sources:new") is None
    assert m._replace_frontmatter_refs("body only", {}) is None


def test_apply_preflight_refuses_missing_or_malformed_peer_without_partial_write(tmp_path):
    m = _load("reid_apply_edges", REID)
    missing = tmp_path / "missing.md"
    plain = tmp_path / "plain.md"
    plain.write_text("body", encoding="utf-8")
    no_id = tmp_path / "no-id.md"
    no_id.write_text("---\ntype: source\n---\nbody", encoding="utf-8")
    before = no_id.read_text()
    assert m.apply_rewrites([
        (missing, "old", "new"), (plain, "old", "new"),
        (no_id, "", "sources:url-new"),
    ]) == 0
    assert no_id.read_text() == before


def test_apply_can_add_a_missing_id_when_every_input_is_valid(tmp_path):
    m = _load("reid_apply_no_id", REID)
    no_id = tmp_path / "no-id.md"
    no_id.write_text("---\ntype: source\n---\nbody", encoding="utf-8")
    assert m.apply_rewrites([(no_id, "", "sources:url-new")]) == 1
    assert no_id.read_text() == "---\nid: sources:url-new\ntype: source\n---\nbody"


def test_main_can_reidentify_a_source_that_has_no_legacy_id(tmp_path):
    m = _load("reid_main_no_legacy_id", REID)
    page = _page(tmp_path, "sources/no-id.md", url="https://example.test/no-id")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert f"id: {m.source_url_id('https://example.test/no-id')}" in page.read_text()


def test_apply_defensive_rewriter_failures_return_zero(tmp_path, monkeypatch):
    m = _load("reid_apply_rewriter_failures", REID)
    source = tmp_path / "source.md"
    source.write_text("---\nid: sources:old\n---\n", encoding="utf-8")
    monkeypatch.setattr(m, "_replace_id", lambda *_args: None)
    assert m.apply_rewrites([(source, "sources:old", "sources:url-new")]) == 0

    reference = tmp_path / "reference.md"
    reference.write_text("---\nsource: sources:older\n---\n", encoding="utf-8")
    monkeypatch.setattr(m, "_replace_frontmatter_refs", lambda *_args: None)
    assert m.apply_rewrites(
        [], [(reference, {"sources:older": "sources:url-new"})]) == 0


def test_apply_refuses_a_missing_reference_file(tmp_path):
    m = _load("reid_apply_missing_reference", REID)
    missing = tmp_path / "missing.md"
    assert m.apply_rewrites(
        [], [(missing, {"sources:old": "sources:url-new"})]) == 0


def test_apply_refuses_scalar_reference_frontmatter_even_when_token_matches(tmp_path):
    m = _load("reid_apply_scalar_reference", REID)
    reference = tmp_path / "reference.md"
    reference.write_text("---\nsources:older\n---\n", encoding="utf-8")
    before = reference.read_text()
    assert m.apply_rewrites(
        [], [(reference, {"sources:older": "sources:url-new"})]) == 0
    assert reference.read_text() == before


def test_apply_refuses_a_stale_identity_plan_before_writing_any_peer(tmp_path):
    m = _load("reid_apply_stale", REID)
    changed = tmp_path / "changed.md"
    changed.write_text("---\ntype: source\nid: sources:changed\n---\nbody", encoding="utf-8")
    peer = tmp_path / "peer.md"
    peer.write_text("---\ntype: source\nid: sources:peer\n---\nbody", encoding="utf-8")
    before = {changed: changed.read_text(), peer: peer.read_text()}

    assert m.apply_rewrites([
        (changed, "sources:stale", "sources:url-a"),
        (peer, "sources:peer", "sources:url-b"),
    ]) == 0
    assert {path: path.read_text() for path in before} == before


def test_apply_refuses_contradictory_targets_for_one_path(tmp_path):
    m = _load("reid_apply_conflict", REID)
    page = tmp_path / "page.md"
    page.write_text("---\ntype: source\nid: sources:old\n---\nbody", encoding="utf-8")
    before = page.read_text()

    assert m.apply_rewrites([
        (page, "sources:old", "sources:url-a"),
        (page, "sources:old", "sources:url-b"),
    ]) == 0
    assert page.read_text() == before


def test_apply_accepts_an_identical_duplicate_plan_entry(tmp_path):
    m = _load("reid_apply_identical", REID)
    page = tmp_path / "page.md"
    page.write_text("---\ntype: source\nid: sources:old\n---\nbody", encoding="utf-8")
    item = (page, "sources:old", "sources:url-new")
    assert m.apply_rewrites([item, item]) == 1
    assert "id: sources:url-new" in page.read_text()


def test_apply_refuses_descending_conflict_and_lexically_stale_id(tmp_path):
    m = _load("reid_apply_comparisons", REID)
    page = tmp_path / "page.md"
    page.write_text("---\ntype: source\nid: sources:z-current\n---\nbody", encoding="utf-8")
    before = page.read_text()
    assert m.apply_rewrites([
        (page, "sources:z-current", "sources:url-z"),
        (page, "sources:a-other", "sources:url-a"),
    ]) == 0
    assert m.apply_rewrites([
        (page, "sources:a-stale", "sources:url-new"),
    ]) == 0
    assert page.read_text() == before


def test_apply_refuses_descending_conflict_that_would_otherwise_match_snapshot(tmp_path):
    m = _load("reid_apply_descending_conflict", REID)
    page = tmp_path / "page.md"
    page.write_text("---\nid: sources:a-current\n---\nbody", encoding="utf-8")
    before = page.read_text()
    assert m.apply_rewrites([
        (page, "sources:z-stale", "sources:url-z"),
        (page, "sources:a-current", "sources:url-a"),
    ]) == 0
    assert page.read_text() == before


@pytest.mark.parametrize("content", [
    "body only",
    "---\nx: [\n---\nbody",
    "---\nscalar\n---\nbody",
])
def test_apply_refuses_each_invalid_source_frontmatter_shape(tmp_path, content):
    m = _load("reid_apply_invalid_source", REID)
    page = tmp_path / "page.md"
    page.write_text(content, encoding="utf-8")
    before = page.read_text()
    assert m.apply_rewrites([(page, "", "sources:url-new")]) == 0
    assert page.read_text() == before


@pytest.mark.parametrize("content", [
    "body only",
    "---\nx: [\n---\nbody",
    "---\nscalar\n---\nbody",
    "---\ntype: concept\nsource: sources:different\n---\n",
])
def test_apply_refuses_each_invalid_reference_frontmatter_shape(tmp_path, content):
    m = _load("reid_apply_invalid_reference", REID)
    source = tmp_path / "source.md"
    source.write_text("---\nid: sources:old\n---\n", encoding="utf-8")
    reference = tmp_path / "reference.md"
    reference.write_text(content, encoding="utf-8")
    before = {source: source.read_text(), reference: reference.read_text()}
    assert m.apply_rewrites(
        [(source, "sources:old", "sources:url-new")],
        [(reference, {"sources:old": "sources:url-new"})],
    ) == 0
    assert {path: path.read_text() for path in before} == before


def test_apply_returns_exact_unique_file_count_for_ids_and_references(tmp_path):
    m = _load("reid_apply_count", REID)
    first = tmp_path / "first.md"
    first.write_text("---\nid: sources:first\npeer: sources:second\n---\n", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("---\nid: sources:second\n---\n", encoding="utf-8")
    assert m.apply_rewrites(
        [(first, "sources:first", "sources:url-first"),
         (second, "sources:second", "sources:url-second")],
        [(first, {"sources:second": "sources:url-second"})],
    ) == 2
    assert "id: sources:url-first" in first.read_text()
    assert "peer: sources:url-second" in first.read_text()
    assert "id: sources:url-second" in second.read_text()


def test_apply_staging_failure_cleans_temporaries_and_changes_nothing(tmp_path, monkeypatch):
    m = _load("reid_apply_stage_failure", REID)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("---\nid: sources:first\n---\n", encoding="utf-8")
    second.write_text("---\nid: sources:second\n---\n", encoding="utf-8")
    before = {first: first.read_text(), second: second.read_text()}
    real_write = Path.write_text

    def fail_second_stage(path, text, **kwargs):
        if path.name == "second.md.reid-tmp":
            first.with_suffix(".md.reid-tmp").unlink()
            raise OSError("staging failed")
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second_stage)
    assert m.apply_rewrites([
        (first, "sources:first", "sources:url-first"),
        (second, "sources:second", "sources:url-second"),
    ]) == 0
    assert {path: path.read_text() for path in before} == before
    assert not list(tmp_path.glob("*.reid-tmp"))


def test_apply_staging_failure_removes_an_existing_staged_temporary(tmp_path, monkeypatch):
    m = _load("reid_apply_stage_cleanup", REID)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("---\nid: sources:first\n---\n", encoding="utf-8")
    second.write_text("---\nid: sources:second\n---\n", encoding="utf-8")
    real_write = Path.write_text

    def fail_second_stage(path, text, **kwargs):
        if path.name == "second.md.reid-tmp":
            raise OSError("staging failed")
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second_stage)
    assert m.apply_rewrites([
        (first, "sources:first", "sources:url-first"),
        (second, "sources:second", "sources:url-second"),
    ]) == 0
    assert not list(tmp_path.glob("*.reid-tmp"))


def test_apply_replace_failure_rolls_back_every_original(tmp_path, monkeypatch):
    m = _load("reid_apply_replace_failure", REID)
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("---\nid: sources:first\n---\n", encoding="utf-8")
    second.write_text("---\nid: sources:second\n---\n", encoding="utf-8")
    before = {first: first.read_text(), second: second.read_text()}
    real_replace = Path.replace
    failed = False

    def fail_second_replace(path, target):
        nonlocal failed
        if path.name == "second.md.reid-tmp" and not failed:
            failed = True
            raise OSError("publication failed")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)
    assert m.apply_rewrites([
        (first, "sources:first", "sources:url-first"),
        (second, "sources:second", "sources:url-second"),
    ]) == 0
    assert failed
    assert {path: path.read_text() for path in before} == before
    assert not list(tmp_path.glob("*.reid-tmp"))
    assert not list(tmp_path.glob("*.reid-*-tmp"))


def test_cli_missing_vault_limits_and_long_reports(tmp_path, monkeypatch, capsys):
    m = _load("reid_cli_edges", REID)
    assert m.main(["--vault", str(tmp_path)]) == 2
    (tmp_path / "wiki").mkdir()
    pairs = [(f"sources:url-{i:020d}", f"a{i}", f"b{i}") for i in range(11)]
    rewrites = [(tmp_path / f"p{i}.md", f"old{i}", f"new{i}") for i in range(9)]
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0,
               "no_frontmatter": 0, "tombstoned": 0}
    monkeypatch.setattr(m, "plan", lambda _v: {"rewrite": rewrites,
        "dup_pairs": pairs, "skipped": skipped, "total": 20})
    assert m.main(["--vault", str(tmp_path), "--limit", "9"]) == 0
    out = capsys.readouterr().out
    assert "and 1 more" in out


@pytest.mark.parametrize("reported_done", [0, 2])
def test_cli_refuses_every_inexact_apply_count(tmp_path, monkeypatch, capsys, reported_done):
    m = _load(f"reid_cli_bad_count_{reported_done}", REID)
    (tmp_path / "wiki").mkdir()
    page = tmp_path / "wiki" / "sources" / "a.md"
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0,
               "no_frontmatter": 0, "tombstoned": 0}
    monkeypatch.setattr(m, "plan", lambda _v: {
        "rewrite": [(page, "sources:old", "sources:url-new")],
        "dup_pairs": [], "skipped": skipped, "total": 1})
    monkeypatch.setattr(m, "structured_reference_rewrites", lambda *_a: [])
    monkeypatch.setattr(m, "apply_rewrites", lambda *_a: reported_done)
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 1
    assert f"applied {reported_done} of 1 files" in capsys.readouterr().err


def test_cli_accepts_an_exact_apply_count_above_the_integer_cache(
        tmp_path, monkeypatch, capsys):
    m = _load("reid_cli_large_exact_count", REID)
    (tmp_path / "wiki").mkdir()
    rewrites = [
        (tmp_path / "wiki" / f"p{i}.md", f"sources:old-{i}", f"sources:url-{i}")
        for i in range(257)
    ]
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0,
               "no_frontmatter": 0, "tombstoned": 0}
    monkeypatch.setattr(m, "plan", lambda _v: {
        "rewrite": rewrites, "dup_pairs": [], "skipped": skipped, "total": 257})
    monkeypatch.setattr(m, "structured_reference_rewrites", lambda *_a: [])
    monkeypatch.setattr(m, "apply_rewrites", lambda *_a: int("257"))
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert "files changed    : 257" in capsys.readouterr().out


def test_cli_counts_a_page_changed_as_both_identity_and_reference_once(
        tmp_path, monkeypatch, capsys):
    m = _load("reid_cli_unique_count", REID)
    (tmp_path / "wiki").mkdir()
    page = tmp_path / "wiki" / "sources" / "a.md"
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0,
               "no_frontmatter": 0, "tombstoned": 0}
    monkeypatch.setattr(m, "plan", lambda _v: {
        "rewrite": [(page, "sources:old", "sources:url-new")],
        "dup_pairs": [], "skipped": skipped, "total": 1})
    monkeypatch.setattr(m, "structured_reference_rewrites",
                        lambda *_a: [(page, {"sources:old": "sources:url-new"})])
    monkeypatch.setattr(m, "apply_rewrites", lambda *_a: 1)
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert "files changed    : 1 (1 source ids, 1 reference pages)" in capsys.readouterr().out


def test_cli_bounds_ambiguous_reference_evidence_to_ten(tmp_path, monkeypatch, capsys):
    m = _load("reid_cli_ambiguous_bound", REID)
    (tmp_path / "wiki").mkdir()
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0,
               "no_frontmatter": 0, "tombstoned": 0}
    rewrites = [
        (tmp_path / "a.md", "sources:legacy", "sources:url-a"),
        (tmp_path / "b.md", "sources:legacy", "sources:url-b"),
    ]
    monkeypatch.setattr(m, "plan", lambda _v: {
        "rewrite": rewrites, "dup_pairs": [], "skipped": skipped, "total": 2})
    refs = [(tmp_path / "wiki" / f"r{i:02d}.md", {"sources:legacy": "sources:legacy"})
            for i in range(11)]
    monkeypatch.setattr(m, "structured_reference_rewrites", lambda *_a: refs)
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 1
    err = capsys.readouterr().err
    assert "r00.md" in err and "r09.md" in err
    assert "r10.md" not in err


def test_cli_bounds_safe_reference_evidence_to_ten(tmp_path, monkeypatch, capsys):
    m = _load("reid_cli_safe_bound", REID)
    (tmp_path / "wiki").mkdir()
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0,
               "no_frontmatter": 0, "tombstoned": 0}
    rewrite = [(tmp_path / "a.md", "sources:legacy", "sources:url-new")]
    monkeypatch.setattr(m, "plan", lambda _v: {
        "rewrite": rewrite, "dup_pairs": [], "skipped": skipped, "total": 1})
    refs = [(tmp_path / "wiki" / f"r{i:02d}.md", {"sources:legacy": "sources:url-new"})
            for i in range(11)]
    monkeypatch.setattr(m, "structured_reference_rewrites", lambda *_a: refs)
    assert m.main(["--vault", str(tmp_path), "--check-references"]) == 0
    out = capsys.readouterr().out
    assert "structured refs  : 11 across 11 page(s)" in out
    assert "r00.md" in out and "r09.md" in out
    assert "r10.md" not in out


def test_reference_check_with_no_conflicts_continues(tmp_path):
    m = _load("reid_no_conflict", REID)
    _page(tmp_path, "sources/a.md", url="https://example.test/a", pid="sources:old-a")
    assert m.main(["--vault", str(tmp_path), "--check-references"]) == 0
