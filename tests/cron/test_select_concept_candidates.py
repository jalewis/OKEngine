"""Regression: the concept-backfill wake-gate must recognize HIERARCHICAL
concept pages as existing.

After the OKF migration, concepts live at ``wiki/concepts/<letter>/<slug>.md``.
A flat ``glob('*.md')`` finds only ``INDEX.md`` and floods the agent with
already-existing concepts as "missing" — which the MCP write path then refuses
(and the old file_write path would have flat-duplicated). This locks in the
recursive, dual-form existence check.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_concept_candidates.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.modules.pop("select_concept_candidates", None)
    spec = importlib.util.spec_from_file_location("select_concept_candidates", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_hierarchical_concepts_count_as_existing(tmp_path):
    wiki = tmp_path / "wiki"
    cdir = wiki / "concepts"
    (cdir / "r").mkdir(parents=True)
    (cdir / "p").mkdir(parents=True)
    (cdir / "r" / "ransomware.md").write_text("---\ntype: concept\nsources: [x]\n---\n# Ransomware\n")
    (cdir / "p" / "phishing.md").write_text("---\ntype: concept\nsources: [x]\n---\n# Phishing\n")
    (cdir / "INDEX.md").write_text("---\ntype: dashboard\ntitle: Index\n---\n")
    m = _load(tmp_path)

    existing = m.list_existing_concepts()
    # both forms present so either wikilink style matches
    assert "r/ransomware" in existing and "ransomware" in existing
    assert "p/phishing" in existing and "phishing" in existing
    assert "INDEX" not in existing


def test_existing_hierarchical_concept_not_reported_missing(tmp_path):
    wiki = tmp_path / "wiki"
    cdir = wiki / "concepts"
    (cdir / "r").mkdir(parents=True)
    (cdir / "r" / "ransomware.md").write_text("---\ntype: concept\nsources: [x]\n---\n# Ransomware\n")
    # a page that links to the concept BOTH ways + to a genuinely-missing one
    (wiki / "sources").mkdir()
    (wiki / "sources" / "s1.md").write_text(
        "---\ntype: source\n---\nSee [[concepts/r/ransomware]] and "
        "[[concepts/ransomware]] and [[concepts/q/quantum-risk]].\n")
    m = _load(tmp_path)

    existing = m.list_existing_concepts()
    refs = m.scan_wikilinks()
    missing = {slug for slug in refs if slug not in existing}
    # the existing concept must NOT be missing in either link form
    assert "r/ransomware" not in missing
    assert "ransomware" not in missing
    # the genuinely-absent one IS missing
    assert "q/quantum-risk" in missing
