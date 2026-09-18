import importlib.util
import sys
from pathlib import Path

import pytest


MOD = Path(__file__).parents[1] / "scripts" / "dedupe_review_queue.py"
spec = importlib.util.spec_from_file_location("dedupe_review_queue", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_dedupe_retains_first_row_and_unrelated_content():
    text = (
        "---\ntitle: Review Queue\n---\n\n# Review Queue\n\n"
        "- 2026-07-22 **sources/a.md** — first reason\n"
        "free-form operator note\n"
        "- 2026-07-22 **sources/b.md** — only reason\n"
        "- 2026-07-23 **sources/a.md** — replay wording\n"
    )
    cleaned, removed = m.dedupe(text)
    assert removed == ["sources/a.md"]
    assert cleaned.count("**sources/a.md**") == 1
    assert "first reason" in cleaned and "replay wording" not in cleaned
    assert "free-form operator note" in cleaned and "**sources/b.md**" in cleaned


def test_dedupe_ignores_noncanonical_bullets():
    text = "- note **sources/a.md** — x\n- note **sources/a.md** — x\n"
    assert m.dedupe(text) == (text, [])


def test_main_dry_run_then_write_with_backup(tmp_path, monkeypatch, capsys):
    queue = tmp_path / "wiki" / "_review-queue.md"
    queue.parent.mkdir()
    original = (
        "- 2026-07-22 **sources/a.md** — first\n"
        "- 2026-07-23 **sources/a.md** — duplicate\n"
    )
    queue.write_text(original)
    monkeypatch.setattr(sys, "argv", ["dedupe-review", "--pack", str(tmp_path)])
    assert m.main() == 0
    assert queue.read_text() == original
    assert "dry run" in capsys.readouterr().out

    monkeypatch.setattr(
        sys, "argv", ["dedupe-review", "--pack", str(tmp_path), "--write"])
    assert m.main() == 0
    assert "duplicate" not in queue.read_text()
    backup = queue.with_suffix(".md.bak-okengine-397")
    assert backup.read_text() == original
    assert "updated:" in capsys.readouterr().out

    # An existing backup is preserved on a later repair.
    queue.write_text(original)
    backup.write_text("operator-owned backup")
    assert m.main() == 0
    assert backup.read_text() == "operator-owned backup"

    # A clean queue takes neither the write nor dry-run branch.
    queue.write_text("# Review Queue\n")
    assert m.main() == 0
    assert "dry run" not in capsys.readouterr().out


def test_main_errors_when_queue_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dedupe-review", "--pack", str(tmp_path)])
    with pytest.raises(SystemExit) as raised:
        m.main()
    assert raised.value.code == 2
