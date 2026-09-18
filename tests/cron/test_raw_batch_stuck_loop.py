"""Regression: rejected raw files remain visible and retryable until accepted.

A DUPLICATE raw file (its story already has a source under another slug) never gets its own source,
Duplicates are resolved by appending their raw key to an accepted canonical source. Offer count alone
must never claim completion because that hid rejected/empty compilations.
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
    values = {
        "WIKI_PATH": str(vault),
        "CRON_DEFER_UTC_HOURS": "",          # never off-peak-defer in the test
        "OKENGINE_LANE_ID": "raw-backfill-test",
        "OKENGINE_CONTRACT_DIGEST": "sha256:test-contract",
        "OKENGINE_SELECTION_MANIFEST": str(vault / "raw" / ".selection.json"),
    }
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        spec = importlib.util.spec_from_file_location("select_raw_batch", MOD)
        m = importlib.util.module_from_spec(spec)
        sys.modules["select_raw_batch"] = m
        spec.loader.exec_module(m)
        return m
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run(m):
    buf = io.StringIO()
    previous = {
        key: os.environ.get(key)
        for key in ("OKENGINE_LANE_ID", "OKENGINE_CONTRACT_DIGEST")
    }
    try:
        os.environ["OKENGINE_LANE_ID"] = "raw-backfill-test"
        os.environ["OKENGINE_CONTRACT_DIGEST"] = "sha256:test-contract"
        with contextlib.redirect_stdout(buf):
            m.main()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return buf.getvalue()


def test_rejected_item_remains_retryable_after_stuck_after(tmp_path):
    (tmp_path / "raw" / "2026").mkdir(parents=True)
    (tmp_path / "raw" / "2026" / "dup.md").write_text("a duplicate story", encoding="utf-8")
    (tmp_path / "wiki" / "sources").mkdir(parents=True)   # NO source references dup -> always unprocessed
    m = _load(tmp_path)

    # STUCK_AFTER fruitless offers: the file appears in each batch...
    for i in range(m.STUCK_AFTER):
        out = _run(m)
        assert "raw/2026/dup.md" in out, f"run {i + 1} should still offer the file"

    # ...and remains visible/retryable rather than being falsely marked complete.
    out = _run(m)
    assert "raw/2026/dup.md" in out
    assert "Retryable" in out


def test_processed_file_is_pruned_and_never_stuck(tmp_path):
    (tmp_path / "raw" / "2026").mkdir(parents=True)
    (tmp_path / "raw" / "2026" / "real.md").write_text("a real story", encoding="utf-8")
    src = tmp_path / "wiki" / "sources"
    src.mkdir(parents=True)
    m = _load(tmp_path)
    _run(m)                                            # offer it once (count -> 1)
    # simulate the agent ingesting it: a source now carries its raw: path
    (src / "real-story.md").write_text(
        "---\ntype: source\nraw: raw/2026/real.md\npublisher: Example\n"
        "published: 2026-01-01\n---\n\n# Real\n\n" + "Accepted source content. " * 6,
        encoding="utf-8")
    out = _run(m)
    assert "real.md" not in out                        # now processed, not offered
