"""Regression: tier_refresh must tier walk-up sub-domain pages with the right namespace.

_count_namespace walked the sub-domain bases (okengine#178) but computed `rel` relative to WIKI, so
a sub-domain source read as `<subdomain>/sources/…` — tier_of inferred the namespace as the SUBDOMAIN
name, the sources date/status tiering config never applied, and the page fell to untiered (multipack
under-count). rel is now namespace-relative.
"""
import importlib.util
import os
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "tier_refresh.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("tier_refresh", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["tier_refresh"] = m
    spec.loader.exec_module(m)
    return m


def test_subdomain_source_is_tiered_not_dropped(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "sources" / "2026" / "07").mkdir(parents=True)
    (wiki / "sources" / "2026" / "07" / "root-src.md").write_text(
        "---\ntype: source\npublished: 2026-07-01\n---\n", encoding="utf-8")
    (wiki / "sec").mkdir()
    (wiki / "sec" / "schema.yaml").write_text("types: {source: {}}\n", encoding="utf-8")
    (wiki / "sec" / "sources" / "2026" / "07").mkdir(parents=True)
    (wiki / "sec" / "sources" / "2026" / "07" / "sub-src.md").write_text(
        "---\ntype: source\npublished: 2026-07-01\n---\n", encoding="utf-8")

    m = _load(tmp_path)
    cfg = {"hot_days": 30, "warm_days": 365,
           "namespaces": {"sources": {"date_field": "published", "from_path": True}}}
    counts = m._count_namespace("sources", cfg["namespaces"]["sources"], cfg, date(2026, 7, 7))
    # BOTH the root and the sub-domain source are counted + tiered (was 1 — the sub-domain fell out)
    assert sum(counts.values()) == 2
