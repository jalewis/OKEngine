"""CLAUDE.md's okengine delivery-discipline section stays present, short, and points at real paths.

CLAUDE.md and scripts/audit/ are excluded from the public snapshot, so every check is guarded.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLAUDE = REPO / "CLAUDE.md"
HEADING = "## Delivery discipline (okengine specifics)"


def _section() -> str:
    if not CLAUDE.is_file():
        pytest.skip("CLAUDE.md is excluded from this checkout (public snapshot)")
    text = CLAUDE.read_text(encoding="utf-8")
    assert HEADING in text, "the delivery-discipline section was removed"
    body = text.split(HEADING, 1)[1]
    return body.split("\n## ", 1)[0]


def test_section_sits_after_the_ci_gate_traps():
    _section()
    text = CLAUDE.read_text(encoding="utf-8")
    assert text.index("## CI gate traps") < text.index(HEADING) < text.index("## Before a release")


def test_section_stays_short():
    lines = [line for line in _section().strip().splitlines() if line.strip()]
    assert len(lines) <= 16, f"keep it tight: {len(lines)} lines"


def test_named_paths_exist_and_pythonpath_matches_the_documented_one():
    body = _section()
    for rel in re.findall(r"`((?:scripts|src)/[^`\s]+)`", body):
        if rel.startswith("scripts/audit/") and not (REPO / "scripts" / "audit").is_dir():
            continue   # publish-excluded with CLAUDE.md
        assert (REPO / rel).exists(), rel
    assert "`.:src:okengine-mcp:scripts/cron`" in body
