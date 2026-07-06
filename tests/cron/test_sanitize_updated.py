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
