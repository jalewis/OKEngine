"""okengine#154 layout step — import_lib.rehome_by_type (link-preserving cross-namespace re-home)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import import_lib  # noqa: E402


def _w(p: pathlib.Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _link_total(wiki: pathlib.Path) -> int:
    return sum(len(import_lib._LINK.findall(p.read_text())) for p in wiki.rglob("*.md"))


def test_derive_ns_map_base_entities_and_aliases():
    schema = {"types": {"vendor": {}, "report": {}},
              "type_aliases": {"company": "vendor", "cve": "vulnerability", "daily-brief": "briefing"}}
    m = import_lib.derive_ns_map(schema)
    assert m["source"] == "sources" and m["concept"] == "concepts"   # base/L1 types fixed
    assert m["vendor"] == "entities" and m["report"] == "entities"   # pack types -> entities by default
    assert m["company"] == "entities"                                # alias -> entity type
    assert m["daily-brief"] == "briefings"                           # alias -> base type's namespace


def test_rehome_moves_and_rewrites_links(tmp_path):
    wiki = tmp_path / "wiki"
    # a source mislocated under frontier/, linked BY PATH from an entity
    _w(wiki / "frontier/sources/2026/06/acme-raises.md", "---\ntype: source\n---\n# Acme raises\n")
    _w(wiki / "entities/a/acme.md", "---\ntype: vendor\n---\nFunding: [[frontier/sources/2026/06/acme-raises]].\n")
    # an attack-pattern (entity type) mislocated under concepts/, linked from a concept
    _w(wiki / "concepts/k/keychain-theft.md", "---\ntype: attack-pattern\n---\n# Keychain theft\n")
    _w(wiki / "concepts/o/overview.md", "---\ntype: concept\n---\nSee [[concepts/k/keychain-theft|theft]].\n")

    ns_map = {"source": "sources", "vendor": "entities", "attack-pattern": "entities", "concept": "concepts"}
    before = _link_total(wiki)
    import_lib.rehome_by_type(wiki, ns_map, apply=True)

    assert (wiki / "sources/2026/06/acme-raises.md").exists()           # frontier/sources -> sources (YYYY/MM kept)
    assert not (wiki / "frontier/sources/2026/06/acme-raises.md").exists()
    assert (wiki / "entities/k/keychain-theft.md").exists()             # concepts -> entities
    assert "[[sources/2026/06/acme-raises]]" in (wiki / "entities/a/acme.md").read_text()
    assert "[[entities/k/keychain-theft|theft]]" in (wiki / "concepts/o/overview.md").read_text()  # alias kept
    assert _link_total(wiki) == before                                 # link invariant


def test_rehome_skips_collision_never_overwrites(tmp_path):
    wiki = tmp_path / "wiki"
    _w(wiki / "entities/x/xeon.md", "---\ntype: concept\n---\n# Xeon (mislocated dup)\n")  # concept in entities/
    _w(wiki / "concepts/x/xeon.md", "---\ntype: concept\n---\n# Xeon (canonical)\n")        # target occupied
    changes = import_lib.rehome_by_type(wiki, {"concept": "concepts"}, apply=True)
    assert (wiki / "entities/x/xeon.md").exists() and (wiki / "concepts/x/xeon.md").exists()
    assert any("collision" in c for c in changes)


def test_rehome_dry_run_is_noop(tmp_path):
    wiki = tmp_path / "wiki"
    _w(wiki / "frontier/sources/2026/06/s.md", "---\ntype: source\n---\n# S\n")
    out = import_lib.rehome_by_type(wiki, {"source": "sources"}, apply=False)
    assert (wiki / "frontier/sources/2026/06/s.md").exists()            # untouched
    assert any("dry-run" in c for c in out)


def test_collapse_source_dates(tmp_path):
    wiki = tmp_path / "wiki"
    _w(wiki / "sources/2026/06/26/2026-06-26-acme-breach.md", "---\ntype: source\n---\n# Acme breach\n")
    _w(wiki / "entities/a/acme.md", "---\ntype: vendor\n---\nSee [[sources/2026/06/26/2026-06-26-acme-breach]].\n")
    before = _link_total(wiki)
    import_lib.collapse_source_dates(wiki, apply=True)
    assert (wiki / "sources/2026/06/2026-06-26-acme-breach.md").exists()          # day-dir collapsed
    assert not (wiki / "sources/2026/06/26/2026-06-26-acme-breach.md").exists()
    assert "[[sources/2026/06/2026-06-26-acme-breach]]" in (wiki / "entities/a/acme.md").read_text()  # link rewritten
    assert _link_total(wiki) == before                                           # invariant


def test_collapse_source_dates_disambiguates_same_slug(tmp_path):
    wiki = tmp_path / "wiki"
    _w(wiki / "sources/2026/06/26/daily.md", "---\ntype: source\n---\n# 26\n")
    _w(wiki / "sources/2026/06/27/daily.md", "---\ntype: source\n---\n# 27\n")
    import_lib.collapse_source_dates(wiki, apply=True)
    survivors = {p.name for p in (wiki / "sources/2026/06").glob("*.md")}
    assert "daily.md" in survivors and ("daily-26.md" in survivors or "daily-27.md" in survivors)  # both kept


def test_collapse_source_dates_dry_run_is_noop(tmp_path):
    wiki = tmp_path / "wiki"
    _w(wiki / "sources/2026/06/26/x.md", "---\ntype: source\n---\n# X\n")
    out = import_lib.collapse_source_dates(wiki, apply=False)
    assert (wiki / "sources/2026/06/26/x.md").exists()                           # untouched
    assert any("dry-run" in c for c in out)
