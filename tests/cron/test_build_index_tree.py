"""build_index_tree — INDEX page-list excludes structural + _-prefixed scaffolding."""
import importlib, pathlib, sys
from datetime import datetime, timezone


def test_index_excludes_underscore_scaffolding(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    assert bit._listable("supply-chain-integrity-drift.md")   # a real finding -> listed
    assert not bit._listable("_about.md")                     # namespace card -> excluded
    assert not bit._listable("INDEX.md")                      # structural -> excluded
    assert not bit._listable("_review-queue.md")              # operational scaffold -> excluded


def test_index_emits_fullpath_wikilinks(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    (tmp_path / "wiki" / "lacuna" / "drift.md").write_text("---\ntype: lacuna\ntitle: Drift\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(tmp_path / "wiki" / "lacuna", "now")
    idx = (tmp_path / "wiki" / "lacuna" / "INDEX.md").read_text()
    assert "[[lacuna/drift|drift]]" in idx        # full-path wikilink (cockpit + reader resolve it)
    assert "(drift.md)" not in idx                # not a relative markdown link


def test_index_shows_a_date_from_the_autostamp(tmp_path, monkeypatch):
    # A page whose slug carries no date (lacuna/entity/concept) still shows WHEN in the INDEX,
    # keyed off the write-path auto-stamp (last_updated/created) — the reader-parity date column.
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    (tmp_path / "wiki" / "lacuna" / "drift.md").write_text(
        "---\ntype: lacuna\ntitle: Drift\nlast_updated: '2026-07-01T01:34:15Z'\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(tmp_path / "wiki" / "lacuna", "now")
    idx = (tmp_path / "wiki" / "lacuna" / "INDEX.md").read_text()
    assert "| Page | Type | Created | Updated | Title |" in idx   # both date columns
    assert "2026-07-01" in idx                           # date rendered (YYYY-MM-DD, not the full ts)
    assert "01:34:15" not in idx                         # trimmed to date, no time noise


def test_index_sorts_newest_created_first(tmp_path, monkeypatch):
    # An INDEX's first job is surfacing NEW pages: rows order by `created` desc (published as
    # the fallback), pages without either sort last.
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    w = tmp_path / "wiki" / "lacuna"
    (w / "old.md").write_text("---\ntype: lacuna\ntitle: Old\ncreated: '2026-06-01T00:00:00Z'\n---\n")
    (w / "new.md").write_text("---\ntype: lacuna\ntitle: New\ncreated: '2026-07-02T00:00:00Z'\n---\n")
    (w / "mid.md").write_text("---\ntype: lacuna\ntitle: Mid\npublished: 2026-06-15\n---\n")
    (w / "undated.md").write_text("---\ntype: lacuna\ntitle: Undated\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(w, "now")
    idx = (w / "INDEX.md").read_text()
    positions = {s: idx.index(f"|{s}]]") for s in ("new", "mid", "old", "undated")}
    assert positions["new"] < positions["mid"] < positions["old"] < positions["undated"]
    assert "| 2026-07-02 |" in idx                       # the Created column carries the stamp


def test_index_folds_about(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    (tmp_path / "wiki" / "lacuna" / "_about.md").write_text(
        "---\ntype: about\n---\n# Lacuna\nStructural-gap discovery here.\n")
    (tmp_path / "wiki" / "lacuna" / "drift.md").write_text("---\ntype: lacuna\ntitle: Drift\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(tmp_path / "wiki" / "lacuna", "now")
    idx = (tmp_path / "wiki" / "lacuna" / "INDEX.md").read_text()
    assert "Structural-gap discovery here." in idx   # _about body folded in
    assert "[[lacuna/drift|drift]]" in idx           # finding still listed


def test_main_writes_complete_structure_and_paginated_indexes(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    ns = wiki / "entities" / "a"
    ns.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("exclude: [operational]\n")
    for name in ("one", "two", "three"):
        (ns / f"{name}.md").write_text(
            f"---\ntype: entity\ntitle: {name.title()}\ncreated: 2026-07-24\n---\n")
    (wiki / "operational").mkdir()
    (wiki / "operational" / "hidden.md").write_text("---\ntype: dashboard\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    monkeypatch.setattr(bit, "MAX_ENTRIES", 2)
    monkeypatch.setattr(
        bit.tz_lib, "deployment_now",
        lambda: datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc),
    )

    assert bit.main() == 0
    assert (ns / "INDEX-p02.md").is_file()
    assert "entities/" in (wiki / "INDEX.md").read_text()
    assert "operational/" not in (wiki / "INDEX.md").read_text()
    for name in ("BUNDLE.md", "HEALTH.md", "AGENTS.md"):
        assert (wiki / name).is_file()
    assert '"wakeAgent": false' in capsys.readouterr().out


def test_main_rejects_missing_wiki(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    assert bit.main() == 1
    assert "wiki not found" in capsys.readouterr().err


def test_frontmatter_and_namespace_discovery_edge_paths(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    empty = wiki / "empty"; empty.mkdir(parents=True)
    populated = wiki / "populated"; populated.mkdir()
    (populated / "page.md").write_text("body")
    (wiki / "plain-file").write_text("not a directory")
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    assert bit._discover_namespaces(wiki) == ["populated"]
    assert bit._fm(populated / "page.md") == {}
    bad = populated / "bad.md"; bad.write_text("---\n[broken\n---\n")
    assert bit._fm(bad) == {}
    unreadable = populated / "unreadable.md"; unreadable.mkdir()
    assert bit._fm(unreadable) == {}


def test_empty_about_and_disappearing_namespace_are_tolerated(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"; ns = wiki / "ns"; ns.mkdir(parents=True)
    (ns / "_about.md").write_text("---\ntype: about\n---\n")
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(ns, "now")
    monkeypatch.setattr(bit, "_discover_namespaces", lambda _wiki: ["missing"])
    monkeypatch.setattr(bit.tz_lib, "deployment_now", lambda: datetime.now(timezone.utc))
    assert bit.main() == 0
    assert "missing/" not in (wiki / "INDEX.md").read_text()
