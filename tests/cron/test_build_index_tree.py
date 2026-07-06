"""build_index_tree — INDEX page-list excludes structural + _-prefixed scaffolding."""
import importlib, pathlib, sys


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
