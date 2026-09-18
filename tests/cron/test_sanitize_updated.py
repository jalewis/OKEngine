"""sanitize_frontmatter_updated collapses a malformed multi-value `updated:` to the newest —
now timestamp-aware (must preserve the time, not truncate to date)."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "sanitize_frontmatter_updated.py"


def _mod():
    spec = importlib.util.spec_from_file_location("sanitize_frontmatter_updated", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["sanitize_frontmatter_updated"] = m
    spec.loader.exec_module(m)
    return m


def test_collapses_multi_timestamp_to_newest_preserving_time():
    out, fixes = _mod().sanitize_text(
        "---\nupdated: 2026-06-28T14:30:00Z 2026-06-27T10:00:00Z\n---\n")
    assert "updated: 2026-06-28T14:30:00Z" in out and "2026-06-27" not in out
    assert fixes


def test_collapses_multi_date_backcompat():
    out, _ = _mod().sanitize_text("---\nupdated: 2026-05-28 2026-05-26 2026-05-24\n---\n")
    assert "updated: 2026-05-28" in out and "2026-05-26" not in out


def test_single_timestamp_left_untouched():
    src = "---\nupdated: 2026-06-28T14:30:00Z\n---\n"
    out, fixes = _mod().sanitize_text(src)
    assert out == src and not fixes


def test_main_updates_only_live_pages(tmp_path, monkeypatch, capsys):
    m = _mod()
    wiki = tmp_path / "wiki"
    live = wiki / "entities" / "live.md"
    backup = wiki / "entities.bak.20260724" / "backup.md"
    live.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    malformed = "---\nupdated: 2026-07-22 2026-07-24\n---\nBody\n"
    live.write_text(malformed)
    backup.write_text(malformed)
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)

    assert m.main() == 0
    assert "updated: 2026-07-24" in live.read_text()
    assert backup.read_text() == malformed
    assert "Fixed 1 value(s) across 1 file(s)" in capsys.readouterr().out
    assert m.main() == 0
    assert "Fixed 0 value(s)" in capsys.readouterr().out


def test_main_rejects_missing_wiki(tmp_path, monkeypatch, capsys):
    m = _mod()
    monkeypatch.setattr(m, "WIKI", tmp_path / "missing")
    assert m.main() == 1
    assert "does not exist" in capsys.readouterr().err


def test_main_tolerates_read_and_write_failures_and_reports_permissions(tmp_path, monkeypatch, capsys):
    m = _mod()
    wiki = tmp_path / "wiki"; wiki.mkdir()
    unreadable = wiki / "unreadable.md"; unreadable.mkdir()
    plain = wiki / "plain.md"; plain.write_text("body\n")
    denied = wiki / "denied.md"
    denied.write_text("---\nupdated: 2026-01-01 2026-01-02\n---\n")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)
    original = Path.write_text

    def write(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    assert m.main() == 0
    output = capsys.readouterr().out
    assert "PERMISSION DENIED" in output
    assert "1 file(s) skipped for permissions" in output
