import importlib.util
from pathlib import Path


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
