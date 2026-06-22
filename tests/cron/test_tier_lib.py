"""Regression: derived hot/warm/cold tiering (G4) — tier_lib.tier_of."""
import importlib.util
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "tier_lib.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="tier_lib absent")


def _load():
    spec = importlib.util.spec_from_file_location("tier_lib", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TL = _load()
CFG = TL._DEFAULT_TIER
TODAY = date(2026, 6, 15)


def t(rel, fm=None):
    return TL.tier_of(rel, fm, CFG, TODAY)


def test_sources_tier_derived_from_path():
    assert t("sources/2026/06/x") == "hot"           # this month
    assert t("sources/2026/06/15/x") == "hot"        # day-sharded, today
    assert t("sources/2026/01/x") == "warm"          # ~5 months -> warm
    assert t("sources/2025/08/x") == "warm"          # ~10 months -> warm
    assert t("sources/2020/01/x") == "cold"          # years -> cold
    # path date wins; frontmatter ignored for from_path sources
    assert t("sources/2020/01/x", {"published": "2026-06-14"}) == "cold"


def test_entities_tier_from_updated():
    assert t("entities/vendor/acme", {"updated": "2026-06-10"}) == "hot"
    assert t("entities/vendor/acme", {"updated": "2026-02-01"}) == "warm"
    assert t("entities/vendor/acme", {"updated": "2024-01-01"}) == "cold"
    assert t("entities/vendor/acme", {}) == "cold"        # no recency signal -> cold


def test_predictions_open_floor_hot_regardless_of_date():
    # open prediction with a far-future resolves_by is still hot (working set)
    assert t("predictions/p1", {"status": "open", "resolves_by": "2030-01-01"}) == "hot"
    assert t("predictions/p1", {"status": "active", "resolves_by": "2019-01-01"}) == "hot"
    # resolved prediction tiers by how long ago it resolved
    assert t("predictions/p2", {"status": "confirmed", "resolves_by": "2026-06-01"}) == "hot"
    assert t("predictions/p2", {"status": "refuted", "resolves_by": "2022-01-01"}) == "cold"


def test_untiered_namespace_returns_none():
    assert t("dashboards/wardley/map") is None
    assert t("briefings/2026/06/x") is None
    assert t("research/sources/2026/06/x") is None   # a sub-domain untiered in this pack


def test_tier_of_file_reads_frontmatter(tmp_path):
    wiki = tmp_path / "wiki"
    p = wiki / "entities" / "vendor" / "acme.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: Acme\nupdated: 2026-06-12\n---\n# Acme\n")
    assert TL.tier_of_file(p, wiki, CFG, TODAY) == "hot"
    p.write_text("---\ntype: entity\nname: Acme\nupdated: 2023-01-01\n---\n# Acme\n")
    assert TL.tier_of_file(p, wiki, CFG, TODAY) == "cold"
