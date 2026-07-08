"""Regression: source-staleness must resolve citations to date-partitioned sources.

The score map is keyed `sources/<stem>`, but normalize_link returned the full citation path
(`sources/2026/07/foo`), so a citation to any date-partitioned source never matched its score
entry — staleness was silently never applied to partitioned sources (the common case). This pins
the `<namespace>/<slug>` stem collapse so partitioned and flat citations both resolve.
"""
import importlib.util
import os
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_source_staleness.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ.pop("DECAY_ENTITY_TYPES", None)   # empty ⇒ accept any entity/concept type
    import sys
    spec = importlib.util.spec_from_file_location("select_source_staleness", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_source_staleness"] = m   # register before exec so dataclasses resolve __module__
    spec.loader.exec_module(m)
    return m


def test_normalize_link_collapses_partition_to_stem(tmp_path):
    m = _load(tmp_path)
    assert m.normalize_link("[[sources/2026/07/foo]]") == "sources/foo"
    assert m.normalize_link("sources/2026/07/foo.md") == "sources/foo"
    assert m.normalize_link("sources/foo") == "sources/foo"          # flat still works
    assert m.normalize_link("[[sources/2026/07/foo|Foo]]") == "sources/foo"


def test_partitioned_source_citation_resolves_end_to_end(tmp_path):
    wiki = tmp_path / "wiki"
    src = wiki / "sources" / "2026" / "01"
    src.mkdir(parents=True)
    # a clearly-stale source (old, low reliability) at a date-partitioned path
    (src / "old-report.md").write_text(
        "---\ntype: source\npublished: 2020-01-01\nreliability: C\ncredibility: 3\n"
        "source_kind: news\n---\n# Old Report\n", encoding="utf-8")
    con = wiki / "concepts" / "x"
    con.mkdir(parents=True)
    # concept cites the source via the FULL partition path — the case that used to fail
    (con / "foo.md").write_text(
        "---\ntype: concept\nsources: ['[[sources/2026/01/old-report]]']\n---\n# Foo\n",
        encoding="utf-8")
    m = _load(tmp_path)
    scores = m.score_all_sources(date(2026, 7, 7))
    assert "sources/old-report" in scores                      # producer key (stem)
    anchors = m.discover_anchors(scores)
    # the concept's citation must have resolved to a score (non-empty) — the bug made this empty
    assert any(a.segment == "concepts" and a.rel_path == "concepts/foo" for a in anchors), \
        "partitioned-source citation did not resolve — anchor was dropped"
