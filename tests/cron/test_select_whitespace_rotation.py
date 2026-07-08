"""Regression: the whitespace wake-gate must rotate on the MAPPED capability, not every cited concept.

_recently_thesised() read every `[[concepts/…]]` link in a whitespace-thesis, so a concept the
thesis merely referenced (see_also / body comparison) was excluded from whitespace discovery for
REANALYZE_DAYS — starving genuinely-un-thesised, demand-rich/supply-thin capabilities (the same
okengine.lacuna precedent). This pins reading the authoritative `capability` frontmatter.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "extensions" / "okengine.frontier-watch" / "select_whitespace.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["OKENGINE_MCP_WRITE_DATE"] = "2026-07-07"
    spec = importlib.util.spec_from_file_location("select_whitespace", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_whitespace"] = m
    spec.loader.exec_module(m)
    return m


def test_covers_capability_not_secondary_citations(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "frontier").mkdir(parents=True)
    (wiki / "frontier" / "agentic-soc-gap.md").write_text(
        "---\n"
        "type: whitespace-thesis\n"
        "capability: '[[concepts/agentic-soc]]'\n"
        "updated: 2026-07-07\n"
        "---\n"
        "The gap resembles [[concepts/autonomous-remediation]] and [[concepts/detection-as-code]].\n",
        encoding="utf-8")
    m = _load(tmp_path)
    covered = m._recently_thesised()
    assert "agentic-soc" in covered                       # the mapped capability IS retired
    assert "autonomous-remediation" not in covered        # a secondary citation is NOT over-excluded
    assert "detection-as-code" not in covered


def test_legacy_thesis_without_capability_falls_back_to_links(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "frontier").mkdir(parents=True)
    (wiki / "frontier" / "old.md").write_text(
        "---\ntype: whitespace-thesis\nupdated: 2026-07-07\n---\nMaps [[concepts/x/legacy-cap]].\n",
        encoding="utf-8")
    m = _load(tmp_path)
    assert "legacy-cap" in m._recently_thesised()
