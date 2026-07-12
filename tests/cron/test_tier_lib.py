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


def test_entities_tier_falls_back_to_envelope_date():
    """okengine#116: entities carry the OKF envelope `last_updated` (the agent never sets a
    domain `updated`), so `date_field: updated` must fall back to last_updated/created — else
    a freshly-written entity tiers cold and vanishes from the hot set / dashboard."""
    assert t("entities/vendor/acme", {"last_updated": "2026-06-10"}) == "hot"
    assert t("entities/vendor/acme", {"created": "2026-06-12"}) == "hot"
    # an explicit configured field still wins when present (no regression for packs that set it)
    assert t("entities/vendor/acme", {"updated": "2024-01-01", "last_updated": "2026-06-10"}) == "cold"


def test_predictions_open_floor_hot_regardless_of_date():
    # open prediction with a far-future resolves_by is still hot (working set)
    assert t("predictions/p1", {"status": "open", "resolves_by": "2030-01-01"}) == "hot"
    assert t("predictions/p1", {"status": "active", "resolves_by": "2019-01-01"}) == "hot"
    # resolved prediction tiers by how long ago it resolved
    assert t("predictions/p2", {"status": "confirmed", "resolves_by": "2026-06-01"}) == "hot"
    assert t("predictions/p2", {"status": "refuted", "resolves_by": "2022-01-01"}) == "cold"


def test_untiered_namespace_returns_none():
    assert t("dashboards/wardley/map") is None
    assert t("operational/metrics") is None          # operational is not a tiered namespace
    assert t("research/notes/x") is None             # a sub-domain namespace untiered in this pack
    # briefings/findings/trends ARE tiered in the engine core (base-schema, 7 namespaces) — the
    # _DEFAULT_TIER fallback now mirrors it, so they are no longer spuriously untiered (invariant-audit #51)
    assert t("briefings/2026/06/x", {"published": "2026-06-10"}) == "hot"


def test_tier_of_file_reads_frontmatter(tmp_path):
    wiki = tmp_path / "wiki"
    p = wiki / "entities" / "vendor" / "acme.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: entity\nname: Acme\nupdated: 2026-06-12\n---\n# Acme\n")
    assert TL.tier_of_file(p, wiki, CFG, TODAY) == "hot"
    p.write_text("---\ntype: entity\nname: Acme\nupdated: 2023-01-01\n---\n# Acme\n")
    assert TL.tier_of_file(p, wiki, CFG, TODAY) == "cold"


def test_load_cfg_uses_the_shared_merged_schema_composer(tmp_path):
    """invariant-audit #51: load_cfg must read the COMPOSED tier (base-schema ⊕ pack) via
    schema_lib.merged_schema — the same composer select_daily_brief / pred_lib / the write path use —
    so a pack that omits `tier:` inherits the engine core instead of a divergent hardcoded default."""
    import os
    import sys as _sys
    _sys.path.insert(0, str(REPO / "scripts" / "cron"))
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    import schema_lib
    (tmp_path / "schema.yaml").write_text(
        "tier:\n  namespaces:\n    predictions:\n      open_values: [open, active, custom]\n")
    cfg = TL.load_cfg(tmp_path)
    composed = schema_lib.merged_schema(tmp_path).get("tier")
    assert cfg == composed                                        # identical to the shared composer
    assert "custom" in cfg["namespaces"]["predictions"]["open_values"]   # pack value composed on core
