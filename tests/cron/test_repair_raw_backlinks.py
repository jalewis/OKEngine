"""Regression: a reattached `raw:` backlink must be certain, never a best guess.

A wrong backlink asserts a provenance that never happened, and unlike an absence nothing downstream
would question it. A loose title match "found" 4 of 8 matches during triage that were false — it had
paired CrowdStrike's "622 vulnerabilities" capture with BleepingComputer's "570 flaws" page, two
different articles about the same Patch Tuesday.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "repair_raw_backlinks.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="repair_raw_backlinks absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("repair_raw_backlinks", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["repair_raw_backlinks"] = mod
    spec.loader.exec_module(mod)
    return mod


def _page(vault: Path, name: str, fm: str):
    p = vault / "wiki" / "sources" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: source\n{fm}---\n\nBody.\n", encoding="utf-8")
    return p


def _raw(vault: Path, name: str, fm: str):
    p = vault / "raw" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: raw\n{fm}---\n\nBody.\n", encoding="utf-8")
    return p


def test_an_exact_url_match_is_joined(tmp_path):
    mod = _load()
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\nurl: https://example.com/a\n")
    _page(tmp_path, "page", "title: A Long Enough Title Here\nurl: https://example.com/a\n")

    joins, _stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert [rel for _p, rel, _how in joins] == ["raw/cap.md"]


def test_two_captures_sharing_a_title_are_never_joined(tmp_path):
    """Ambiguity is reported and skipped, never resolved by preference."""
    mod = _load()
    _raw(tmp_path, "cap1", "title: Patch Tuesday Coverage Report\n")
    _raw(tmp_path, "cap2", "title: Patch Tuesday Coverage Report\n")
    _page(tmp_path, "page", "title: Patch Tuesday Coverage Report\n")

    joins, stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []
    assert any("ambiguous" in k for k in stats)


def test_reference_data_is_never_given_a_backlink(tmp_path):
    """API imports have no raw capture at all; a missing backlink there is correct."""
    mod = _load()
    _raw(tmp_path, "cap", "title: Some Reference Dataset Row\n")
    _page(tmp_path, "page", "title: Some Reference Dataset Row\nsource_kind: reference-data\n")

    joins, stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []
    assert stats["reference-data (no raw capture by design)"] == 1


def test_a_capture_already_cited_is_not_reattached_to_a_second_page(tmp_path):
    """One capture produced one page; claiming it twice would invent provenance."""
    mod = _load()
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\n")
    _page(tmp_path, "owner", "title: Irrelevant\nraw: raw/cap.md\n")
    _page(tmp_path, "other", "title: A Long Enough Title Here\n")

    joins, stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []
    assert stats["raw file already cited by another page"] == 1


def test_short_titles_never_match(tmp_path):
    """Short titles collide across unrelated captures."""
    mod = _load()
    _raw(tmp_path, "cap", "title: News\n")
    _page(tmp_path, "page", "title: News\n")
    joins, _stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []


def test_main_dry_run_reports_without_writing(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\nurl: https://example.com/a\n")
    page = _page(tmp_path, "page", "title: A Long Enough Title Here\nurl: https://example.com/a\n")

    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0 and "DRY-RUN" in out and "0 written" in out
    assert "raw:" not in page.read_text(encoding="utf-8"), "dry-run must not write"


def test_main_apply_reattaches_and_is_idempotent(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\nurl: https://example.com/a\n")
    page = _page(tmp_path, "page", "title: A Long Enough Title Here\nurl: https://example.com/a\n")

    assert mod.main(["--apply"]) == 0
    assert "raw: raw/cap.md" in page.read_text(encoding="utf-8")
    capsys.readouterr()

    # a second run must find nothing left to do — a repair that repeats itself is a repair that
    # cannot be run on a schedule
    assert mod.main(["--apply"]) == 0
    assert "0 backlink(s) reattachable" in capsys.readouterr().out


def test_main_limit_bounds_the_batch(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    for i in range(3):
        _raw(tmp_path, f"cap{i}", f"title: Distinct Title Number {i} Here\nurl: https://e.com/{i}\n")
        _page(tmp_path, f"page{i}", f"title: Distinct Title Number {i} Here\nurl: https://e.com/{i}\n")

    mod.main(["--apply", "--limit", "1"])
    assert "1 written" in capsys.readouterr().out


def test_an_unparseable_page_is_skipped_not_joined(tmp_path, monkeypatch):
    """A page whose frontmatter will not parse has no title or url to match on. Joining it on
    filename alone would assert a provenance nothing verified."""
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    bad = tmp_path / "wiki" / "sources" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\ntitle: 'unterminated\n---\n\nBody.\n", encoding="utf-8")
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\n")

    joins, _stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []


def test_an_unreadable_capture_does_not_abort_the_index(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\n")
    target = tmp_path / "raw" / "cap.md"
    original = mod.Path.read_text

    def read_text(self, *args, **kwargs):
        if self == target:
            raise OSError("fixture")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(mod.Path, "read_text", read_text)
    by_title, by_url = mod.index_raw(tmp_path)
    assert isinstance(by_title, dict) and isinstance(by_url, dict)


def test_a_page_with_no_frontmatter_is_ignored(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    p = tmp_path / "wiki" / "sources" / "plain.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("no frontmatter\n", encoding="utf-8")
    joins, _stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []


def test_two_pages_sharing_a_title_are_never_joined_to_one_capture(tmp_path):
    """The mirror of the capture-side ambiguity: one capture, two pages claiming the same title.
    Joining would attach the provenance to whichever page sorted first — a 50/50 guess written into
    the record as fact. Both sides of the match have to be unique before a join is safe."""
    mod = _load()
    _raw(tmp_path, "cap", "title: Patch Tuesday Coverage Report\n")
    _page(tmp_path, "page-one", "title: Patch Tuesday Coverage Report\n")
    _page(tmp_path, "page-two", "title: Patch Tuesday Coverage Report\n")

    joins, stats = mod.survey(tmp_path, *mod.index_raw(tmp_path))
    assert joins == []
    assert stats["ambiguous — several pages share the title"] == 2


def test_a_page_that_gains_a_backlink_mid_run_is_not_overwritten(tmp_path, monkeypatch, capsys):
    """The survey and the write are two separate reads of the same page. If another lane attaches a
    raw capture in between, this one must not overwrite that attribution with its own inference —
    a recorded provenance outranks a guessed one."""
    mod = _load()
    _raw(tmp_path, "cap", "title: A Long Enough Title Here\nurl: https://example.com/a\n")
    page = _page(tmp_path, "page", "title: A Long Enough Title Here\nurl: https://example.com/a\n")

    real = mod.read_page
    seen = {"n": 0}

    def racing(path, *a, **kw):
        fm, body = real(path, *a, **kw)
        if path == page and fm:
            seen["n"] += 1
            if seen["n"] > 1:                       # the apply-phase re-read
                fm = {**fm, "raw": "raw/other-capture.md"}
        return fm, body

    monkeypatch.setattr(mod, "read_page", racing)
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    monkeypatch.setattr(mod, "WIKI", tmp_path / "wiki")
    assert mod.main(["--apply"]) == 0
    assert "raw_backlink_repaired" not in page.read_text(encoding="utf-8")
