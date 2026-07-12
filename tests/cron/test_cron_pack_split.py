"""Regression tests for the engine/domain-pack cron generator (cron_pack_split).

Guards the engine↔domain-pack boundary (docs/engine-domain-boundary.md): the
deployed cron-plus-jobs.json must round-trip losslessly through split→merge, and
every live job must be classified in config/cron-tiers.yaml exactly once.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
MOD_PATH = REPO / "scripts" / "cron_pack_split.py"

# NB: NO module-level skip on config/cron-plus-jobs.json (invariant-audit B7.7). That file is a
# gitignored DEPLOYMENT artifact — absent on a fresh clone / public CI — and gating the WHOLE module
# on it silently skipped the generator-LOGIC tests (regen/split/merge/validators) too, which are
# self-contained (they seed a pack in tmp_path) and MUST run in CI. Only the handful of live-FLEET
# snapshot assertions genuinely need the artifact; they skip LOUDLY + individually via _live_jobs().


def _mod():
    spec = importlib.util.spec_from_file_location("cron_pack_split", MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split"] = m
    spec.loader.exec_module(m)
    return m


def _live_jobs(m):
    """Load THIS host's generated fleet file, or skip LOUDLY. These assertions pin the live deployed
    cron-plus-jobs.json (gitignored, absent on a fresh clone / CI) — unlike the generator-logic tests
    which build their own pack in tmp_path and always run."""
    if not m.JOBS.is_file():
        pytest.skip("config/cron-plus-jobs.json absent (no local deployment) — live-fleet assertion "
                    "skipped; the generator-logic tests still ran. Regenerate via a deploy or "
                    "`python scripts/cron_pack_split.py` against a pack to exercise these too.")
    return m._load_jobs(m.JOBS)


def test_every_live_job_classified_exactly_once():
    m = _mod()
    jobs = _live_jobs(m)
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
    jobs = _live_jobs(m)
    tier_of = m._tier_map(m.TIERS)
    parts = m.split(jobs, tier_of)
    merged = m.merge(parts[m.ENGINE_CRONS], parts[m.DOMAIN_CRONS], parts[m.DOMAIN_PROMPTS],
                     extensions=parts[m.EXTENSIONS])
    assert m._canon(jobs) == m._canon(merged), "merge(split(x)) != x — boundary lossy"


def test_engine_template_prompt_moves_to_pack_and_returns():
    """An engine-template job's prompt is stripped from the engine half and lives
    only in the domain pack, but the merge restores it byte-for-byte."""
    m = _mod()
    jobs = _live_jobs(m)
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
    jobs = _live_jobs(m)
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


def test_every_deployed_job_gets_an_id():  # invariant-audit CRITICAL
    """An id-less cron crashes the pinned scheduler's eager `job.get("name", job["id"])` heal on the
    first tick and stalls the WHOLE fleet forever. No deployed job may lack an id — mint a stable one
    from the name at the _dump_jobs chokepoint; an existing id is preserved."""
    m = _mod()
    import json
    dumped = json.loads(m._dump_jobs([
        {"name": "pack:no-id-lane", "schedule": {"kind": "cron", "expr": "0 4 * * *"}},   # no id
        {"name": "has-id-lane", "id": "abc123def456", "schedule": {"kind": "cron", "expr": "0 5 * * *"}},
    ]))
    by_name = {j["name"]: j for j in dumped["jobs"]}
    assert by_name["pack:no-id-lane"]["id"] and len(by_name["pack:no-id-lane"]["id"]) == 12
    assert by_name["has-id-lane"]["id"] == "abc123def456"          # existing id untouched
    assert m._ensure_id({"name": "pack:no-id-lane"})["id"] == by_name["pack:no-id-lane"]["id"]  # stable


def test_sanitize_unpauses_a_runtime_paused_job():  # invariant-audit HIGH
    """A budget-guard/manual PAUSE lives in jobs.json as {enabled:false, paused_at:...}. `dump`
    captures source truth, and regen drops enabled:false jobs — so capturing a pause would silently
    remove cost-bearing crons from the next deploy. sanitize must un-pause (drop the markers, restore
    enabled:true); a genuine source-level enabled:false (no paused_at) is left alone."""
    m = _mod()
    paused, shipped_off = m.sanitize([
        {"name": "raw-backfill", "enabled": False, "paused_at": "2026-07-10T00:00:00Z",
         "paused_reason": "over budget", "schedule": {}},
        {"name": "disabled-placeholder", "enabled": False, "schedule": {}},   # intentional, no paused_at
    ])
    assert paused["enabled"] is True and not (set(paused) & m.PAUSE_MARKERS)
    assert shipped_off["enabled"] is False                          # source disable preserved


def _seed_slice2(m, tmp_path):
    """Point the module's file paths at tmp and seed sources from the real jobs."""
    jobs = _live_jobs(m)
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


def test_regen_tolerates_pack_without_crons_dir(tmp_path):  # invariant-audit #12
    """A pack with ONLY engine crons has no crons/ dir. regen must still run (empty domain set) so
    the deploy always regenerates THIS pack's set instead of skipping regen and shipping a stale
    leftover — which, on a multi-pack host, could be a DIFFERENT pack's job set."""
    m = _mod()
    m.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    m.PACK_DIR = tmp_path / "engineonly-pack"          # deliberately NO crons/ subdir
    m.JOBS = tmp_path / "cron-plus-jobs.json"
    m.PACK_DIR.mkdir()
    (m.PACK_DIR / "pack.yaml").write_text("name: engineonly-pack\n")
    m.ENGINE_CRONS_FILE.write_text(m._dump_list([{"name": "eng-a", "schedule": {"expr": "0 7 * * *"}}]))
    merged = m.regen()                                  # must NOT raise FileNotFoundError
    assert m.JOBS.is_file()
    assert "eng-a" in {j["name"] for j in merged}


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


def test_bare_string_schedule_normalized_to_dict():
    """HIGH #1: a documented bare-string schedule must be normalized to {kind:cron, expr:...} in the
    deployed jobs.json — else it reaches cron-plus verbatim and crashes every tick, stalling the fleet."""
    import json
    m = _mod()
    out = json.loads(m._dump_jobs([{"name": "z", "schedule": "0 13 * * SUN", "prompt": "x", "enabled": True}]))
    assert out["jobs"][0]["schedule"] == {"kind": "cron", "expr": "0 13 * * SUN"}   # dict, not bare string
    # a dict schedule is passed through unchanged
    out2 = json.loads(m._dump_jobs([{"name": "a", "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                                     "prompt": "y", "enabled": True}]))
    assert out2["jobs"][0]["schedule"] == {"kind": "cron", "expr": "0 9 * * *"}


def test_validate_unique_ids_flags_collisions():  # invariant-audit M37
    """cron-plus keys by id (minted from name); a dup id/name means one job runs the other's def and
    the twin never fires. The single-pack merge + extension pass had no gate, and _by_name/
    validate_ordering both collapse a dup name into one dict entry (blind to it)."""
    m = _mod()
    # duplicate explicit id
    errs = m.validate_unique_ids([{"name": "a", "id": "x1"}, {"name": "b", "id": "x1"}])
    assert any("x1" in e for e in errs), errs
    # duplicate name (mints a colliding id)
    assert m.validate_unique_ids([{"name": "dup"}, {"name": "dup"}]), "dup name must error"
    # disabled placeholders never deploy -> not counted
    assert m.validate_unique_ids([{"name": "c", "id": "y"},
                                  {"name": "d", "id": "y", "enabled": False}]) == []
    # a clean fleet passes
    assert m.validate_unique_ids([{"name": "a", "id": "1"}, {"name": "b", "id": "2"}]) == []


def test_current_fleet_has_no_id_collisions():  # invariant-audit M37 (regression on real data)
    m = _mod()
    assert m.validate_unique_ids(_live_jobs(m)) == [], "the deployed fleet has a colliding id/name"


def test_dump_restores_source_sentinels_and_profiles(tmp_path, monkeypatch):  # invariant-audit M20
    """dump-from-live reads the DEPLOYED jobs.json (schedules + models already EXPANDED by the deploy
    transform). Writing the resolved per-install values back to SHARED source would bake one install's
    jitter minute / endpoint for everyone — the sentinel + @profile representation must be restored
    from the current source for jobs that still exist there."""
    m = _mod()
    src = tmp_path / "engine-crons.json"
    src.write_text(json.dumps([
        {"name": "canonical-assemble", "schedule": {"kind": "cron", "expr": "@jitter:weekly"}},
        {"name": "brief", "schedule": {"kind": "cron", "expr": "@morning:30"}},
        {"name": "reason-lane", "model": "@reasoning"},
    ]))
    monkeypatch.setattr(m, "ENGINE_CRONS_FILE", src)
    monkeypatch.setattr(m, "PACK_DIR", tmp_path / "nopack")     # no domain-crons file -> skipped cleanly
    live = [
        {"name": "canonical-assemble", "schedule": {"kind": "cron", "expr": "10 13 * * 1"}},   # jitter-resolved
        {"name": "brief", "schedule": {"kind": "cron", "expr": "30 7 * * *"}},                 # morning-resolved
        {"name": "reason-lane", "model": "qwen3.5:27b", "provider": "custom",                  # profile-baked
         "base_url": "http://h:1/v1", "ollama_num_ctx": 65536},
        {"name": "new-live-job", "schedule": {"kind": "cron", "expr": "0 5 * * *"}},           # not in source
    ]
    out = {j["name"]: j for j in m._restore_source_reprs(live)}
    assert out["canonical-assemble"]["schedule"]["expr"] == "@jitter:weekly", "jitter sentinel restored"
    assert out["brief"]["schedule"]["expr"] == "@morning:30", "morning sentinel restored"
    assert out["reason-lane"]["model"] == "@reasoning", "@profile ref restored"
    assert not ({"provider", "base_url", "ollama_num_ctx"} & set(out["reason-lane"])), "baked endpoint dropped"
    assert out["new-live-job"]["schedule"]["expr"] == "0 5 * * *", "a genuinely-new live job stays verbatim"


def test_merge_packs_rewrites_intra_pack_after_targets():  # invariant-audit batch-3 re-verify (ordering)
    """merge_packs prefixes domain job names to <pack>:<job>; an intra-pack `after:` target names a
    SIBLING domain job that was ALSO prefixed, so it must be rewritten too — else the dependency
    dangles at the bare pre-prefix name and validate_ordering rejects/misroutes it. An engine target
    stays bare."""
    m = _mod()
    engine = [{"name": "engine-lane", "enabled": True},
              {"name": "entity-backfill", "enabled": True}]        # an engine-template lane the pack drives
    tier_of = {"engine-lane": "engine", "entity-backfill": "engine-template"}
    packs = [{"name": "acme",
              "prompts": {"entity-backfill": "do the backfill"},   # -> entity-backfill@acme
              "domain": [
                  {"name": "jobB", "enabled": True},
                  # after: a sibling domain job, a driven engine-TEMPLATE lane, AND a pure-engine lane
                  {"name": "jobA", "enabled": True,
                   "after": ["jobB", "entity-backfill", "engine-lane"]}]}]
    out, errors = m.merge_packs(engine, packs, tier_of=tier_of)
    assert errors == [], errors
    ja = next(j for j in out if j["name"] == "acme:jobA")
    # sibling domain -> <pack>:, driven engine-template -> <job>@<pack>, pure-engine -> bare
    assert ja["after"] == ["acme:jobB", "entity-backfill@acme", "engine-lane"], ja["after"]
    # the composed fleet's ordering is now sound (no dangling after) — the whole point
    assert m.validate_ordering(out)[1] == [], "rewritten after graph must validate"


def test_restore_source_reprs_all_shapes_rename_and_profile_fields(tmp_path, monkeypatch):  # M20 re-verify
    """_restore_source_reprs must: (a) restore a sentinel in the TOP-LEVEL `expr` shape (not just the
    dict shape), (b) match a RENAMED live job by id, (c) preserve a PROFILE field the source set
    independently of the @ref while dropping the deploy-baked ones."""
    m = _mod()
    src = tmp_path / "engine-crons.json"
    src.write_text(json.dumps([
        {"name": "toplevel-lane", "id": "t1", "expr": "@jitter:2h"},                 # top-level expr shape
        {"name": "renamed-lane", "id": "r1", "schedule": {"kind": "cron", "expr": "@jitter:weekly"}},
        {"name": "profile-lane", "id": "p1", "model": "@reasoning", "ollama_num_ctx": 8192},  # source-set field
    ]))
    monkeypatch.setattr(m, "ENGINE_CRONS_FILE", src)
    monkeypatch.setattr(m, "PACK_DIR", tmp_path / "nopack")
    live = [
        {"name": "toplevel-lane", "id": "t1", "expr": "55 */2 * * *"},               # jitter-resolved top-level
        {"name": "renamed-in-scheduler", "id": "r1", "schedule": {"kind": "cron", "expr": "10 13 * * 1"}},
        {"name": "profile-lane", "id": "p1", "model": "qwen3.5:27b", "provider": "custom",
         "base_url": "http://h:1/v1", "ollama_num_ctx": 65536},
    ]
    out = {j["id"]: j for j in m._restore_source_reprs(live)}
    assert out["t1"]["expr"] == "@jitter:2h", "top-level-expr sentinel must be restored"
    assert "schedule" not in out["t1"], "must keep source's top-level shape, not inject a schedule dict"
    assert out["r1"]["schedule"]["expr"] == "@jitter:weekly", "renamed job matched by id + restored"
    assert out["p1"]["model"] == "@reasoning"
    assert out["p1"]["ollama_num_ctx"] == 8192, "source-set profile field preserved (source's value)"
    assert not ({"provider", "base_url"} & set(out["p1"])), "deploy-baked endpoint fields dropped"


def test_merge_packs_rejects_domain_name_shadowing_engine_lane():  # batch-3 re-verify (low)
    """A domain job whose bare name shadows an engine (or driven engine-template) lane makes an
    intra-pack after: target ambiguous — it would silently rebind to the domain twin. Fail loud."""
    m = _mod()
    engine = [{"name": "reshelve", "enabled": True}]
    packs = [{"name": "acme", "domain": [{"name": "reshelve", "enabled": True}]}]   # shadows the engine lane
    _, errors = m.merge_packs(engine, packs, tier_of={"reshelve": "engine"})
    assert any("shadows" in e and "reshelve" in e for e in errors), errors


def test_pack_marked_dotted_name_routes_to_domain_not_extensions(tmp_path):  # invariant-audit #24
    """A pack domain cron whose name contains '.'/':' carries a `pack:` provenance marker and must
    route to DOMAIN, not the EXTENSIONS partition (which dump_from_live silently erases — it writes
    only engine/domain/prompts)."""
    m = _mod()
    tier_of = {"plain-feed": "domain"}
    jobs = [
        {"name": "acme.fetch", "pack": "okpack-x", "schedule": {"expr": "0 6 * * *"}},
        {"name": "okpack-x:feed-fetch", "pack": "okpack-x", "schedule": {"expr": "0 7 * * *"}},
        {"name": "plain-feed", "pack": "okpack-x", "schedule": {"expr": "0 8 * * *"}},
        {"name": "some.extension:op", "extension": "okengine.viz", "schedule": {"expr": "0 9 * * *"}},
    ]
    parts = m.split(jobs, tier_of)
    domain_names = {j["name"] for j in parts[m.DOMAIN_CRONS]}
    ext_names = {j["name"] for j in parts[m.EXTENSIONS]}
    assert domain_names == {"acme.fetch", "okpack-x:feed-fetch", "plain-feed"}
    assert ext_names == {"some.extension:op"}      # only the real extension-marked job
