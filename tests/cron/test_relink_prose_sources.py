"""relink_prose_sources (okengine#158 P2): prose source entries -> page-refs ONLY on a unique,
confident slug-token match; vague/ambiguous prose is left flagged (no fabrication)."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


_load("schema_lib", "scripts/cron/schema_lib.py")
R = _load("relink_prose_sources", "scripts/cron/relink_prose_sources.py")

IDX = {
    "sources/2026/06/eset-gamaredon-russia-aligned-threat-actor": {"eset", "gamaredon", "russia", "aligned", "threat", "actor"},
    "sources/2026/06/fortinet-mirai-nexcorium-botnet": {"fortinet", "mirai", "nexcorium", "botnet"},
    "sources/2026/06/eset-apt-activity-report": {"eset", "apt", "activity", "report"},
}


def test_unique_match_relinks():
    assert R._match("Gamaredon ESET writeup", IDX) == "sources/2026/06/eset-gamaredon-russia-aligned-threat-actor"
    assert R._match("Nexcorium Mirai", IDX) == "sources/2026/06/fortinet-mirai-nexcorium-botnet"


def test_ambiguous_or_vague_left_alone():
    assert R._match("Vendor advisory", IDX) is None          # only stopwords -> no tokens
    assert R._match("ESET report", IDX) is None               # "eset" matches 2 sources -> ambiguous
    assert R._match("Cisco Talos disclosure", IDX) is None    # no source slug has these tokens


def test_relink_text_rewrites_only_confident_prose():
    text = ("---\ntype: entity\nname: Gamaredon\nsources:\n"
            "- Gamaredon ESET writeup\n"
            "- Vendor advisory\n"
            "- sources/2026/06/already-a-page\n"
            "---\n# body\n")
    new, n = R.relink_text(text, IDX)
    assert n == 1
    assert "- sources/2026/06/eset-gamaredon-russia-aligned-threat-actor" in new   # relinked
    assert "- Vendor advisory" in new                                              # vague kept
    assert "- sources/2026/06/already-a-page" in new                              # page-ref untouched
    assert "Gamaredon ESET writeup" not in new                                     # prose replaced


def test_source_index_and_main_relink_only_entities(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    source = wiki / "sources" / "2026" / "07" / "cisco-talostest-malware.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source\n---\n")
    entity = wiki / "entities" / "m" / "malware.md"
    entity.parent.mkdir(parents=True)
    entity.write_text(
        "---\ntype: malware\nsources:\n  - Cisco Talostest malware report\n---\nBody\n")
    skipped = wiki / "entities" / "_index.md"
    skipped.write_text("---\ntype: index\nsources:\n  - Cisco Talostest malware report\n---\n")
    monkeypatch.setattr(R, "VAULT", tmp_path)
    monkeypatch.setattr(R, "WIKI", wiki)
    monkeypatch.setattr(R, "BATCH", 1)

    index = R._source_index()
    assert index == {
        "sources/2026/07/cisco-talostest-malware": {"cisco", "talostest", "malware"},
    }
    assert R.main() == 0
    assert "sources/2026/07/cisco-talostest-malware" in entity.read_text()
    assert "Cisco Talostest malware report" in skipped.read_text()
    assert "1 prose entr(ies)" in capsys.readouterr().out
    assert R.main() == 0
    assert "0 prose entr(ies)" in capsys.readouterr().out


def test_main_rejects_missing_wiki(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(R, "WIKI", tmp_path / "missing")
    assert R.main() == 1
    assert "wiki not found" in capsys.readouterr().err


def test_source_index_and_relink_text_edge_shapes(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    monkeypatch.setattr(R, "WIKI", wiki)
    assert R._source_index() == {}
    sources = wiki / "sources"
    sources.mkdir()
    for name in ("_skip.md", "INDEX.md", "INDEX-extra.md"):
        (sources / name).write_text("ignored")
    assert R._source_index() == {}
    assert R._match("Report advisory", {}) is None
    assert R.relink_text("plain", IDX) == ("plain", 0)
    no_sources = "---\ntype: entity\n---\nbody\n"
    assert R.relink_text(no_sources, IDX) == (no_sources, 0)
    source_at_end = "---\ntype: entity\nsources:\n---\n"
    assert R.relink_text(source_at_end, IDX) == (source_at_end, 0)
    # An indented blank/non-item line advances within the source list; a top-level
    # key terminates it. Quoted values retain newline behavior when rewritten.
    text = ("---\ntype: entity\nsources:\n  \n  - 'Gamaredon ESET writeup'\n"
            "title: stop here\n---\n")
    new, count = R.relink_text(text, IDX)
    assert count == 1 and new.endswith("\n")


def test_main_batch_read_error_and_no_source_field(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    entities = wiki / "entities"
    entities.mkdir(parents=True)
    first = entities / "a.md"
    first.write_text("---\ntype: entity\nsources:\n- prose\n---\n")
    unreadable = entities / "b.md"
    unreadable.write_text("---\ntype: entity\nsources:\n- prose\n---\n")
    clean = entities / "c.md"
    clean.write_text("---\ntype: entity\n---\n")
    monkeypatch.setattr(R, "WIKI", wiki)
    monkeypatch.setattr(R, "BATCH", 1)
    monkeypatch.setattr(R, "_source_index", lambda: {})
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("race"))
        if self == unreadable else original(self, *a, **k),
    )
    assert R.main() == 0

    # Force one edit so the next loop iteration exits at the batch boundary.
    monkeypatch.setattr(R, "relink_text", lambda text, _idx: (text + "changed", 1))
    assert R.main() == 0
    monkeypatch.setattr(R, "BATCH", 0)
    assert R.main() == 0
