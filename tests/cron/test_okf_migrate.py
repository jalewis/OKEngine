"""okf_migrate (okengine#165): the mover follows the governing schema's partitioning —
flat AND wrongly-nested pages re-nest to the canonical key; duplicate-slug collisions
are held back for the dedup pass; link rewrite covers nested old paths."""
import importlib
import pathlib
import sys

import pytest

yaml = pytest.importorskip("yaml")
REPO = pathlib.Path(__file__).resolve().parents[2]


def _mod():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import okf_migrate
    return importlib.reload(okf_migrate)


def _vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n"
        "types:\n  vendor: {}\n  malware: {}\n")
    w = tmp_path / "wiki" / "entities"
    # flat page -> by-letter
    (w / "flatcorp.md").parent.mkdir(parents=True, exist_ok=True)
    (w / "flatcorp.md").write_text("---\ntype: vendor\n---\n")
    # legacy type-dir page -> re-nest to by-letter
    (w / "vendor" / "a").mkdir(parents=True)
    (w / "vendor" / "a" / "acme.md").write_text("---\ntype: vendor\n---\n")
    # already canonical -> untouched
    (w / "g").mkdir()
    (w / "g" / "goodco.md").write_text("---\ntype: vendor\n---\n")
    # duplicate slug: legacy AND canonical both exist -> collision, held back
    (w / "malware" / "d").mkdir(parents=True)
    (w / "malware" / "d" / "dupbot.md").write_text("---\ntype: malware\n---\nlegacy copy\n")
    (w / "d").mkdir()
    (w / "d" / "dupbot.md").write_text("---\ntype: malware\n---\ncanonical copy\n")
    # a page linking the legacy paths
    (tmp_path / "wiki" / "concepts").mkdir()
    (tmp_path / "wiki" / "concepts" / "note.md").write_text(
        "---\ntype: concept\n---\nsee [[entities/vendor/a/acme|Acme]] and "
        "[[entities/flatcorp]] and [[entities/malware/d/dupbot]]\n")
    return tmp_path


def test_build_map_renests_and_holds_collisions(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    move_map, collisions = m.build_map(root, "entities")
    assert move_map["entities/flatcorp"] == "entities/f/flatcorp"
    assert move_map["entities/vendor/a/acme"] == "entities/a/acme"
    assert "entities/g/goodco" not in move_map              # canonical already
    assert ("entities/malware/d/dupbot", "entities/d/dupbot") in collisions
    assert "entities/malware/d/dupbot" not in move_map      # held back, not clobbered


def test_apply_moves_and_rewrites_nested_links(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    rc = m.main(["--namespace", "entities", "--apply", "--root", str(root)])
    assert rc == 0
    assert (root / "wiki" / "entities" / "a" / "acme.md").is_file()
    assert not (root / "wiki" / "entities" / "vendor" / "a" / "acme.md").exists()
    assert (root / "wiki" / "entities" / "f" / "flatcorp.md").is_file()
    note = (root / "wiki" / "concepts" / "note.md").read_text()
    assert "[[entities/a/acme|Acme]]" in note               # alias preserved
    assert "[[entities/f/flatcorp]]" in note
    assert "[[entities/malware/d/dupbot]]" in note          # collision link untouched
    # both dupbot copies still exist — nothing clobbered
    assert (root / "wiki" / "entities" / "malware" / "d" / "dupbot.md").is_file()
    assert (root / "wiki" / "entities" / "d" / "dupbot.md").read_text().endswith("canonical copy\n")


def test_two_sources_one_destination_both_held(tmp_path):
    m = _mod()
    root = tmp_path
    (root / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\ntypes:\n  vendor: {}\n")
    w = root / "wiki" / "entities"
    (w / "vendor" / "x").mkdir(parents=True)
    (w / "vendor" / "x" / "xcorp.md").write_text("---\ntype: vendor\n---\nA\n")
    (w / "xcorp.md").write_text("---\ntype: vendor\n---\nB\n")
    move_map, collisions = m.build_map(root, "entities")
    assert "entities/vendor/x/xcorp" not in move_map
    assert "entities/xcorp" not in move_map
    assert len([c for c in collisions if c[1] == "entities/x/xcorp"]) == 2


def test_canonical_key_and_find_page_agree_with_the_drain(tmp_path):
    """okengine#54: the public helpers no_agent importers use must place a page EXACTLY where the
    reshelve drain would (canonical_key), and must locate an existing page wherever it sits so the
    importer merges in place instead of re-creating a flat duplicate (find_page)."""
    m = _mod()
    root = tmp_path
    (root / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n"
        "    cves: {strategy: by-date, date_field: date_added, reshard_by: year}\n"
        "types:\n  cve: {}\n")

    # is_partitioned: cves declares a strategy; an undeclared/flat namespace does not
    assert m.is_partitioned(root, "cves") is True
    assert m.is_partitioned(root, "dashboards") is False

    # canonical_key: by-date on date_added -> cves/YYYY/MM/slug (matches _new_key/the drain)
    fm = {"type": "cve", "cve_id": "CVE-2026-45659", "date_added": "2026-07-01"}
    assert m.canonical_key(root, "cves", "CVE-2026-45659", fm) == "cves/2026/07/CVE-2026-45659"
    # no usable date -> falls back to flat, exactly where the drain would leave it
    assert m.canonical_key(root, "cves", "CVE-2000-1", {"type": "cve"}) == "cves/CVE-2000-1"

    w = root / "wiki" / "cves"
    (w / "2026" / "07").mkdir(parents=True)
    (w / "2026" / "07" / "CVE-2026-45659.md").write_text("---\ntype: cve\n---\ncanonical\n")
    # only the sharded copy exists -> found despite the importer historically probing the flat root
    got = m.find_page(root, "cves", "CVE-2026-45659")
    assert got == w / "2026" / "07" / "CVE-2026-45659.md"
    # duplicate (the bug): a stale flat copy alongside the shard -> the DEEPEST (canonical) wins,
    # so repeated importer runs converge on the shard rather than ping-ponging
    (w / "CVE-2026-45659.md").write_text("---\ntype: cve\n---\nstale flat\n")
    assert m.find_page(root, "cves", "CVE-2026-45659") == w / "2026" / "07" / "CVE-2026-45659.md"
    # absent -> None
    assert m.find_page(root, "cves", "CVE-1999-9999") is None


def test_dedup_partition_collisions_collapses_to_canonical(tmp_path):
    """okengine#54 cleanup: same-slug copies at a flat root AND a wrong-shaped shard collapse onto
    the ONE canonical path (chosen by canonical_key — same fn the importer uses), frontmatter is
    union-merged, losers deleted, and [[links]] to a dropped path are rewritten."""
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import dedup_partition_collisions as dd
    import importlib
    dd = importlib.reload(dd)
    root = tmp_path
    (root / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n"
        "    security-incidents: {strategy: by-date, date_field: incident_date, reshard_by: year}\n"
        "types:\n  incident: {}\n")
    w = root / "wiki" / "security-incidents"
    # wrong-shape (year-only, from a partition-unaware writer) carries a curated field ...
    (w / "2014").mkdir(parents=True)
    (w / "2014" / "inc-x.md").write_text(
        "---\ntype: incident\nincident_date: '2014-07-02'\nseverity: high\n---\nshort\n")
    # ... and the canonical shard carries the fuller body
    (w / "2014" / "07").mkdir(parents=True)
    (w / "2014" / "07" / "inc-x.md").write_text(
        "---\ntype: incident\nincident_date: '2014-07-02'\n---\nthe full incident writeup body\n")
    # a page linking the wrong-shape path -> must be rewritten to canonical
    (root / "wiki" / "notes").mkdir()
    (root / "wiki" / "notes" / "n.md").write_text(
        "---\ntype: note\n---\nsee [[security-incidents/2014/inc-x]]\n")

    mm, review, removed = dd.dedup_namespace(root, "security-incidents", apply=True)
    dd._rewrite_links(root, "security-incidents", mm, apply=True)

    canonical = w / "2014" / "07" / "inc-x.md"
    assert canonical.is_file() and not (w / "2014" / "inc-x.md").exists()   # collapsed to shard
    assert removed == 1
    fm, body = dd._read(canonical)
    assert fm.get("severity") == "high"          # curated field merged in from the wrong-shape copy
    assert "full incident writeup" in body       # longest body kept
    note = (root / "wiki" / "notes" / "n.md").read_text()
    assert "[[security-incidents/2014/07/inc-x]]" in note   # link rewritten to canonical


def test_unknown_partition_strategy_raises(tmp_path):  # invariant-audit #25
    """An unrecognized strategy (typo `by_date`, invented `by-year`) silently degraded to flat while
    every 'is partitioned?' matcher treated it as partitioned — forking canonicals. Fail loud."""
    m = _mod()
    with pytest.raises(ValueError):
        m._new_key("sources", "x", {"type": "source", "published": "2026-06-01"},
                   {"strategy": "by_date", "date_field": "published"}, set())
