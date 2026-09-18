"""Regression: quarantining surplus captures must never strand a reference.

Captures are primary evidence, so this moves rather than deletes. The identity guard scans by VALUE
SHAPE rather than field name: five distinct fields point into the raw tree, and a `raw:`-only scan
would have quarantined 1,752 captures that pack-defined fields cite — manufacturing exactly the
dangling references such a corpus spends days repairing.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "dedupe_raw_captures.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="dedupe_raw_captures absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("dedupe_raw_captures", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dedupe_raw_captures"] = mod
    spec.loader.exec_module(mod)
    return mod


def _capture(vault: Path, name: str, url: str) -> Path:
    p = vault / "raw" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: raw\nurl: {url}\n---\n\nBody.\n", encoding="utf-8")
    return p


def _page(vault: Path, name: str, fm: str):
    p = vault / "wiki" / "sources" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: source\n{fm}---\n\nBody.\n", encoding="utf-8")


def test_a_cited_capture_is_never_surplus(tmp_path):
    mod = _load()
    _capture(tmp_path, "first", "https://example.com/a")
    _capture(tmp_path, "second", "https://example.com/a")
    _page(tmp_path, "p", "raw: raw/second.md\n")

    surplus, _stats = mod.plan(tmp_path)
    assert [p.name for p in surplus] == ["first.md"], "the cited copy must be the one kept"


def test_a_reference_from_any_field_protects_a_capture(tmp_path):
    """Not just `raw:`. A pack importer may cite captures under any field name it likes, and the
    engine cannot enumerate pack vocabulary — so the guard keys on the value's shape."""
    mod = _load()
    _capture(tmp_path, "first", "https://example.com/a")
    _capture(tmp_path, "second", "https://example.com/a")
    _page(tmp_path, "p", "some_pack_defined_refs:\n- raw/second.md\n")

    surplus, _stats = mod.plan(tmp_path)
    assert [p.name for p in surplus] == ["first.md"]


def test_a_rotating_cache_buster_groups_as_one_article(tmp_path):
    mod = _load()
    _capture(tmp_path, "a", "https://example.com/post/?p=111")
    _capture(tmp_path, "b", "https://example.com/post/?p=222")

    surplus, stats = mod.plan(tmp_path)
    assert len(surplus) == 1 and stats["surplus captures"] == 1


def test_distinct_articles_are_never_grouped(tmp_path):
    """Over-grouping would quarantine real evidence, which is unrecoverable in intent if not in fact."""
    mod = _load()
    _capture(tmp_path, "a", "https://example.com/one")
    _capture(tmp_path, "b", "https://example.com/two")
    surplus, _stats = mod.plan(tmp_path)
    assert surplus == []


def test_an_unreadable_or_urlless_capture_is_left_alone(tmp_path):
    """Cannot establish identity means cannot judge surplus — leave it where it is."""
    mod = _load()
    p = tmp_path / "raw" / "nourl.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\ntype: raw\n---\n\nBody.\n", encoding="utf-8")

    surplus, stats = mod.plan(tmp_path)
    assert surplus == []
    assert stats["no url — cannot group, left alone"] == 1


def test_main_dry_run_moves_nothing_and_reports(tmp_path, monkeypatch, capsys):
    """Dry-run is the DEFAULT for a script that relocates primary evidence."""
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    _capture(tmp_path, "a", "https://example.com/x")
    _capture(tmp_path, "b", "https://example.com/x")

    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out and "0 quarantined" in out
    assert len(list((tmp_path / "raw").glob("*.md"))) == 2, "dry-run must not move anything"


def test_main_apply_quarantines_the_surplus(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    _capture(tmp_path, "a", "https://example.com/x")
    _capture(tmp_path, "b", "https://example.com/x")
    quarantine = tmp_path / "q"

    rc = mod.main(["--apply", "--quarantine", str(quarantine)])
    out = capsys.readouterr().out
    assert rc == 0 and "APPLY" in out and "1 quarantined" in out
    assert len(list((tmp_path / "raw").glob("*.md"))) == 1, "one copy is kept"
    assert list(quarantine.rglob("*.md")), "the surplus is MOVED, never deleted"


def test_main_never_overwrites_an_existing_quarantined_name(tmp_path, monkeypatch):
    """Two runs must not silently clobber: quarantine is the only copy of what was moved."""
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    quarantine = tmp_path / "q"
    # BOTH names are pre-occupied. Seeding only one leaves the test passing whichever copy the
    # dedupe happens to keep, so it would not actually reach the collision it claims to cover.
    for name in ("a", "b"):
        dest = quarantine / "raw" / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"an earlier quarantined {name}\n", encoding="utf-8")
    _capture(tmp_path, "a", "https://example.com/x")
    _capture(tmp_path, "b", "https://example.com/x")

    mod.main(["--apply", "--quarantine", str(quarantine)])
    for name in ("a", "b"):
        assert (quarantine / "raw" / f"{name}.md").read_text(encoding="utf-8") == (
            f"an earlier quarantined {name}\n"), f"{name} was clobbered"
    assert len(list(quarantine.rglob("*.md"))) == 3, "the new file lands beside them, not on one"


def test_an_unparseable_capture_is_left_alone_not_grouped(tmp_path, monkeypatch):
    """A capture whose frontmatter will not parse has no establishable identity, so it cannot be
    judged surplus. The live corpus contained 6 of these; grouping them would risk quarantining a
    unique record on the strength of a failed parse."""
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    bad = tmp_path / "raw" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\ntitle: 'unterminated\n---\n\nBody.\n", encoding="utf-8")
    _capture(tmp_path, "ok", "https://example.com/x")

    surplus, stats = mod.plan(tmp_path)
    assert surplus == []
    assert stats.get("no url — cannot group, left alone", 0) + \
        stats.get("unreadable — left alone", 0) >= 1


def test_a_capture_with_no_frontmatter_is_left_alone(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    p = tmp_path / "raw" / "plain.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("no frontmatter at all\n", encoding="utf-8")
    surplus, _stats = mod.plan(tmp_path)
    assert surplus == []


def test_an_unreadable_page_does_not_abort_the_scan(tmp_path, monkeypatch):
    """One bad file must not cost the whole run — that is how a repair lane becomes unrunnable."""
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    _capture(tmp_path, "a", "https://example.com/x")
    _capture(tmp_path, "b", "https://example.com/x")
    target = tmp_path / "raw" / "a.md"
    original = mod.Path.read_text

    def read_text(self, *args, **kwargs):
        if self == target:
            raise OSError("fixture")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(mod.Path, "read_text", read_text)
    surplus, _stats = mod.plan(tmp_path)
    assert isinstance(surplus, list)


def test_a_wiki_page_without_frontmatter_references_nothing(tmp_path, monkeypatch):
    """The reference scan reads every page in the vault to decide what is safe to quarantine. A page
    it cannot parse declares no references — but it must not stop the scan either, because the pages
    AFTER it are the ones still protecting captures."""
    mod = _load()
    monkeypatch.setattr(mod, "VAULT", tmp_path)
    kept = _capture(tmp_path, "a", "https://example.com/x")
    _capture(tmp_path, "b", "https://example.com/x")
    prose = tmp_path / "wiki" / "sources" / "prose.md"
    prose.parent.mkdir(parents=True, exist_ok=True)
    prose.write_text("# no frontmatter, mentions raw/a.md in passing\n", encoding="utf-8")
    _page(tmp_path, "citing", "raw: raw/a.md\n")

    rc = mod.main([])
    assert rc == 0
    assert kept.is_file(), "the cited capture is protected by the page that CAN be parsed"
