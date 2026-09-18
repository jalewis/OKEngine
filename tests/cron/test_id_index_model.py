"""IdIndex model coverage — the id/name resolver behind write-path dedup (okengine#462, T1).

`scripts/cron/id_index.py` is a baked write-path lib: `write_server._dedup_on_create`
consults it to decide whether a write MERGES into an existing page or CREATES a new
one. A wrong answer produces a duplicate canonical (the #54 partition-dup class) or,
worse, merges two genuinely different entities.

Covered here (pure, in-memory — no vault walk needed):

  _skip / _frontmatter    what the scan excludes and how it parses malformed pages
  IdIndex._add            live claim, tombstone, collision recording, alias indexing
  IdIndex._add_identity   normalised name/alias buckets for the dedup match (#324)
  resolve / collisions    the read surface
  to_dict / from_dict     the persisted artifact contract, incl. PRE-V2 payloads

Two hardening behaviours are asserted explicitly because both come from real
incidents: a non-list `aliases:` value must not crash the build (#196 / #348), and
ids claimed by several live pages must be RECORDED for review, never auto-merged.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"
sys.path.insert(0, str(CRON))
spec = importlib.util.spec_from_file_location("id_index_model", CRON / "id_index.py")
ii = importlib.util.module_from_spec(spec)
sys.modules["id_index_model"] = ii
spec.loader.exec_module(ii)


# ── _skip: what never enters the index ───────────────────────────────────────

def test_reserved_and_hidden_files_are_skipped():
    for name in ("INDEX.md", "log.md", "AGENTS.md", "HOT.md", "BUNDLE.md", "HEALTH.md",
                 "_draft.md", ".hidden.md", "page.bak.20260101.md", "schema.yaml"):
        assert ii._skip(Path("/v/wiki") / name), name


def test_ordinary_pages_are_not_skipped():
    for name in ("acme.md", "CVE-2026-1.md", "some-index-page.md"):
        assert not ii._skip(Path("/v/wiki/entities") / name), name


# ── _frontmatter: malformed pages must not crash the build ───────────────────

def test_frontmatter_parses_and_degrades_safely():
    assert ii._frontmatter("---\nid: a\ntype: actor\n---\n# A\n") == {"id": "a", "type": "actor"}
    assert ii._frontmatter("no frontmatter at all") == {}
    assert ii._frontmatter("---\n[unclosed\n---\n") == {}, "unparseable YAML -> {}"
    assert ii._frontmatter("---\n- a\n- b\n---\n") == {}, "non-mapping -> {}"


# ── _add: claims, tombstones, collisions, aliases ────────────────────────────

def test_first_live_page_claims_the_id():
    idx = ii.IdIndex()
    assert idx.has_slug_identity_index is True
    idx._add("acme", "entities/a/acme.md", {})
    assert idx.resolve("acme") == "entities/a/acme.md"
    assert idx.collisions() == {}


def test_slug_identity_index_rejects_each_empty_key_component():
    idx = ii.IdIndex()
    assert idx.slug_identity_hits("", "agent-tesla") == []
    assert idx.slug_identity_hits("entities", "---") == []
    idx._add_slug_identity("", "entities/a/a.md", "agent-tesla")
    idx._add_slug_identity("entities", "entities/a/a.md", "---")
    assert idx.slug_identity_to_rels == {}


def test_repeat_claim_from_the_same_path_is_not_a_collision():
    idx = ii.IdIndex()
    idx._add("acme", "entities/a/acme.md", {})
    idx._add("acme", "entities/a/acme.md", {})
    assert idx.collisions() == {}


def test_two_live_pages_claiming_one_id_are_recorded_for_review():
    """Slug ids collide by design, so a collision is surfaced -- never auto-merged."""
    idx = ii.IdIndex()
    idx._add("acme", "entities/a/acme.md", {})
    idx._add("acme", "sources/2026/acme.md", {})
    assert idx.collisions() == {"acme": ["entities/a/acme.md", "sources/2026/acme.md"]}
    assert idx.resolve("acme") == "entities/a/acme.md", "the first claimant still resolves"


def test_tombstoned_pages_are_marked_and_do_not_collide():
    idx = ii.IdIndex()
    idx._add("gone", "entities/g/gone.md", {"status": "tombstoned"})
    assert idx.is_tombstoned("gone")
    assert idx.resolve("gone") == "entities/g/gone.md"

    idx._add("gone", "entities/g/gone-2.md", {"status": "Tombstoned"})
    assert idx.collisions() == {}, "tombstones must not manufacture collisions"


def test_aliases_are_indexed_and_resolve_to_the_page():
    idx = ii.IdIndex()
    idx._add("acme", "entities/a/acme.md", {"aliases": ["acme-corp", "acme inc"]})
    assert idx.resolve("acme-corp") == "entities/a/acme.md"
    assert idx.resolve("acme inc") == "entities/a/acme.md"


def test_scalar_and_malformed_alias_values_do_not_crash_the_build():
    """okengine#196/#348: one page authored with a bare scalar (or an int) must not
    take down the whole index build."""
    idx = ii.IdIndex()
    idx._add("a", "entities/a/a.md", {"aliases": "one, two"})
    assert idx.resolve("one") == "entities/a/a.md" and idx.resolve("two") == "entities/a/a.md"

    idx._add("b", "entities/b/b.md", {"aliases": 7})
    assert idx.resolve("b") == "entities/b/b.md"

    idx._add("c", "entities/c/c.md", {"aliases": [7, True]})
    assert idx.resolve("7") == "entities/c/c.md", "non-str members coerce rather than crash"


def test_an_alias_does_not_override_an_existing_claim():
    idx = ii.IdIndex()
    idx._add("acme", "entities/a/acme.md", {})
    idx._add("other", "entities/o/other.md", {"aliases": ["acme"]})
    assert idx.resolve("acme") == "entities/a/acme.md"


# ── _add_identity: the dedup match buckets (okengine#324) ────────────────────

def test_identity_buckets_index_name_and_aliases_normalised():
    idx = ii.IdIndex()
    idx._add_identity("entities/a/acme.md", {"name": "ACME  Corp", "aliases": ["Acme-Inc"]})
    assert any("entities/a/acme.md" in v for v in idx.name_to_rels.values())
    assert any("entities/a/acme.md" in v for v in idx.alias_to_rels.values())


def test_identity_falls_back_from_name_to_title_to_stem():
    idx = ii.IdIndex()
    idx._add_identity("entities/t/titled.md", {"title": "Titled Thing"})
    idx._add_identity("entities/s/stemmed.md", {})
    assert any("entities/t/titled.md" in v for v in idx.name_to_rels.values())
    assert any("entities/s/stemmed.md" in v for v in idx.name_to_rels.values()), \
        "a page with neither name nor title still indexes by its slug"


def test_identity_buckets_do_not_duplicate_the_same_page():
    idx = ii.IdIndex()
    for _ in range(3):
        idx._add_identity("entities/a/acme.md", {"name": "Acme", "aliases": ["A"]})
    assert all(len(v) == 1 for v in idx.name_to_rels.values())
    assert all(len(v) == 1 for v in idx.alias_to_rels.values())


def test_identity_tolerates_scalar_aliases():
    idx = ii.IdIndex()
    idx._add_identity("entities/a/a.md", {"name": "A", "aliases": "x, y"})
    assert len(idx.alias_to_rels) == 2

    idx._add_identity("entities/b/b.md", {"name": "B", "aliases": 7})
    assert any("entities/b/b.md" in v for v in idx.name_to_rels.values())


# ── artifact round-trip ──────────────────────────────────────────────────────

def test_to_dict_from_dict_round_trip_preserves_the_index():
    idx = ii.IdIndex()
    idx._add("acme", "entities/a/acme.md", {"aliases": ["acme-corp"]})
    idx._add("acme", "sources/2026/acme.md", {})
    idx._add("gone", "entities/g/gone.md", {"status": "tombstoned"})
    idx._add_identity("entities/a/acme.md", {"name": "Acme"})

    payload = idx.to_dict()
    assert payload["norm_version"] == 3

    back = ii.from_dict(payload)
    assert back.resolve("acme") == "entities/a/acme.md"
    assert back.resolve("acme-corp") == "entities/a/acme.md"
    assert back.is_tombstoned("gone")
    assert back.collisions() == idx.collisions()
    assert back.name_to_rels == idx.name_to_rels


def test_pre_v2_artifact_loads_with_empty_identity_buckets():
    """A pre-#324 artifact has no name/alias buckets; write_server falls back to a live
    scan until the refresh cron rewrites v2, so dedup is never blind."""
    back = ii.from_dict({
        "by_id": {"acme": "entities/a/acme.md"},
        "aliases": {"acme-corp": "entities/a/acme.md"},
        "tombstoned": ["gone"],
        "collisions": {},
    })
    assert back.resolve("acme") == "entities/a/acme.md"
    assert back.is_tombstoned("gone")
    assert back.name_to_rels == {} and back.alias_to_rels == {}


def test_from_dict_tolerates_an_empty_payload():
    back = ii.from_dict({})
    assert back.resolve("anything") is None
    assert back.collisions() == {}


# ── load(): the persisted artifact ───────────────────────────────────────────

def test_load_returns_none_for_an_absent_or_unreadable_artifact(tmp_path):
    assert ii.load(tmp_path / "absent.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    assert ii.load(broken) is None


def test_load_reads_a_persisted_index(tmp_path):
    import json
    path = tmp_path / "id-index.json"
    path.write_text(json.dumps({"by_id": {"acme": "entities/a/acme.md"},
                                "aliases": {}, "tombstoned": [], "collisions": {}}),
                    encoding="utf-8")
    idx = ii.load(path)
    assert idx is not None and idx.resolve("acme") == "entities/a/acme.md"


# ── _scan(): the vault walk ──────────────────────────────────────────────────

def page(vault, rel, body="# P\n", **fm):
    import yaml
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump(fm, sort_keys=False) if fm else ""
    p.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
    return p


def test_scan_indexes_sharded_pages_and_skips_id_less_ones(tmp_path):
    """rglob, not glob: sharded pages must be included (corpus_indexer historically
    missed them)."""
    page(tmp_path, "entities/a/acme.md", id="acme", type="actor")
    page(tmp_path, "entities/b/c/deep.md", id="deep", type="actor")
    page(tmp_path, "sources/2026/no-id.md", type="source")

    idx = ii._scan(tmp_path)
    assert idx.resolve("acme") == "entities/a/acme.md"
    assert idx.resolve("deep") == "entities/b/c/deep.md", "sharded page indexed"
    assert idx.resolve("no-id") is None, "a page without an id is not claimed"


def test_scan_returns_an_empty_index_without_a_wiki_dir(tmp_path):
    assert ii._scan(tmp_path).by_id == {}


def test_scan_indexes_entity_identity_even_without_an_id(tmp_path):
    """The create-time dedup must catch duplicates of id-less entity pages too."""
    page(tmp_path, "entities/a/acme.md", type="actor", name="Acme Corp")
    idx = ii._scan(tmp_path)
    assert idx.by_id == {}
    assert any("entities/a/acme.md" in v for v in idx.name_to_rels.values())


def test_scan_excludes_tombstoned_pages_from_identity_buckets(tmp_path):
    page(tmp_path, "entities/a/gone.md", type="actor", name="Gone", status="tombstoned")
    idx = ii._scan(tmp_path)
    assert idx.name_to_rels == {}
    assert idx.slug_identity_to_rels == {}


def test_scan_qualifies_nested_subdomain_namespaces_exactly(tmp_path):
    """Every nested schema container is part of the qualified namespace key (#592)."""
    (tmp_path / "wiki" / "acme").mkdir(parents=True)
    (tmp_path / "wiki" / "acme" / "schema.yaml").write_text("types: {}\n")
    (tmp_path / "wiki" / "acme" / "region").mkdir()
    (tmp_path / "wiki" / "acme" / "region" / "schema.yaml").write_text("types: {}\n")
    (tmp_path / "wiki" / "acme" / "region" / "division").mkdir()
    (tmp_path / "wiki" / "acme" / "region" / "division" / "schema.yaml").write_text(
        "types: {}\n"
    )
    page(
        tmp_path,
        "acme/region/division/entities/agent-tesla.md",
        id="entities:agent-tesla",
        type="actor",
        status="verified",
    )
    idx = ii._scan(tmp_path)
    expected = ["acme/region/division/entities/agent-tesla.md"]
    assert idx.slug_identity_hits("acme/region/division/entities", "agenttesla") == expected
    assert idx.slug_identity_hits("acme/region/entities", "agenttesla") == []
    assert idx.slug_identity_hits("acme/entities", "agenttesla") == []
    assert idx.slug_identity_hits("entities", "agenttesla") == []


def test_scan_indexes_walk_up_subdomain_entities(tmp_path):
    """A page at <sub>/entities/x is namespace 'entities', not '<sub>' -- a root-only
    gate let duplicate canonicals into every co-installed vault (invariant-audit #351)."""
    (tmp_path / "wiki" / "sub").mkdir(parents=True)
    (tmp_path / "wiki" / "sub" / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    page(tmp_path, "sub/entities/a/acme.md", type="actor", name="Acme")

    idx = ii._scan(tmp_path)
    assert any("sub/entities/a/acme.md" in v for v in idx.name_to_rels.values()), \
        "sub-domain entity identity must be indexed for dedup"


def test_scan_skips_reserved_files(tmp_path):
    page(tmp_path, "INDEX.md", id="should-not-index")
    page(tmp_path, "entities/a/real.md", id="real", type="actor")
    idx = ii._scan(tmp_path)
    assert idx.resolve("should-not-index") is None
    assert idx.resolve("real") == "entities/a/real.md"


# ── write_index / build: artifact lifecycle ──────────────────────────────────

def test_write_index_persists_atomically_and_round_trips(tmp_path):
    idx = ii.IdIndex()
    idx._add("acme", "entities/a/acme.md", {})
    target = tmp_path / "state" / "id-index.json"

    ii.write_index(idx, target)
    assert target.is_file(), "parent directories are created"
    assert not target.with_suffix(".json.tmp").exists(), "the temp file is renamed away"
    assert ii.load(target).resolve("acme") == "entities/a/acme.md"


def test_build_force_full_scans_without_touching_the_artifact(tmp_path):
    page(tmp_path, "entities/a/acme.md", id="acme", type="actor")
    idx = ii.build(tmp_path, force=True)
    assert idx.resolve("acme") == "entities/a/acme.md"


def test_build_falls_back_to_a_live_scan_when_no_artifact_exists(tmp_path, monkeypatch):
    """First deploy, before the refresh cron has run: dedup must never be blind."""
    monkeypatch.setattr(ii, "load", lambda *a, **k: None)
    page(tmp_path, "entities/a/acme.md", id="acme", type="actor")
    assert ii.build(tmp_path).resolve("acme") == "entities/a/acme.md"


def test_build_serves_the_persisted_artifact_and_kicks_one_refresh(tmp_path, monkeypatch):
    """The write path must not pay for a 64k-page scan; it loads the artifact and
    refreshes in the background -- and only ONE refresh per vault at a time."""
    persisted = ii.from_dict({"by_id": {"stale": "entities/s/stale.md"}})
    monkeypatch.setattr(ii, "load", lambda *a, **k: persisted)

    started = []
    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(args)
        def start(self):
            pass
    monkeypatch.setattr(ii.threading, "Thread", FakeThread)

    ii._REFRESHING.discard(str(tmp_path))
    idx = ii.build(tmp_path)
    assert idx.resolve("stale") == "entities/s/stale.md", "served instantly from the artifact"
    assert len(started) == 1

    ii.build(tmp_path)
    assert len(started) == 1, "a refresh already in flight is not duplicated"
    ii._REFRESHING.discard(str(tmp_path))


def test_refresh_into_updates_the_holders_index_in_place(tmp_path, monkeypatch):
    """A caller holding the index object must see the fresh data, and an id gained
    in-session while the rebuild ran must not be dropped."""
    page(tmp_path, "entities/a/fresh.md", id="fresh", type="actor")
    idx = ii.IdIndex()
    idx._add("in-session", "entities/i/in-session.md", {})

    # _refresh_into re-persists the artifact; INDEX_PATH is resolved at import from
    # HERMES_DATA and defaults to a container path, so redirect the write rather than
    # letting a unit test touch anything outside tmp_path.
    written = {}
    monkeypatch.setattr(ii, "write_index", lambda fresh: written.setdefault("idx", fresh))

    ii._REFRESHING.add("k")
    ii._refresh_into(idx, tmp_path, "k")
    assert written["idx"].resolve("fresh") == "entities/a/fresh.md", "artifact re-persisted"

    assert idx.resolve("fresh") == "entities/a/fresh.md", "updated in place"
    assert idx.resolve("in-session") == "entities/i/in-session.md", "racing write not dropped"
    assert "k" not in ii._REFRESHING, "the in-flight marker is always cleared"
