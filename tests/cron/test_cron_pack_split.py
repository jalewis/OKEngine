"""Regression tests for the engine/domain-pack cron generator (cron_pack_split).

Guards the engine↔domain-pack boundary (docs/engine-domain-boundary.md): the
deployed cron-plus-jobs.json must round-trip losslessly through split→merge, and
every live job must be classified in config/cron-tiers.yaml exactly once.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
MOD_PATH = REPO / "scripts" / "cron_pack_split.py"

pytestmark = pytest.mark.skipif(
    not (MOD_PATH.is_file() and (REPO / "config" / "cron-plus-jobs.json").is_file()),
    reason="cron_pack_split.py or cron-plus-jobs.json not present",
)


def _mod():
    spec = importlib.util.spec_from_file_location("cron_pack_split", MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split"] = m
    spec.loader.exec_module(m)
    return m


def test_every_live_job_classified_exactly_once():
    m = _mod()
    jobs = m._load_jobs(m.JOBS)
    tier_of = m._tier_map(m.TIERS)
    names = [j["name"] for j in jobs]
    assert len(names) == len(set(names)), "duplicate job names in cron-plus-jobs.json"
    # extension/pack jobs carry a provenance marker (#141/#143) and are legitimately not in
    # cron-tiers — only engine/engine-template/domain crons must be classified there.
    unmarked = [j["name"] for j in jobs if not j.get("extension") and not j.get("pack")]
    unclassified = [n for n in unmarked if n not in tier_of]
    assert not unclassified, f"unmarked jobs missing from cron-tiers.yaml: {unclassified}"


def test_round_trip_is_lossless():
    m = _mod()
    jobs = m._load_jobs(m.JOBS)
    tier_of = m._tier_map(m.TIERS)
    parts = m.split(jobs, tier_of)
    merged = m.merge(parts[m.ENGINE_CRONS], parts[m.DOMAIN_CRONS], parts[m.DOMAIN_PROMPTS],
                     extensions=parts[m.EXTENSIONS])
    assert m._canon(jobs) == m._canon(merged), "merge(split(x)) != x — boundary lossy"


def test_engine_template_prompt_moves_to_pack_and_returns():
    """An engine-template job's prompt is stripped from the engine half and lives
    only in the domain pack, but the merge restores it byte-for-byte."""
    m = _mod()
    jobs = m._load_jobs(m.JOBS)
    tier_of = m._tier_map(m.TIERS)
    et = [j for j in jobs if tier_of.get(j["name"]) == "engine-template" and j.get("prompt")]
    if not et:
        pytest.skip("no engine-template jobs carry a prompt")
    parts = m.split(jobs, tier_of)
    engine_by_name = {j["name"]: j for j in parts[m.ENGINE_CRONS]}
    sample = et[0]["name"]
    assert "prompt" not in engine_by_name[sample], "engine half leaked a domain prompt"
    assert parts[m.DOMAIN_PROMPTS][sample] == et[0]["prompt"], "pack prompt mismatch"


def test_split_partitions_counts():
    m = _mod()
    jobs = m._load_jobs(m.JOBS)
    tier_of = m._tier_map(m.TIERS)
    parts = m.split(jobs, tier_of)
    n_tmpl = sum(1 for t in tier_of.values() if t == "engine-template")
    n_pack = sum(1 for j in jobs if j.get("pack"))
    n_ext = sum(1 for j in jobs if m._is_extension_job(j, tier_of))
    # partition conservation: every job lands in exactly one bucket (no crash, no loss).
    assert len(parts[m.ENGINE_CRONS]) + len(parts[m.DOMAIN_CRONS]) + len(parts[m.EXTENSIONS]) \
        == len(jobs)
    assert len(parts[m.DOMAIN_CRONS]) == n_pack             # cron-tiers `domain:` is [] by design
    assert len(parts[m.EXTENSIONS]) == n_ext                # extension-marked / namespaced
    assert len(parts[m.DOMAIN_PROMPTS]) <= n_tmpl           # only engine-template jobs w/ a prompt


def test_sanitize_strips_runtime_fields():
    m = _mod()
    dirty = [{"name": "x", "schedule": {}, "next_run_at": "t", "last_run_at": "t",
              "last_run_success": True, "last_error": None, "last_delivery_error": None,
              "repeat": {"times": 1, "completed": 1}}]
    clean = m.sanitize(dirty)[0]
    assert not (set(clean) & m.RUNTIME_FIELDS)
    assert "completed" not in clean["repeat"]


def _seed_slice2(m, tmp_path):
    """Point the module's file paths at tmp and seed sources from the real jobs."""
    jobs = m._load_jobs(m.JOBS)
    parts = m.split(jobs, m._tier_map(m.TIERS))
    pack = tmp_path / "pack" / "crons"
    pack.mkdir(parents=True)
    (tmp_path / "engine-crons.json").write_text(m._dump_list(parts[m.ENGINE_CRONS]))
    (pack / m.DOMAIN_CRONS).write_text(m._dump_list(parts[m.DOMAIN_CRONS]))
    (pack / m.DOMAIN_PROMPTS).write_text(m._dump_prompts(parts[m.DOMAIN_PROMPTS]))
    m.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    m.PACK_DIR = tmp_path / "pack"
    m.JOBS = tmp_path / "cron-plus-jobs.json"
    return jobs


def test_regen_reproduces_jobs(tmp_path):
    m = _mod()
    jobs = _seed_slice2(m, tmp_path)
    merged = m.regen()
    # extension jobs regenerate from the enabled-state (discovery), which _seed_slice2 does
    # not recreate, so compare the engine+pack jobs (the split-able sources). #141/#143
    ne = lambda js: [j for j in js if not j.get("extension")]
    assert m._canon(ne(merged)) == m._canon(ne(jobs)), "regen(sources) != original jobs"
    # idempotent: second regen is byte-identical
    first = m.JOBS.read_text()
    m.regen()
    assert m.JOBS.read_text() == first


def test_dump_from_live_round_trips_through_sources(tmp_path):
    m = _mod()
    jobs = _seed_slice2(m, tmp_path)
    # simulate live state: add runtime fields the scheduler would have written
    live = {"jobs": [{**j, "next_run_at": "2026-01-01T00:00:00Z",
                      "last_run_success": True} for j in jobs]}
    livefile = tmp_path / "live.json"
    livefile.write_text(__import__("json").dumps(live))
    m.dump_from_live(str(livefile))
    out = m._load_jobs(m.JOBS)
    ne = lambda js: [j for j in js if not j.get("extension")]   # see test_regen (#141/#143)
    assert m._canon(ne(out)) == m._canon(ne(jobs)), "dump->regen lost or mutated jobs"
    assert all(not (set(j) & m.RUNTIME_FIELDS) for j in out), "runtime fields leaked"


def test_engine_template_opt_in_skips_unprompted():
    """Multi-pack: a pack opts into a shared engine-template job by supplying its
    prompt. With tier_of, an engine-template stub the pack didn't prompt is SKIPPED
    (else it ships enabled+promptless=broken). Pure-engine jobs always ship.
    Legacy 3-arg merge stays back-compatible (ships everything)."""
    m = _mod()
    engine = [
        {"name": "always-engine", "schedule": "x"},   # tier engine -> always ships
        {"name": "tmpl-a", "schedule": "x"},          # engine-template
        {"name": "tmpl-b", "schedule": "x"},          # engine-template
    ]
    tier_of = {"always-engine": "engine", "tmpl-a": "engine-template",
               "tmpl-b": "engine-template"}
    prompts = {"tmpl-a": "do A"}                       # pack opts into A only
    merged = m.merge(engine, [], prompts, tier_of=tier_of)
    assert {j["name"] for j in merged} == {"always-engine", "tmpl-a"}
    assert next(j for j in merged if j["name"] == "tmpl-a")["prompt"] == "do A"
    # back-compat: no tier_of ships every engine cron
    assert len(m.merge(engine, [], prompts)) == 3


def test_round_trip_preserves_extension_jobs_synthetic():
    """Regression (#152): the round-trip must carry the EXTENSIONS partition through merge.
    Without it, deploy-folded extension jobs are silently dropped — which is exactly what the
    CLI `check`/`merge` path did (it called merge() without `extensions=`). Synthetic so it holds
    regardless of whether the live cron-plus-jobs.json happens to carry extensions."""
    m = _mod()
    tier_of = m._tier_map(m.TIERS)
    eng_name = next(n for n, t in tier_of.items() if t == "engine")
    jobs = [
        {"name": eng_name, "no_agent": True, "schedule": {"kind": "cron", "expr": "0 1 * * *"}},
        {"name": "mypack-digest", "pack": "mypack", "schedule": {"kind": "cron", "expr": "0 2 * * *"}},
        {"name": "okengine.lacuna", "extension": "okengine.lacuna",
         "schedule": {"kind": "cron", "expr": "0 3 * * *"}},
    ]
    parts = m.split(jobs, tier_of)
    assert len(parts[m.EXTENSIONS]) == 1
    lossless = m.merge(parts[m.ENGINE_CRONS], parts[m.DOMAIN_CRONS], parts[m.DOMAIN_PROMPTS],
                       extensions=parts[m.EXTENSIONS])
    assert m._canon(lossless) == m._canon(jobs)
    # control: dropping the EXTENSIONS partition loses the extension job (the #152 bug)
    lossy = m.merge(parts[m.ENGINE_CRONS], parts[m.DOMAIN_CRONS], parts[m.DOMAIN_PROMPTS])
    assert m._canon(lossy) != m._canon(jobs)
    assert not any(j.get("extension") for j in lossy)


def test_validate_ordering_topo_cycle_missing_self():
    """Regression (#129): the after: dependency gate — topo order on a valid DAG, fail-loud on a
    cycle, a missing target, or a self-reference."""
    m = _mod()
    order, errs = m.validate_ordering(
        [{"name": "a"}, {"name": "b", "after": ["a"]}, {"name": "c", "after": ["b", "a"]}])
    assert not errs
    assert order.index("a") < order.index("b") < order.index("c")
    _, e_cyc = m.validate_ordering([{"name": "x", "after": ["y"]}, {"name": "y", "after": ["x"]}])
    assert any("cyclic" in e for e in e_cyc), e_cyc
    _, e_miss = m.validate_ordering([{"name": "a", "after": ["ghost"]}])
    assert any("no such job" in e for e in e_miss), e_miss
    _, e_self = m.validate_ordering([{"name": "a", "after": ["a"]}])
    assert any("itself" in e for e in e_self), e_self


def test_missing_lane_scripts_catches_unstaged(tmp_path):
    import cron_pack_split
    (tmp_path / "select_pdb_brief.py").write_text("#")
    (tmp_path / "okengine.lacuna").mkdir()
    jobs = [
        {"name": "pdb", "script": "select_pdb_brief.py"},                                    # staged engine lane
        {"name": "lacuna", "script": "/opt/data/scripts/okengine.lacuna/select_lacuna_field.py"},  # UNSTAGED ext
        {"name": "brief", "script": ""},                                                     # no script
    ]
    assert cron_pack_split.missing_lane_scripts(jobs, str(tmp_path)) == [
        ("lacuna", "/opt/data/scripts/okengine.lacuna/select_lacuna_field.py")]
    (tmp_path / "okengine.lacuna" / "select_lacuna_field.py").write_text("#")               # stage it
    assert cron_pack_split.missing_lane_scripts(jobs, str(tmp_path)) == []
