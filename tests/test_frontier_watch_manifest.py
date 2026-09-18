"""Regression: a lane gated on a selection manifest must have a selector that writes one.

`whitespace-sweep` verifies its work against a selection manifest, and its selector never wrote
one — so every run failed the completion receipt with "selection manifest unavailable" regardless
of what the agent did. Same shape as the six lanes that demanded a receipt against a manifest their
selectors never wrote (171 guaranteed failures), and invisible from the lane's own output: the
wake-gate digest looked perfectly healthy right up to the receipt check.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEL = REPO / "extensions" / "okengine.frontier-watch" / "select_whitespace.py"


def test_the_selector_writes_a_selection_manifest():
    src = SEL.read_text(encoding="utf-8")
    assert "write_selection_manifest(" in src, (
        "the lane's receipt check reads a manifest this selector must write")


def test_the_manifest_name_matches_the_lane_id():
    """The receipt looks for `<extension>:<operation>.json`; a mismatch fails exactly like absence."""
    src = SEL.read_text(encoding="utf-8")
    assert "okengine.frontier-watch:whitespace-sweep.json" in src


def test_the_manifest_is_written_before_the_digest():
    """So a receipt has something to verify even if the agent turn dies partway through."""
    src = SEL.read_text(encoding="utf-8")
    assert src.index("write_selection_manifest(") < src.index('print("=== whitespace candidates ===")')


def test_every_selector_on_the_shared_convention_writes_its_manifest():
    """The class, not the instance.

    Scoped to the SHARED `cron-plus/selections/` convention, which is what the completion-receipt
    checker reads. A first version flagged any selector mentioning SELECTION_MANIFEST and produced
    two false positives: `select_raw_batch` writes `raw/.selection.json` with its own completion
    ledger, and `select_dup_candidates` takes a caller-supplied path. Both are self-consistent —
    they simply do not use this convention, and a test that cannot tell the difference between a
    different convention and a missing write is worse than no test.
    """
    missing = []
    for path in sorted(REPO.glob("extensions/*/select_*.py")) + sorted(
            REPO.glob("scripts/cron/select_*.py")):
        src = path.read_text(encoding="utf-8")
        on_shared = '"cron-plus" / "selections"' in src or "cron-plus/selections" in src
        if on_shared and "write_selection_manifest(" not in src:
            missing.append(path.relative_to(REPO).as_posix())
    assert not missing, f"selectors on the shared convention that never write a manifest: {missing}"
