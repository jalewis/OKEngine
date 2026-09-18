"""Regression: ensure_readable restores the reader-needed read bits on pages a
writer (e.g. the Hermes file tool for the daily brief) left owner-only 0600."""
import importlib.util
import os
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "ensure_readable.py"


def _load():
    spec = importlib.util.spec_from_file_location("ensure_readable", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_restores_other_read_on_owner_only_page(tmp_path):
    m = _load()
    wiki = tmp_path / "wiki"
    (wiki / "briefings").mkdir(parents=True)
    (wiki / "entities" / "a").mkdir(parents=True)
    brief = wiki / "briefings" / "2026-06-22.md"
    brief.write_text("---\ntype: dashboard\n---\nbrief\n")
    os.chmod(brief, 0o600)                                 # file-tool default — reader can't read
    ok = wiki / "entities" / "a" / "acme.md"
    ok.write_text("---\ntype: entity\nname: Acme\n---\nx\n")
    os.chmod(ok, 0o644)                                    # already fine

    res = m.run(wiki)

    assert res["fixed"] == 1 and res["pages"] == ["briefings/2026-06-22.md"]
    assert (brief.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH)) == (stat.S_IRGRP | stat.S_IROTH)
    # additive only: the owner-write bit and the already-good page are untouched
    assert brief.stat().st_mode & stat.S_IWUSR
    assert (ok.stat().st_mode & 0o777) == 0o644

    # idempotent: a second run fixes nothing
    assert m.run(wiki)["fixed"] == 0


def test_missing_wiki_and_main_reporting(tmp_path, monkeypatch, capsys):
    m = _load()
    assert m.run(tmp_path / "missing") == {"fixed": 0, "pages": []}

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "private.md"
    page.write_text("body")
    os.chmod(page, 0o600)
    monkeypatch.setattr(m, "WIKI", wiki)
    assert m.main() == 0
    captured = capsys.readouterr()
    assert "restored g+r/o+r on 1 page" in captured.err
    assert '"wakeAgent": false' in captured.out

    assert m.main() == 0
    assert "all pages already reader-readable" in capsys.readouterr().err


def test_scan_tolerates_stat_and_chmod_races(tmp_path, monkeypatch):
    m = _load()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    stat_gone = wiki / "stat-gone.md"
    chmod_denied = wiki / "chmod-denied.md"
    stat_gone.write_text("x")
    chmod_denied.write_text("x")
    os.chmod(chmod_denied, 0o600)
    original_stat = Path.stat
    original_chmod = os.chmod
    monkeypatch.setattr(
        Path, "stat",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone"))
        if self == stat_gone else original_stat(self, *a, **k),
    )
    monkeypatch.setattr(
        os, "chmod",
        lambda path, mode: (_ for _ in ()).throw(OSError("denied"))
        if Path(path) == chmod_denied else original_chmod(path, mode),
    )
    assert m.run(wiki) == {"fixed": 0, "pages": []}
