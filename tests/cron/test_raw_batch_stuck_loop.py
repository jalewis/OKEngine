"""Regression: select_raw_batch must stop re-offering a raw file that never becomes processed.

A DUPLICATE raw file (its story already has a source under another slug) never gets its own source,
so it stayed "unprocessed" and — being newest — was re-offered in every batch: the raw-backfill lane
looped on the same duplicates forever instead of draining. The offer-count manifest now skips a file
after STUCK_AFTER fruitless offers so the selector advances.
"""
import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_raw_batch.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["CRON_DEFER_UTC_HOURS"] = ""          # never off-peak-defer in the test
    spec = importlib.util.spec_from_file_location("select_raw_batch", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_raw_batch"] = m
    spec.loader.exec_module(m)
    return m


def _run(m):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.main()
    return buf.getvalue()


def test_duplicate_stops_being_offered_after_stuck_after(tmp_path):
    (tmp_path / "raw" / "2026").mkdir(parents=True)
    (tmp_path / "raw" / "2026" / "dup.md").write_text("a duplicate story", encoding="utf-8")
    (tmp_path / "wiki" / "sources").mkdir(parents=True)   # NO source references dup -> always unprocessed
    m = _load(tmp_path)

    # STUCK_AFTER fruitless offers: the file appears in each batch...
    for i in range(m.STUCK_AFTER):
        out = _run(m)
        assert "raw/2026/dup.md" in out, f"run {i + 1} should still offer the file"

    # ...then it's skipped as stuck and the lane reports caught-up instead of looping forever
    out = _run(m)
    assert "raw/2026/dup.md" not in out
    assert "Backfill complete" in out
    assert "skipped" in out.lower()


def test_processed_file_is_pruned_and_never_stuck(tmp_path):
    (tmp_path / "raw" / "2026").mkdir(parents=True)
    (tmp_path / "raw" / "2026" / "real.md").write_text("a real story", encoding="utf-8")
    src = tmp_path / "wiki" / "sources"
    src.mkdir(parents=True)
    m = _load(tmp_path)
    _run(m)                                            # offer it once (count -> 1)
    # simulate the agent ingesting it: a source now carries its raw: path
    (src / "real-story.md").write_text(
        "---\ntype: source\nraw: raw/2026/real.md\n---\n", encoding="utf-8")
    out = _run(m)
    assert "real.md" not in out                        # now processed, not offered
