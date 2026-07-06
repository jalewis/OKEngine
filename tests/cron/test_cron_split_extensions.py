"""cron_pack_split is extension-tier aware — okengine#141.

regen() folds extension jobs (`<id>` / `<id>:<op>`) into the deployed fleet, but split()/
dump used to classify only engine/engine-template/domain via cron-tiers.yaml and crash on
anything else as 'unclassified'. Extension jobs now route to their own partition (by the
composer's `extension` marker, or a namespaced name on legacy artifacts) and merge re-adds
them losslessly. Module-gated only (no live artifact needed), so this runs in CI.
"""
import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron_pack_split.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="cron_pack_split absent")


def _mod():
    spec = importlib.util.spec_from_file_location("cron_pack_split", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _engine_cron_name(m):
    return next(n for n, t in m._tier_map(m.TIERS).items() if t == "engine")


def test_marked_and_namespaced_jobs_partition_to_extensions():
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    eng = _engine_cron_name(m)
    jobs = [
        {"name": eng, "schedule": {"kind": "cron", "expr": "0 * * * *"}},
        {"name": "okengine.predictions:grade", "extension": "okengine.predictions",
         "schedule": {"kind": "cron", "expr": "23 6 * * *"}},                # marker
        {"name": "okengine.contradictions",
         "schedule": {"kind": "cron", "expr": "0 4 * * *"}},                 # dotted, no marker
    ]
    parts = m.split(jobs, tier_of)
    assert [j["name"] for j in parts[m.EXTENSIONS]] == \
        ["okengine.predictions:grade", "okengine.contradictions"]
    assert [j["name"] for j in parts[m.ENGINE_CRONS]] == [eng]


def test_split_merge_round_trips_with_extensions():
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    eng = _engine_cron_name(m)
    jobs = [
        {"name": eng, "schedule": {"kind": "cron", "expr": "0 * * * *"}},
        {"name": "okengine.predictions:grade", "extension": "okengine.predictions",
         "schedule": {"kind": "cron", "expr": "23 6 * * *"}, "no_agent": False},
    ]
    parts = m.split(jobs, tier_of)
    merged = m.merge(parts[m.ENGINE_CRONS], parts[m.DOMAIN_CRONS], parts[m.DOMAIN_PROMPTS],
                     extensions=parts[m.EXTENSIONS])
    assert m._canon(merged) == m._canon(jobs)        # nothing lost, nothing crashed


def test_non_extension_unclassified_job_still_fails_loud():
    """The fail-loud safety stays for a genuinely unclassified non-extension job (no
    marker, no namespace chars) — that's a real misconfiguration, not an extension."""
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    with pytest.raises(SystemExit):
        m.split([{"name": "totally-unknown-cron",
                  "schedule": {"kind": "cron", "expr": "0 0 * * *"}}], tier_of)


def test_merge_without_extensions_is_backcompat():
    m = _mod()
    eng = {"name": _engine_cron_name(m), "schedule": {"kind": "cron", "expr": "0 * * * *"}}
    assert m.merge([eng], [], {}) == [eng]            # extensions param optional


# --- okengine#143: pack-domain provenance marker ---------------------------

def test_pack_marked_job_routes_to_domain():
    """cron-tiers `domain:` is empty by design, so a pack's own crons route to the domain
    partition by their `pack:` marker (#143) — split no longer crashes on them."""
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    eng = _engine_cron_name(m)
    jobs = [
        {"name": eng, "schedule": {"kind": "cron", "expr": "0 * * * *"}},
        {"name": "okpack-x-feed-fetch", "pack": "okpack-x",
         "schedule": {"kind": "cron", "expr": "*/30 * * * *"}},
    ]
    parts = m.split(jobs, tier_of)
    assert [j["name"] for j in parts[m.DOMAIN_CRONS]] == ["okpack-x-feed-fetch"]
    assert [j["name"] for j in parts[m.ENGINE_CRONS]] == [eng]
    merged = m.merge(parts[m.ENGINE_CRONS], parts[m.DOMAIN_CRONS], parts[m.DOMAIN_PROMPTS],
                     extensions=parts[m.EXTENSIONS])
    assert m._canon(merged) == m._canon(jobs)        # lossless


def test_merge_packs_stamps_the_pack_marker():
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    packs = [{"name": "okpack-x", "prompts": {},
              "domain": [{"name": "feed-fetch", "schedule": {"kind": "cron", "expr": "0 * * * *"}}]}]
    jobs, errors = m.merge_packs([], packs, tier_of)
    assert not errors, errors
    dj = next(j for j in jobs if j["name"] == "okpack-x:feed-fetch")
    assert dj["pack"] == "okpack-x"                  # marker stamped for split-back


def test_pack_and_extension_markers_coexist():
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    jobs = [
        {"name": "okpack-x-feed", "pack": "okpack-x", "schedule": {"kind": "cron", "expr": "0 * * * *"}},
        {"name": "okengine.predictions:grade", "extension": "okengine.predictions",
         "schedule": {"kind": "cron", "expr": "0 6 * * *"}},
    ]
    parts = m.split(jobs, tier_of)
    assert [j["name"] for j in parts[m.DOMAIN_CRONS]] == ["okpack-x-feed"]
    assert [j["name"] for j in parts[m.EXTENSIONS]] == ["okengine.predictions:grade"]
