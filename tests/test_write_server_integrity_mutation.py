"""Observable contracts of the write path's integrity guards (okengine#612).

`src/okengine/write_services/integrity.py` holds the reject/flag guards every writer crosses:
alias shape and review-rejected names, recorded alias removals, fabricated and missing source
citations, tombstone successors, identity contradictions, wikilink review, briefing citations,
degenerate generations, future record dates, and the atomic publish primitive.

Each test loads the enforced write path (`okengine-mcp/write_server.py`) against a real temp
vault and calls the bound guard the transactions call, asserting the exact verdict a caller
sees: the rejection text (including which values it lists and where it truncates), the flags
queued for review, or the bytes and mode on disk. Nothing is mocked.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"
TODAY = "2026-06-15"

_SCHEMA = """\
okf:
  required: [type]
types:
  entity: {required: [type, name]}
  concept: {}
  topic: {}
  tool: {}
  actor: {}
  threat_actor: {}
type_aliases: {threat_actor: actor}
strict_types: false
partitioning:
  namespaces:
    entities: {strategy: by-letter}
    concepts: {strategy: flat}
    topics: {strategy: flat}
    briefings: {strategy: flat}
    sources: {strategy: flat}
"""


def _load(root: Path, monkeypatch):
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(root))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", TODAY)
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    name = f"write_server_integrity_mutation_{id(root)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vault(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    # The module-level WIKI the briefing walk reads is bound at import; keep it on this vault.
    monkeypatch.setattr(m, "WIKI", tmp_path / "wiki")
    return m, tmp_path / "wiki"


def _page(wiki: Path, rel: str, fm: dict | None = None, body: str = "body\n") -> Path:
    page = wiki / f"{rel}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\n" + yaml.safe_dump(fm or {"type": "concept"}, sort_keys=False)
                    + "---\n" + body, encoding="utf-8")
    return page


# --- _alias_shape_reject ------------------------------------------------------------------

_REJECTED_TAIL = (" was removed from this page's aliases by review and is listed in "
                  "`rejected_aliases` — to re-admit a name, remove it from `rejected_aliases` "
                  "in the same update.")
_BAD_TAIL = (" — put references in `sources`, descriptions in the body, and each alias in its "
             "own list entry.")
_LONG_NAME = "Long Name " + "x" * 60  # 70 characters, still a single name (limit is 80)


def test_review_rejected_aliases_are_listed_truncated_with_an_ellipsis_past_five(vault):
    m, wiki = vault
    names = [_LONG_NAME, "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    fm = {"type": "entity", "name": "Acme", "aliases": names, "rejected_aliases": names}
    result = m._alias_shape_reject(wiki / "entities/a/acme.md", fm, {"aliases": []})
    assert result == (
        "rejected: " + ", ".join(repr(n[:60]) for n in names[:5]) + " …" + _REJECTED_TAIL
    )


def test_exactly_five_review_rejected_aliases_carry_no_ellipsis(vault):
    m, wiki = vault
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    fm = {"type": "entity", "aliases": names, "rejected_aliases": names}
    result = m._alias_shape_reject(wiki / "entities/a/acme.md", fm, None)
    assert result == "rejected: " + ", ".join(repr(n) for n in names) + _REJECTED_TAIL


def test_one_review_rejected_alias_carries_no_ellipsis(vault):
    m, wiki = vault
    fm = {"type": "entity", "aliases": ["Alpha"], "rejected_aliases": ["alpha"]}
    assert m._alias_shape_reject(wiki / "entities/a/acme.md", fm, None) == (
        "rejected: 'Alpha'" + _REJECTED_TAIL)


def test_malformed_aliases_are_listed_with_reasons_truncated_past_five(vault):
    m, wiki = vault
    url = "https://example.org/" + "p" * 60
    bad = [url, "a, b, c", "line\nbreak", "https://e.org/2", "https://e.org/3",
           "https://e.org/4"]
    fm = {"type": "entity", "aliases": bad}
    result = m._alias_shape_reject(wiki / "entities/a/acme.md", fm, {})
    why = ["a URL", "several names in one entry (send each alias separately)",
           "multi-line text", "a URL", "a URL"]
    expected = "; ".join(f"{v[:60]!r} is {w}" for v, w in zip(bad[:5], why))
    assert result == "rejected: an alias must be a single name: " + expected + " …" + _BAD_TAIL


def test_exactly_five_and_one_malformed_aliases_carry_no_ellipsis(vault):
    m, wiki = vault
    five = ["https://e.org/%d" % i for i in range(5)]
    result = m._alias_shape_reject(wiki / "entities/a/acme.md", {"aliases": five}, None)
    assert result == ("rejected: an alias must be a single name: "
                      + "; ".join(f"{v!r} is a URL" for v in five) + _BAD_TAIL)
    one = m._alias_shape_reject(wiki / "entities/a/acme.md", {"aliases": "www.e.org"}, None)
    assert one == "rejected: an alias must be a single name: 'www.e.org' is a URL" + _BAD_TAIL


def test_a_scalar_legacy_alias_on_the_page_is_grandfathered(vault):
    m, wiki = vault
    legacy = "https://legacy.example/actor"
    fm = {"type": "entity", "aliases": [legacy, "Acme"]}
    assert m._alias_shape_reject(wiki / "entities/a/acme.md", fm, {"aliases": legacy}) is None


# --- _record_alias_removals ---------------------------------------------------------------


def test_dropped_aliases_are_appended_after_the_existing_rejected_list(vault):
    m, _ = vault
    fm = {"aliases": ["Alpha"], "rejected_aliases": ["Zulu"]}
    added = m._record_alias_removals(fm, {"aliases": ["Alpha", "Bravo", "Charlie"]})
    assert added == ["Bravo", "Charlie"]
    assert fm["rejected_aliases"] == ["Zulu", "Bravo", "Charlie"]


def test_a_scalar_rejected_list_is_kept_as_one_name(vault):
    m, _ = vault
    fm = {"aliases": [], "rejected_aliases": "Zulu Group"}
    assert m._record_alias_removals(fm, {"aliases": ["Bravo"]}) == ["Bravo"]
    assert fm["rejected_aliases"] == ["Zulu Group", "Bravo"]


def test_an_empty_rejected_list_starts_fresh(vault):
    m, _ = vault
    fm = {"aliases": [], "rejected_aliases": None}
    assert m._record_alias_removals(fm, {"aliases": ["Bravo"]}) == ["Bravo"]
    assert fm["rejected_aliases"] == ["Bravo"]


def test_nothing_dropped_leaves_the_page_untouched(vault):
    m, _ = vault
    fm = {"aliases": ["Alpha"]}
    assert m._record_alias_removals(fm, {"aliases": ["alpha"]}) == []
    assert "rejected_aliases" not in fm
    fm2 = {"aliases": ["Alpha"]}
    assert m._record_alias_removals(fm2, None) == []
    assert "rejected_aliases" not in fm2


def test_a_drop_already_listed_is_not_repeated(vault):
    m, _ = vault
    fm = {"aliases": [], "rejected_aliases": ["bravo"]}
    assert m._record_alias_removals(fm, {"aliases": ["Bravo"]}) == []
    assert fm["rejected_aliases"] == ["bravo"]


# --- _unresolvable_link_flags / _wikilink_resolves ----------------------------------------


def test_unresolvable_links_are_counted_once_and_truncated_past_five(vault):
    m, wiki = vault
    body = ("[[concepts/zz1.md]] [[concepts/zz1]] [[/]] [[bare]] [[concepts/zz2]] "
            "[[concepts/zz3]] [[concepts/zz4]] [[concepts/zz5]]")
    flags = m._unresolvable_link_flags(wiki / "concepts/page.md", body)
    assert flags == [
        "6 unresolvable wikilink(s): [[concepts/zz1]] (no such page); [[bare]] (bare name); "
        "[[concepts/zz2]] (no such page); [[concepts/zz3]] (no such page); "
        "[[concepts/zz4]] (no such page) …"
    ]


def test_five_and_one_unresolvable_links_carry_no_ellipsis(vault):
    m, wiki = vault
    five = " ".join(f"[[b{i}]]" for i in range(5))
    assert m._unresolvable_link_flags(wiki / "entities/a/acme.md", five) == [
        "5 unresolvable wikilink(s): " + "; ".join(f"[[b{i}]] (bare name)" for i in range(5))]
    assert m._unresolvable_link_flags(wiki / "concepts/x.md", "[[b0]]") == [
        "1 unresolvable wikilink(s): [[b0]] (bare name)"]


def test_links_resolve_through_first_and_second_letter_shards(vault):
    m, wiki = vault
    _page(wiki, "entities/q/q-lin")
    _page(wiki, "entities/q/i/qi")
    _page(wiki, "entities/_/_x")
    body = "[[entities/q-lin]] [[entities/old/q-lin]] [[entities/qi]] [[entities/_x]] [[x/q]]"
    assert m._unresolvable_link_flags(wiki / "concepts/c.md", body) == [
        "2 unresolvable wikilink(s): [[entities/_x]] (no such page); [[x/q]] (no such page)"]


def test_a_one_letter_base_does_not_probe_a_second_letter_shard(vault):
    m, _ = vault
    assert m._wikilink_resolves("entities/q") is False


def test_shard_lookup_keys_on_the_last_segment_only(vault):
    m, wiki = vault
    _page(wiki, "entities/q/q-lin")
    # An intermediate segment that cannot start a shard does not block the base's own shard.
    assert m._wikilink_resolves("entities/_old/q-lin") is True


def test_a_bare_name_never_resolves_through_a_shard(vault):
    m, wiki = vault
    _page(wiki, "q/q/q")
    assert m._wikilink_resolves("q") is False


def test_links_from_other_namespaces_are_not_reviewed(vault):
    m, wiki = vault
    assert m._unresolvable_link_flags(wiki / "sources/s.md", "[[bare]]") == []
    assert m._unresolvable_link_flags(wiki / "topics/t.md", "[[bare]]") == []


# --- _fabricated_source_reject ------------------------------------------------------------

_FAB_HEAD = ("rejected: sources use the invalid singular `source/` namespace (the schema's "
             "source namespace is plural `sources/`): ")
_FAB_TAIL = (" — this is the entity-backfill fabrication signature; cite an EXISTING "
             "`sources/<…>` page or a provenance label (e.g. 'MITRE ATT&CK').")


def test_singular_source_refs_are_rejected_and_truncated_past_five(vault):
    m, wiki = vault
    refs = ["source/ab.md", "source/a.md", "source/ab", "source/c1", "source/c2", "source/c3",
            "source/c4"]
    result = m._fabricated_source_reject(wiki / "entities/a/acme.md", {"sources": refs})
    flagged = ["source/ab.md", "source/ab", "source/c1", "source/c2", "source/c3"]
    assert result == _FAB_HEAD + "; ".join(flagged) + " …" + _FAB_TAIL


def test_five_and_one_singular_source_refs_carry_no_ellipsis(vault):
    m, wiki = vault
    five = [f"source/c{i}" for i in range(5)]
    assert m._fabricated_source_reject(wiki / "entities/a/acme.md", {"sources": five}) == (
        _FAB_HEAD + "; ".join(five) + _FAB_TAIL)
    assert m._fabricated_source_reject(wiki / "entities/a/acme.md", {"source": "source/zz"}) == (
        _FAB_HEAD + "source/zz" + _FAB_TAIL)


def test_singular_source_refs_outside_entities_are_not_judged(vault):
    m, wiki = vault
    for rel in ("concepts/c.md", "topics/t.md"):
        assert m._fabricated_source_reject(wiki / rel, {"sources": ["source/ab"]}) is None


# --- _missing_source_reject ---------------------------------------------------------------

_MISS_HEAD = "rejected: entity `sources:` must cite existing canonical source pages; unresolved: "
_MISS_TAIL = (" — read and use the exact canonical source path; never invent or "
              "forward-reference evidence.")


def test_missing_source_pages_are_listed_and_truncated_past_five(vault):
    m, wiki = vault
    _page(wiki, "sources/real", {"type": "source"})
    refs = ["[[sources/real]]"] + [f"sources/m{i}" for i in range(6)]
    result = m._missing_source_reject(wiki / "entities/a/acme.md", {"sources": refs})
    assert result == _MISS_HEAD + "; ".join(f"sources/m{i}" for i in range(5)) + " …" + _MISS_TAIL


def test_five_and_one_missing_sources_carry_no_ellipsis(vault):
    m, wiki = vault
    five = [f"sources/m{i}" for i in range(5)]
    assert m._missing_source_reject(wiki / "entities/a/acme.md", {"sources": five}) == (
        _MISS_HEAD + "; ".join(five) + _MISS_TAIL)
    assert m._missing_source_reject(wiki / "entities/a/acme.md", {"sources": ["sources/x"]}) == (
        _MISS_HEAD + "sources/x" + _MISS_TAIL)


def test_missing_sources_outside_entities_are_not_judged(vault):
    m, wiki = vault
    for rel in ("concepts/c.md", "topics/t.md"):
        assert m._missing_source_reject(wiki / rel, {"sources": ["sources/none"]}) is None


# --- _tombstone_successor_reject ----------------------------------------------------------

_DISTINCT = "different actors"


def test_a_distinct_reason_is_refused_only_for_entities(vault):
    m, wiki = vault
    entity = wiki / "entities/a/acme.md"
    assert m._tombstone_successor_reject(entity, {"type": "actor"}, _DISTINCT, None) == (
        "rejected: this reason says the subjects are DIFFERENT, and a tombstone retires a page "
        "that is the SAME identity as another — remove the conflating alias with update_entity "
        "and leave both pages live")
    for rel in ("concepts/c.md", "topics/t.md"):
        assert m._tombstone_successor_reject(wiki / rel, {}, _DISTINCT, None) is None


def test_a_duplicate_without_a_successor_is_refused(vault):
    m, wiki = vault
    assert m._tombstone_successor_reject(
        wiki / "entities/a/acme.md", {}, "duplicate of another page", "  ") == (
        "rejected: a duplicate is retired ONTO its canonical page — pass superseded_by with the "
        "live page it duplicates")
    assert m._tombstone_successor_reject(wiki / "entities/a/acme.md", {}, "stale", None) is None


def test_a_flat_or_misdirected_successor_resolves_to_its_partition_seat(vault):
    m, wiki = vault
    page = _page(wiki, "entities/a/acme", {"type": "actor", "name": "Acme"})
    _page(wiki, "entities/b/beta", {"type": "actor", "name": "Beta"})
    for succ in ("entities/beta", "[[entities/zz/beta]]", "wiki/entities/beta.md"):
        assert m._tombstone_successor_reject(page, {"type": "actor"}, "duplicate", succ) is None


def test_a_successor_that_resolves_nowhere_is_refused(vault):
    m, wiki = vault
    page = _page(wiki, "entities/a/acme", {"type": "actor"})
    for succ in ("nowhere", "entities/ghost"):
        assert m._tombstone_successor_reject(page, {}, "duplicate", succ) == (
            f"rejected: superseded_by {succ!r} does not resolve to an existing page")


def test_a_page_cannot_supersede_itself(vault):
    m, wiki = vault
    page = _page(wiki, "entities/a/acme", {"type": "actor"})
    assert m._tombstone_successor_reject(page, {"type": "actor"}, "duplicate",
                                         "entities/a/acme") == (
        "rejected: a page cannot supersede itself")


def test_a_tombstoned_successor_is_refused_and_a_live_one_accepted(vault):
    m, wiki = vault
    page = _page(wiki, "entities/a/acme", {"type": "actor"})
    _page(wiki, "entities/d/dead", {"type": "actor", "status": " Tombstoned "})
    _page(wiki, "entities/l/live", {"type": "actor", "status": "verified"})
    assert m._tombstone_successor_reject(page, {"type": "actor"}, "duplicate",
                                         "entities/dead") == (
        "rejected: superseded_by entities/d/dead.md is itself tombstoned — point at the live "
        "canonical page")
    assert m._tombstone_successor_reject(page, {"type": "actor"}, "duplicate",
                                         "entities/live") is None


def test_a_successor_of_another_type_is_refused_in_either_order(vault):
    m, wiki = vault
    page = _page(wiki, "entities/a/acme", {"type": "actor"})
    _page(wiki, "entities/t/toolpage", {"type": "tool"})
    _page(wiki, "entities/a/apage", {"type": "actor"})
    assert m._tombstone_successor_reject(page, {"type": "actor"}, "duplicate",
                                         "entities/toolpage") == (
        "rejected: superseded_by entities/t/toolpage.md is type 'tool', not 'actor' — a page is "
        "only ever superseded by the same kind of thing")
    assert m._tombstone_successor_reject(page, {"type": "tool"}, "duplicate",
                                         "entities/apage") == (
        "rejected: superseded_by entities/a/apage.md is type 'actor', not 'tool' — a page is "
        "only ever superseded by the same kind of thing")
    # A type alias names the same kind of thing.
    assert m._tombstone_successor_reject(page, {"type": "threat_actor"}, "duplicate",
                                         "entities/apage") is None


# --- _identity_contradiction_flags --------------------------------------------------------


def test_identity_contradictions_list_only_same_prefix_mismatches(vault):
    m, wiki = vault
    fm = {"name": "MMM 5678", "title": "AAA 1111",
          "aliases": ["ZZZ 2222", "MMM 1234", "mmm-0999", "MMM_9999", "MMM 3000", "MMM 4000"]}
    flags = m._identity_contradiction_flags(wiki / "entities/m/mmm-1234.md", fm)
    assert flags == [
        "entity identity contradiction between path and declared name/aliases: "
        "MMM-1234 vs MMM-0999, MMM-1234 vs MMM-3000, MMM-1234 vs MMM-4000, MMM-1234 vs MMM-5678"
    ]


def test_a_scalar_alias_is_read_as_one_value(vault):
    m, wiki = vault
    flags = m._identity_contradiction_flags(wiki / "entities/a/apt-1234.md",
                                            {"name": "Acme", "aliases": "APT 5555"})
    assert flags == ["entity identity contradiction between path and declared name/aliases: "
                     "APT-1234 vs APT-5555"]


def test_identity_contradictions_are_judged_only_for_entities(vault):
    m, wiki = vault
    fm = {"name": "APT 5555"}
    for rel in ("concepts/apt-1234.md", "topics/apt-1234.md"):
        assert m._identity_contradiction_flags(wiki / rel, fm) == []


# --- _degeneration_flags ------------------------------------------------------------------


def test_a_run_of_exactly_the_limit_is_not_degenerate(vault):
    m, _ = vault
    assert m._degeneration_flags(" ".join(["word"] * 250)) == []
    assert m._degeneration_flags(" ".join(["word"] * 251)) == [
        "degenerate: 251-word unpunctuated run (repetition loop)"]


# --- _briefing_link_reject ----------------------------------------------------------------

_CITE = ("every development must end with a resolvable [[sources/<path>]] link (a footnote or "
         "a code-span path is not a citation an analyst can verify)")


def test_a_briefing_citing_entities_without_a_source_is_refused_with_the_count(vault):
    m, wiki = vault
    _page(wiki, "topics/alpha")
    _page(wiki, "concepts/beta")
    _page(wiki, "dashboards/home")
    one = m._briefing_link_reject(wiki / "briefings/b.md", "[[topics/alpha]] [[dashboards/home]]")
    assert one == "briefing cites 1 entity but no source — " + _CITE
    two = m._briefing_link_reject(wiki / "briefings/b.md", "[[topics/alpha]] [[concepts/beta]]")
    assert two == "briefing cites 2 entities but no source — " + _CITE


def test_a_briefing_with_a_source_link_passes(vault):
    m, wiki = vault
    _page(wiki, "topics/alpha")
    _page(wiki, "sources/ab", {"type": "source"})
    assert m._briefing_link_reject(wiki / "briefings/b.md",
                                   "[[topics/alpha]] [[sources/ab.md]]") is None


def test_briefing_broken_links_are_all_reported_with_hints(vault):
    m, wiki = vault
    _page(wiki, "topics/alpha")
    for name in ("gammaa", "gammabb", "gammaccc"):  # distinct similarity ranks
        _page(wiki, f"concepts/{name}")
    result = m._briefing_link_reject(wiki / "briefings/b.md", "[[wrong/alpha]] [[gamma]]")
    assert result == (
        "briefing links must resolve to existing pages (cite what you actually read; do not "
        "guess slugs): [[wrong/alpha]] — did you mean [[topics/alpha]]?; [[gamma]] (no such "
        "page) — did you mean [[concepts/gammaa]] or [[concepts/gammabb]]?")


# --- _converge_alias_rule / _converge_aliases ---------------------------------------------


def test_converge_appends_new_alias_spellings_only_once(vault):
    m, _ = vault
    assert m._converge_aliases("Alpha", ["alpha", "Bravo", "bravo", "", "Charlie"]) == [
        "Alpha", "Bravo", "Charlie"]
    assert m._converge_aliases(None, "Bravo") == ["Bravo"]


def test_converge_alias_removal_is_recorded_and_update_is_additive(vault):
    m, _ = vault
    cur = {"aliases": ["Alpha", "Bravo"]}
    merged, dec = m.converge.merge_frontmatter(cur, {}, remove=["aliases"])
    assert "aliases" in dec.removed
    m._converge_alias_rule(merged, cur, dec)
    assert merged.get("rejected_aliases") == ["Alpha", "Bravo"]

    merged, dec = m.converge.merge_frontmatter(cur, {"aliases": ["Charlie"]})
    assert "aliases" in dec.updated
    m._converge_alias_rule(merged, cur, dec)
    assert merged["aliases"] == ["Alpha", "Bravo", "Charlie"]
    assert "rejected_aliases" not in merged


# --- _future_date_reject ------------------------------------------------------------------


def test_tomorrow_is_admitted_and_the_day_after_is_refused(vault):
    m, _ = vault
    assert m._future_date_reject({"updated": "2026-06-16"}) is None
    assert m._future_date_reject({"published": "2026-06-17T00:00:00Z"}) == (
        "published: 2026-06-17 is in the future (today is 2026-06-15) — record-keeping dates "
        "must be the ACTUAL write date, never a guessed or future one; use today's date")


# --- _atomic_write_text -------------------------------------------------------------------


def test_atomic_write_keeps_an_existing_mode_and_creates_new_files_0644(vault, tmp_path):
    m, _ = vault
    existing = tmp_path / "keep.md"
    existing.write_text("old", encoding="utf-8")
    os.chmod(existing, 0o641)
    m._atomic_write_text(existing, "new")
    assert existing.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o641

    fresh = tmp_path / "deep" / "er" / "fresh.md"
    m._atomic_write_text(fresh, "made")
    assert fresh.read_text(encoding="utf-8") == "made"
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o644
    assert [p.name for p in fresh.parent.iterdir()] == ["fresh.md"], "temp file left behind"


# --- _identity_field_conflicts / _identity_body_kept (okengine#901) -------------------------


def test_identity_conflicts_keep_engine_and_declared_fields_in_conflict_order(vault):
    m, _ = vault
    schema = {"types": {"report": {"identity_fields": ["report_no", "", "  ", 7]}}}
    conflicts = [("summary", "a", "b"), ("report_no", 1, 2), ("url", "u1", "u2"),
                 ("7", "x", "y"), ("title", "t1", "t2")]
    assert m._identity_field_conflicts(schema, "report", conflicts) == [
        "report_no", "url", "title"]


def test_identity_conflicts_without_a_type_declaration_use_the_engine_set(vault):
    m, _ = vault
    conflicts = [("name", "a", "b"), ("subject", "s", "t"), ("report_no", 1, 2)]
    assert m._identity_field_conflicts({}, "report", conflicts) == ["name", "subject"]
    schema = {"types": {"report": {"identity_fields": "report_no"}, "other": None}}
    assert m._identity_field_conflicts(schema, "report", conflicts) == ["name", "subject"]
    assert m._identity_field_conflicts(schema, "other", conflicts) == ["name", "subject"]


def test_identity_body_kept_queues_the_reason_and_returns_the_result_note(vault):
    m, _ = vault
    flags = ["earlier"]
    note = m._identity_body_kept(["title", "url"], flags)
    assert note == "; body kept (identity conflict on `title`, `url`)"
    assert flags == ["earlier", "identity field conflict (`title`, `url`): the write may describe "
                     "a different document — incoming body NOT applied, existing body kept"]
