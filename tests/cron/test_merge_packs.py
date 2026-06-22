"""P3 regression: N-way cron composition — per-pack engine-template instances,
pack-prefixed domain jobs, fail-loud on bad prompts."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron_pack_split.py"


def _load():
    spec = importlib.util.spec_from_file_location("cron_pack_split", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split"] = m
    spec.loader.exec_module(m)
    return m


cps = _load()

ENGINE = [{"name": "reshelve", "script": "reshelve.py"},
          {"name": "entity-backfill", "script": "select_entity_candidates.py"}]
TIER_OF = {"reshelve": "engine", "entity-backfill": "engine-template"}


def test_two_packs_compose_without_collision():
    packs = [
        {"name": "packA", "domain": [{"name": "digest", "schedule": "0 9 * * *"}],
         "prompts": {"entity-backfill": "promptA"}},
        {"name": "packB", "domain": [{"name": "digest", "schedule": "0 10 * * *"}],
         "prompts": {"entity-backfill": "promptB"}},
    ]
    jobs, errors = cps.merge_packs(ENGINE, packs, TIER_OF)
    assert errors == []
    names = {j["name"] for j in jobs}
    assert "reshelve" in names                                  # pure engine ships
    assert "entity-backfill" not in names                       # the bare stub does not
    assert {"entity-backfill@packA", "entity-backfill@packB"} <= names   # per-pack instances
    assert {"packA:digest", "packB:digest"} <= names            # pack-prefixed, no collision
    # the instances carry the pack's prompt + the engine def
    inst = next(j for j in jobs if j["name"] == "entity-backfill@packA")
    assert inst["prompt"] == "promptA" and inst["script"] == "select_entity_candidates.py"


def test_prompt_for_non_engine_template_is_error():
    packs = [{"name": "p", "domain": [], "prompts": {"reshelve": "x"}}]  # reshelve is pure engine
    jobs, errors = cps.merge_packs(ENGINE, packs, TIER_OF)
    assert any("reshelve" in e and "not an" in e for e in errors)
    packs = [{"name": "p", "domain": [], "prompts": {"nonesuch": "x"}}]
    _, errors = cps.merge_packs(ENGINE, packs, TIER_OF)
    assert any("nonesuch" in e for e in errors)


def test_single_pack_legacy_merge_unaffected():
    # the legacy single-pack merge still works (no prefixing) for back-compat
    merged = cps.merge(ENGINE, [{"name": "d1"}], {"entity-backfill": "p"}, tier_of=TIER_OF)
    names = [j["name"] for j in merged]
    assert "reshelve" in names and "entity-backfill" in names and "d1" in names
