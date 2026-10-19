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


@pytest.mark.external
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


def test_tracked_engine_crons_all_classified():  # invariant-audit #351
    """Every job in the TRACKED source config/engine-crons.json must be classified in cron-tiers.yaml
    (engine / engine-template). test_every_live_job_classified_exactly_once reads the GITIGNORED
    generated cron-plus-jobs.json and pytest.skips on a fresh clone / CI, so an engine cron added to
    engine-crons.json without a cron-tiers.yaml classification shipped UNCAUGHT until a deploy hit the
    fail-loud SystemExit in split(). This runs UNCONDITIONALLY against tracked source, closing the CI
    blindspot."""
    m = _mod()
    engine = json.loads(m.ENGINE_CRONS_FILE.read_text())
    tier_of = m._tier_map(m.TIERS)
    names = [j["name"] for j in engine]
    assert len(names) == len(set(names)), f"duplicate names in engine-crons.json: {names}"
    unclassified = [j["name"] for j in engine
                    if not j.get("extension") and not j.get("pack") and j["name"] not in tier_of]
    assert not unclassified, (
        f"engine-crons.json job(s) with no cron-tiers.yaml classification: {unclassified} — "
        f"add each under config/cron-tiers.yaml engine:/engine-template:")


def test_tracked_model_writers_declare_contract_or_legacy_exemption():
    m = _mod()
    jobs = json.loads(m.ENGINE_CRONS_FILE.read_text())
    assert m.validate_output_contracts(jobs, require_declared=True) == []


def test_prompt_file_round_trip_preserves_references_without_inline_source(tmp_path):
    m = _mod()
    pack = tmp_path / "pack"
    (pack / "crons").mkdir(parents=True)
    (pack / "prompts").mkdir()
    (pack / "prompts" / "daily.md").write_text("External daily prompt\n")
    prompt_path = pack / "crons" / m.DOMAIN_PROMPTS
    prompt_path.write_text(json.dumps({
        "daily": {"prompt_file": "prompts/daily.md"}
    }))
    prompts = m._load_prompts(prompt_path)
    jobs = m.merge([{"name": "daily"}], [], prompts, {"daily": "engine-template"})
    assert jobs[0]["prompt"] == "External daily prompt"
    assert jobs[0]["prompt_file"] == "prompts/daily.md"

    parts = m.split(jobs, {"daily": "engine-template"})
    assert "prompt" not in parts[m.ENGINE_CRONS][0]
    assert "prompt_file" not in parts[m.ENGINE_CRONS][0]
    assert parts[m.DOMAIN_PROMPTS] == {
        "daily": {"prompt_file": "prompts/daily.md"}
    }


def test_engine_prompt_file_split_drops_only_resolved_prompt():
    m = _mod()
    parts = m.split([{
        "name": "repair",
        "prompt_file": "prompts/cron/repair.md",
        "prompt": "resolved bytes",
    }], {"repair": "engine"})
    assert parts[m.ENGINE_CRONS] == [{
        "name": "repair", "prompt_file": "prompts/cron/repair.md"
    }]


def _split_through_sources(m, jobs, tmp_path):
    """split() the live fleet, write each half where regen reads it, and load it back the way
    regen does. A `prompt_file` lane's text lives only in its file (split drops it), so an
    in-memory merge(split(x)) cannot restore it; reading the halves back resolves each file
    against its own root: the engine repo for the engine half, the pack for the pack's halves."""
    parts = m.split(jobs, m._tier_map(m.TIERS))
    pack = tmp_path / "pack"
    (pack / "crons").mkdir(parents=True)
    _seed_pack_prompts(m, jobs, pack)
    m.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    m.ENGINE_CRONS_FILE.write_text(m._dump_list(parts[m.ENGINE_CRONS]))
    (pack / "crons" / m.DOMAIN_CRONS).write_text(m._dump_list(parts[m.DOMAIN_CRONS]))
    (pack / "crons" / m.DOMAIN_PROMPTS).write_text(m._dump_prompts(parts[m.DOMAIN_PROMPTS]))
    return {m.ENGINE_CRONS: m._load_jobs(m.ENGINE_CRONS_FILE),
            m.DOMAIN_CRONS: m._load_jobs(pack / "crons" / m.DOMAIN_CRONS),
            m.DOMAIN_PROMPTS: m._load_prompts(pack / "crons" / m.DOMAIN_PROMPTS),
            m.EXTENSIONS: parts[m.EXTENSIONS]}, parts


@pytest.mark.external
def test_round_trip_is_lossless(tmp_path):
    m = _mod()
    jobs = _live_jobs(m)
    halves, _parts = _split_through_sources(m, jobs, tmp_path)
    merged = m.merge(halves[m.ENGINE_CRONS], halves[m.DOMAIN_CRONS], halves[m.DOMAIN_PROMPTS],
                     extensions=halves[m.EXTENSIONS])
    assert m._canon(jobs) == m._canon(merged), "merge(split(x)) != x — boundary lossy"


@pytest.mark.external
def test_engine_template_prompt_moves_to_pack_and_returns(tmp_path):
    """An engine-template job's prompt is stripped from the engine half and lives
    only in the domain pack, but the merge restores it byte-for-byte."""
    m = _mod()
    jobs = _live_jobs(m)
    tier_of = m._tier_map(m.TIERS)
    et = [j for j in jobs if tier_of.get(j["name"]) == "engine-template" and j.get("prompt")]
    if not et:
        pytest.skip("no engine-template jobs carry a prompt")
    halves, parts = _split_through_sources(m, jobs, tmp_path)
    engine_by_name = {j["name"]: j for j in parts[m.ENGINE_CRONS]}
    for job in et:
        sample = job["name"]
        assert "prompt" not in engine_by_name[sample], f"engine half leaked {sample}'s domain prompt"
        assert sample in parts[m.DOMAIN_PROMPTS], f"{sample}'s prompt did not move to the pack"
    # The live prompt is the DEPLOYED text: the prompt file or inline prompt, composed with any
    # output contract by merge(), bound to its writer (bind_contract_writers) and stamped with the
    # discovery contract when the fleet is written (_dump_jobs). Compare that same pipeline's
    # output, not the stored source, which is pre-composition (okengine#994 D).
    deployed = json.loads(m._dump_jobs(m.bind_contract_writers(m.merge(
        halves[m.ENGINE_CRONS], halves[m.DOMAIN_CRONS], halves[m.DOMAIN_PROMPTS],
        tier_of=tier_of, extensions=halves[m.EXTENSIONS]))))["jobs"]
    by_name = {j["name"]: j for j in deployed}
    for job in et:
        assert job["name"] in by_name, f"{job['name']} did not come back from the pack"
        assert by_name[job["name"]]["prompt"] == job["prompt"], f"{job['name']}: pack prompt mismatch"


@pytest.mark.external
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


def _seed_pack_prompts(m, jobs, pack_root):
    """Copy the generating pack's prompts/ into the scratch pack (okengine#994 D).

    split() keeps a lane's `prompt_file` and drops its resolved text on purpose (the file is the
    source), so the pack's prompts/ is a SOURCE the round trip needs and cannot rebuild from the
    live fleet: the live `prompt` is the composed text, not the file. Without this, a fleet whose
    pack supplies prompt files (an engine-template lane's, or its own domain lanes') raised
    FileNotFoundError at regen even with no drift at all. The pack is CRON_PACK_DIR, the deployment
    that generated config/cron-plus-jobs.json. Only when NO pack is named (CRON_PACK_DIR unset, the
    placeholder) are the inputs unknowable, so only then skip loudly naming them. A named pack
    without prompts/ is the wrong pack or real drift: proceed and let regen fail."""
    if m.PACK_DIR != Path(m.PACK_DIR_PLACEHOLDER):
        source = m.PACK_DIR / "prompts"
        if source.is_dir():
            __import__("shutil").copytree(source, pack_root / "prompts")
        return
    outside_engine = sorted({j["prompt_file"] for j in jobs if isinstance(j.get("prompt_file"), str)
                             and not (m.REPO / j["prompt_file"]).is_file()})
    if outside_engine:
        pytest.skip(f"the live fleet needs pack prompt file(s) {outside_engine} and no pack is "
                    "named: set CRON_PACK_DIR to the deployment that generated "
                    "config/cron-plus-jobs.json to run this live-fleet round trip.")


def _seed_slice2(m, tmp_path):
    """Point the module's file paths at tmp and seed sources from the real jobs."""
    jobs = _live_jobs(m)
    parts = m.split(jobs, m._tier_map(m.TIERS))
    pack = tmp_path / "pack" / "crons"
    pack.mkdir(parents=True)
    _seed_pack_prompts(m, jobs, tmp_path / "pack")
    (tmp_path / "engine-crons.json").write_text(m._dump_list(parts[m.ENGINE_CRONS]))
    (pack / m.DOMAIN_CRONS).write_text(m._dump_list(parts[m.DOMAIN_CRONS]))
    (pack / m.DOMAIN_PROMPTS).write_text(m._dump_prompts(parts[m.DOMAIN_PROMPTS]))
    m.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    m.PACK_DIR = tmp_path / "pack"
    m.JOBS = tmp_path / "cron-plus-jobs.json"
    return jobs


@pytest.mark.external
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
    m.ENGINE_CRONS_FILE.write_text(m._dump_list([{
        "name": "eng-a", "schedule": {"expr": "0 7 * * *"}, "max_iterations": 2, "prompt": "p",
    }]))
    merged = m.regen()                                  # must NOT raise FileNotFoundError
    assert m.JOBS.is_file()
    assert "eng-a" in {j["name"] for j in merged}


@pytest.mark.external
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
    generated_expected = json.loads(m._dump_jobs(jobs))["jobs"]
    ne = lambda js: [j for j in js if not j.get("extension")]   # see test_regen (#141/#143)
    assert m._canon(ne(out)) == m._canon(ne(generated_expected)), "dump->regen lost or mutated jobs"
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


def test_engine_template_object_prompt_composes_output_contract():
    m = _mod()
    base = {
        "api": 1, "allowed_namespaces": ["sources", "entities"],
        "allowed_types": ["source", "entity"], "operations": ["create", "update"],
        "required_fields": ["type"], "required_relationships": [],
        "body": {"required": False, "min_non_whitespace": 0},
        "unknown_fields": "review", "unresolved_links": "review",
        "placeholder_links": "reject", "completion": "run",
    }
    policy = {
        **base, "allowed_namespaces": ["sources"], "allowed_types": ["source"],
        "operations": ["create"], "required_fields": ["type", "raw"],
        "body": {"required": True, "min_non_whitespace": 80},
        "unknown_fields": "reject", "completion": "per-selected-item",
    }
    jobs = m.merge(
        [{"name": "raw-backfill", "output_contract": base}], [],
        {"raw-backfill": {"prompt": "compile sources", "output_contract": policy}},
        tier_of={"raw-backfill": "engine-template"},
    )
    assert jobs[0]["prompt"] == "compile sources"
    assert jobs[0]["output_contract"]["allowed_types"] == ["source"]
    assert jobs[0]["output_contract"]["body"]["min_non_whitespace"] == 80


def test_split_preserves_inline_prompt_output_contract_policy():
    m = _mod()
    value = {
        "name": "daily-brief", "prompt": "write today", "max_iterations": 8,
        "output_contract": {
            "api": 1, "allowed_namespaces": ["briefings"],
            "allowed_types": ["briefing"], "operations": ["create"],
            "required_fields": ["type"], "required_relationships": [],
            "body": {"required": True, "min_non_whitespace": 80},
            "unknown_fields": "review", "unresolved_links": "review",
            "placeholder_links": "reject", "completion": "run",
            "required_write_path": "briefings/daily-{date}.md",
        },
    }
    parts = m.split([value], {"daily-brief": "engine-template"})
    assert parts[m.DOMAIN_PROMPTS]["daily-brief"] == {
        "prompt": "write today", "output_contract": value["output_contract"]}
    merged = m.merge(parts[m.ENGINE_CRONS], [], parts[m.DOMAIN_PROMPTS])
    assert merged[0]["output_contract"] == value["output_contract"]


def test_model_writer_contract_inventory_gate():
    m = _mod()
    jobs = [{"name": "unsafe", "enabled_toolsets": ["okengine-write"]},
            {"name": "legacy", "enabled_toolsets": ["okengine-write"],
             "output_contract_exempt": "migration pending"},
            {"name": "script", "no_agent": True, "enabled_toolsets": ["okengine-write"]}]
    assert m.validate_output_contracts(jobs, require_declared=True) == [
        "job 'unsafe' is model-writing but has no output_contract or explicit exemption"
    ]


def test_generated_job_has_stable_lane_id_and_contract_digest():
    m = _mod()
    contract = {
        "api": 1, "allowed_namespaces": ["sources"], "allowed_types": ["source"],
        "operations": ["create"], "required_fields": ["raw"],
        "required_relationships": [], "body": {"required": True, "min_non_whitespace": 80},
        "unknown_fields": "reject", "unresolved_links": "reject",
        "placeholder_links": "reject", "completion": "per-selected-item",
    }
    first = json.loads(m._dump_jobs([{"name": "compile", "output_contract": contract}]))["jobs"][0]
    second = json.loads(m._dump_jobs([{"name": "compile", "output_contract": contract}]))["jobs"][0]
    assert first["id"] == second["id"]
    assert first["output_contract_digest"] == m.output_contract.digest(contract)
    assert first["env"]["OKENGINE_LANE_ID"] == first["id"]
    assert first["env"]["OKENGINE_CONTRACT_DIGEST"] == first["output_contract_digest"]


def test_generated_model_lanes_forbid_guessed_mcp_prompts_and_resources():
    m = _mod()
    generated = json.loads(m._dump_jobs([
        {"name": "raw-backfill", "prompt": "compile raws", "enabled": True},
        {"name": "source-quality-backfill", "prompt": "score sources", "enabled": True},
        {"name": "deterministic", "prompt": "(unused)", "no_agent": True, "enabled": True},
    ]))["jobs"]
    by_name = {job["name"]: job for job in generated}
    for name in ("raw-backfill", "source-quality-backfill"):
        prompt = by_name[name]["prompt"]
        assert "MCP DISCOVERY CONTRACT:" in prompt
        assert "`list_prompts` or `get_prompt`" in prompt
        assert "pass only a URI returned verbatim" in prompt
        assert "never invent `file://`, `okengine://`" in prompt
    assert "MCP DISCOVERY CONTRACT:" not in by_name["deterministic"]["prompt"]


def test_generated_source_quality_lane_embeds_rubric_for_legacy_pack_prompt():
    m = _mod()
    generated = json.loads(m._dump_jobs([{
        "name": "source-quality-backfill",
        "prompt": "Use the rubric in CLAUDE.md.",
        "enabled": True,
    }]))["jobs"][0]
    assert "SOURCE QUALITY RUBRIC:" in generated["prompt"]
    assert "A=completely reliable" in generated["prompt"]
    assert "6=truth cannot be judged" in generated["prompt"]


def test_generated_source_quality_lane_does_not_duplicate_embedded_rubric():
    m = _mod()
    prompt = "Reliability: A=completely reliable. Credibility: 6=truth cannot be judged."
    generated = json.loads(m._dump_jobs([{
        "name": "source-quality-backfill", "prompt": prompt, "enabled": True,
    }]))["jobs"][0]
    assert "SOURCE QUALITY RUBRIC:" not in generated["prompt"]
    assert generated["prompt"].count("A=completely reliable") == 1


def test_contracted_model_writer_is_lane_bound_not_generic_admin():
    m = _mod()
    contract = {
        "api": 1, "allowed_namespaces": ["sources"], "allowed_types": ["source"],
        "operations": ["create"], "required_fields": ["raw"],
        "required_relationships": [], "body": {"required": True, "min_non_whitespace": 80},
        "unknown_fields": "reject", "unresolved_links": "reject",
        "placeholder_links": "reject", "completion": "per-selected-item",
    }
    jobs = m.merge([{"name": "raw-backfill", "output_contract": contract,
                     "enabled_toolsets": ["file", "okengine-write", "okengine"]}], [], {})
    assert jobs[0]["enabled_toolsets"] == ["file", "okengine-write-raw-backfill", "okengine"]
    assert "okengine-write" not in jobs[0]["enabled_toolsets"]


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


def test_validate_ordering_gates_deployed_enabled_shape():  # invariant-audit #351
    """validate_ordering must gate the DEPLOYED (enabled) shape. A disabled `enabled:false`
    placeholder never reaches jobs.json (_dump_jobs drops it), so an ENABLED lane whose after: names
    a disabled job is a DANGLING dependency in the shipped artifact — fail loud here, not deploy it.
    Before the fix, ordering validated the full pre-filter set (disabled target counted as present)
    while _dump_jobs dropped it — a real gate hole."""
    m = _mod()
    _, errs = m.validate_ordering(
        [{"name": "consumer", "after": ["producer"]},
         {"name": "producer", "enabled": False, "schedule": {}}])
    assert any("no such job" in e for e in errs), errs
    # both enabled -> the same edge is sound
    assert m.validate_ordering(
        [{"name": "consumer", "after": ["producer"]}, {"name": "producer"}])[1] == []


def test_validate_ordering_non_string_after_fails_loud():  # invariant-audit #351
    """A non-string after: entry is a malformed dependency edge; dropping it silently (the old bare
    `continue`) vanished an intended ordering constraint with every gate green. Fail loud."""
    m = _mod()
    _, errs = m.validate_ordering([{"name": "a", "after": [123]}])
    assert any("non-string after" in e for e in errs), errs


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


def test_top_level_expr_schedule_normalized_to_dict():
    """HIGH #5: framework_validate._cron_expr blesses a TOP-LEVEL {expr} shape (no schedule key), so a
    pack cron authored that way validates GREEN — but it must NOT deploy schedule-less, or cron-plus
    computes next_run None, next_run_at stays null, and the job silently never fires. The deploy
    chokepoint must lift the top-level expr into {kind:cron, expr:...}."""
    import json
    m = _mod()
    out = json.loads(m._dump_jobs([{"name": "z", "expr": "0 13 * * *", "prompt": "x", "enabled": True}]))
    job = out["jobs"][0]
    assert job["schedule"] == {"kind": "cron", "expr": "0 13 * * *"}, "top-level expr must be lifted"
    # and it must match what the validator resolves as the usable expr (no divergence)
    import importlib.util
    fv = importlib.util.spec_from_file_location("framework_validate", REPO / "scripts" / "framework_validate.py")
    fvm = importlib.util.module_from_spec(fv); fv.loader.exec_module(fvm)
    assert fvm._cron_expr({"name": "z", "expr": "0 13 * * *"}) == "0 13 * * *"   # validator accepts it
    assert fvm._cron_expr(job) == "0 13 * * *"                                    # deployed shape still resolves


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


@pytest.mark.external
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


def test_prompt_file_resolution_rejects_escape_and_preserves_contract(tmp_path):
    m = _mod()
    pack = tmp_path / "pack"
    crons = pack / "crons"
    prompts = pack / "prompts"
    crons.mkdir(parents=True)
    prompts.mkdir()
    (prompts / "lane.md").write_text("Do the work.\n")

    jobs_path = crons / "jobs.json"
    jobs_path.write_text(json.dumps([{"name": "lane", "prompt_file": "../escape.md"}]))
    with pytest.raises(ValueError, match="escapes source root"):
        m._load_jobs(jobs_path)

    prompts_path = crons / "engine-template-prompts.json"
    prompts_path.write_text(json.dumps({"lane": {"prompt_file": "../escape.md"}}))
    with pytest.raises(ValueError, match="escapes pack root"):
        m._load_prompts(prompts_path)

    contract = {
        "api": 1, "allowed_namespaces": ["entities"], "allowed_types": ["*"],
        "operations": ["update"], "required_fields": ["type"],
        "required_relationships": [], "body": {"required": False, "min_non_whitespace": 0},
        "unknown_fields": "reject", "unresolved_links": "review",
        "placeholder_links": "reject", "completion": "per-selected-item",
    }
    parts = m.split(
        [{"name": "lane", "prompt_file": "prompts/lane.md", "prompt": "Do the work.",
          "output_contract": contract}],
        {"lane": "engine-template"},
    )
    assert parts[m.DOMAIN_PROMPTS]["lane"] == {
        "prompt_file": "prompts/lane.md", "output_contract": contract,
    }
    merged, errors = m.merge_packs(
        [{"name": "lane", "output_contract": contract}],
        [{"name": "pack", "domain": [], "prompts": {
            "lane": {"prompt": "Do the work.", "prompt_file": "prompts/lane.md"}
        }}],
        tier_of={"lane": "engine-template"},
    )
    assert errors == []
    assert merged[0]["prompt_file"] == "prompts/lane.md"


def test_source_normalize_runs_after_the_reshard_that_strands_refs():
    """Ordering is the point of this lane's schedule, and nothing else enforces it.

    `reshard-oversized` re-files pages as buckets grow -- and back again as they shrink -- which
    strands every stored reference pointing into the old shard. Measured on a live vault: 77
    assessments carried a `subject:` that no longer resolved, all two-letter -> one-letter
    collapses, and the judgments read as "Review not run" while sitting `active` on disk.

    Repairing pointers BEFORE the move that strands them fixes nothing and leaves a full day of
    drift. This asserts the repair runs later in the day than the reshard, so a future schedule edit
    cannot quietly invert them.
    """
    import json
    from pathlib import Path
    jobs = json.loads((Path(__file__).resolve().parents[2] / "config/engine-crons.json").read_text())
    by_name = {j["name"]: j for j in jobs}

    def minute_of_day(name):
        expr = by_name[name]["schedule"]["expr"].split()
        minute, hour = expr[0], expr[1]
        assert minute.isdigit() and hour.isdigit(), f"{name}: expected a fixed daily time, got {expr}"
        return int(hour) * 60 + int(minute)

    assert minute_of_day("source-normalize") > minute_of_day("reshard-oversized"), (
        "source-normalize must run AFTER reshard-oversized -- repairing stranded refs before the "
        "reshard that strands them leaves the vault broken until the next day")


def test_source_normalize_is_a_bounded_no_agent_lane():
    """It walks the whole corpus, so it must be deterministic and bounded. A lane with no timeout
    that wedges holds its slot forever -- this one previously blocked in uninterruptible I/O for
    days on a live gateway, and a `timeout` cannot kill a process in D state, so the bound is a
    floor on the damage rather than a guarantee."""
    import json
    from pathlib import Path
    jobs = json.loads((Path(__file__).resolve().parents[2] / "config/engine-crons.json").read_text())
    job = next(j for j in jobs if j["name"] == "source-normalize")
    assert job["no_agent"] is True, "deterministic repair — never a model lane"
    assert job.get("prompt"), "even a no_agent lane states what it does, for the operator reading jobs.json"
    assert isinstance(job.get("timeout"), int) and job["timeout"] > 0, "must declare a bound"
    assert job["script"].endswith("source_normalize.py")
    assert job["enabled"] is True


def test_wiki_health_declares_deterministic_artifact_reporting():
    m = _mod()
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    job = next(value for value in jobs if value["name"] == "wiki-health-audit")
    assert job["no_agent"] is True
    assert job["artifact_contract"] == {"api": 1, "min_artifacts": 1}
    assert m.validate_artifact_contracts(jobs) == []


def test_artifact_contract_schema_fails_loudly():
    m = _mod()
    errors = m.validate_artifact_contracts([{
        "name": "broken", "artifact_contract": {"api": 2, "min_artifacts": -1,
                                                   "surprise": True},
    }])
    assert any("unknown key" in error for error in errors)
    assert any("api must be 1" in error for error in errors)
    assert any("non-negative integer" in error for error in errors)
    assert any("only valid for no_agent" in error for error in errors)

    truthy_non_boolean = m.validate_artifact_contracts([{
        "name": "ambiguous", "no_agent": 1,
        "artifact_contract": {"api": 1, "min_artifacts": 0},
    }])
    assert truthy_non_boolean == [
        "job 'ambiguous' artifact_contract is only valid for no_agent jobs"
    ]


# --- okengine#814: _canon compares the jobs' SOURCE. A `prompt` resolved from a `prompt_file` is
# derived — split drops it deliberately and deploy re-resolves it — so comparing it reports the
# boundary lossy when nothing was lost. Exercised directly here rather than through the live fleet
# file, which is gitignored: relying on it is why this went unnoticed in CI and only failed on a
# host that happened to have one.


def test_canon_ignores_a_prompt_resolved_from_a_prompt_file():
    m = _mod()
    with_text = {"name": "drain", "prompt_file": "prompts/cron/drain.md", "prompt": "resolved"}
    without = {"name": "drain", "prompt_file": "prompts/cron/drain.md"}
    assert m._canon([with_text]) == m._canon([without])


def test_canon_still_compares_a_prompt_that_has_no_prompt_file():
    """An inline prompt IS the source, so losing it must still be caught."""
    m = _mod()
    inline = {"name": "lane", "prompt": "inline text"}
    dropped = {"name": "lane"}
    assert m._canon([inline]) != m._canon([dropped])


def test_canon_still_compares_every_other_field_of_a_prompt_file_job():
    m = _mod()
    a = {"name": "drain", "prompt_file": "p.md", "prompt": "x", "schedule": {"expr": "0 1 * * *"}}
    b = {"name": "drain", "prompt_file": "p.md", "prompt": "y", "schedule": {"expr": "0 2 * * *"}}
    assert m._canon([a]) != m._canon([b]), "only the derived prompt is exempt, nothing else"


# ---------------------------------------------------------------------------
# per-lane write-server name length (okengine#840)
# ---------------------------------------------------------------------------
def _dispatched_name(server: str, tool: str) -> str:
    """A faithful local model of Hermes `tools/mcp_tool_schema.mcp_prefixed_tool_name`.

    Reimplemented rather than imported: Hermes is a pinned EXTERNAL dependency and is not on
    the path in CI. Keeping the algorithm here means the gate's limit is pinned to the
    ALGORITHM, not to a magic number someone can quietly widen.
    """
    import hashlib
    import re as _re
    san = lambda v: _re.sub(r"[^A-Za-z0-9_]", "_", str(v or ""))
    full = f"mcp__{san(server)}__{san(tool)}"
    if len(full) <= 64:
        return full
    suffix = "_" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
    return full[:64 - len(suffix)] + suffix


def test_the_writer_name_limit_is_exactly_where_the_separator_survives():
    """The gate's constant must BE the clamp boundary, not a number near it.

    `parse_okengine_write_result` splits the dispatched name on `__` and identifies the write
    server from the second component. Once the clamp truncates past the server/tool separator
    the name collapses to two components and the write is no longer attributed -- so the
    largest safe server name is the longest one whose `__` still survives truncation.
    """
    m = _mod()
    limit = m.CONTRACT_WRITER_NAME_MAX
    shortest_tool = "patch_entity"   # 12 chars; the least-pressure case

    def server_of(name: str) -> str | None:
        parts = _dispatched_name(name, shortest_tool).split("__", 2)
        return parts[1] if len(parts) >= 3 else None

    at_limit = "w" * limit
    assert server_of(at_limit) == at_limit, (
        f"a {limit}-char server name does not survive dispatch; the gate's limit is too high")

    over = "w" * (limit + 1)
    assert server_of(over) != over, (
        f"a {limit + 1}-char server name still survives; the gate's limit is needlessly strict")


def test_an_over_length_lane_is_rejected(monkeypatch):
    """The negative fixture: the gate must reject the defect it claims to prevent.

    contract_writer_name now bounds every name, so a real lane can no longer produce the defect
    through the derivation. Reverting the derivation to its pre-#840 form is exactly the
    regression this branch of the gate exists to catch, so that is the fixture.
    """
    m = _mod()
    name = "okpack-market-intel-weekly-capability-frontier-digest"   # a real live lane
    assert m.validate_contract_writer_names(
        [{"name": name, "output_contract": {"api": 1}}]) == [], "bounded form must pass"
    monkeypatch.setattr(m, "contract_writer_name", m.unbounded_writer_name)
    errors = m.validate_contract_writer_names([{"name": name, "output_contract": {"api": 1}}])
    assert len(errors) == 1, errors
    assert name in errors[0] and "840" in errors[0], errors[0]


def test_a_lane_at_the_limit_passes():
    """49 is still the line for a writer a pack names explicitly -- the derivation aims at 36."""
    m = _mod()
    at_limit = "okengine-write-" + "a" * (m.CONTRACT_WRITER_NAME_MAX - len("okengine-write-"))
    over = at_limit + "a"
    assert len(at_limit) == m.CONTRACT_WRITER_NAME_MAX
    assert m.validate_contract_writer_names([{"name": "x", "enabled_toolsets": [at_limit]}]) == []
    assert m.validate_contract_writer_names([{"name": "x", "enabled_toolsets": [over]}])

@pytest.mark.parametrize("job,why", [
    ({"name": "x" * 90, "no_agent": True, "output_contract": {"api": 1}},
     "no_agent lanes never reach a per-lane write server"),
    ({"name": "x" * 90},
     "a lane with no output_contract is not routed to a contract writer"),
])
def test_lanes_that_never_get_a_contract_writer_are_exempt(job, why):
    m = _mod()
    assert m.validate_contract_writer_names([job]) == [], why


def test_the_engine_half_is_clean_under_its_own_gate():
    """The engine ships no lane that would lose its own writes."""
    m = _mod()
    engine = json.loads((REPO / "config" / "engine-crons.json").read_text())
    engine = engine if isinstance(engine, list) else engine.get("jobs", [])
    assert m.validate_contract_writer_names(engine) == []


# ---------------------------------------------------------------------------
# bounded writer names (okengine#840 root fix)
# ---------------------------------------------------------------------------
def _write_tools() -> list[str]:
    """Every tool write_server.py registers, read from its source rather than restated."""
    import ast
    tree = ast.parse((REPO / "okengine-mcp" / "write_server.py").read_text(encoding="utf-8"))
    tools = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                fn = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(fn, ast.Attribute) and fn.attr == "tool":
                    tools.append(node.name)
    assert tools, "no @mcp.tool registrations found -- write_server.py changed shape"
    return tools


def test_the_write_tool_budget_covers_every_registered_tool():
    """WRITER_NAME_TARGET is sized to the longest write tool. A longer tool added later would
    clamp on every bounded lane, silently, so it must fail here first -- and raising the budget
    shortens the target, which renames every bounded writer on the next deploy."""
    m = _mod()
    longest = max(_write_tools(), key=len)
    assert len(longest) <= m._LONGEST_WRITE_TOOL, (
        f"write tool {longest!r} ({len(longest)} chars) exceeds the budget of "
        f"{m._LONGEST_WRITE_TOOL}; raise _LONGEST_WRITE_TOOL knowing it renames bounded writers")


LONG_LANES = [
    "okengine.predictions:prediction-falsification-search",
    "okengine.predictions:candidate-watch",
    "okpack-market-intel-weekly-capability-frontier-digest",
    "okpack-threat-landscape-weekly-brief",
]


@pytest.mark.parametrize("name", LONG_LANES)
def test_a_long_lane_gets_a_writer_name_that_survives_dispatch(name):
    """The derivation itself now keeps every writer inside the separator-survival limit."""
    m = _mod()
    writer = m.contract_writer_name(name)
    assert len(writer) <= m.WRITER_NAME_TARGET, (writer, len(writer))
    assert writer.startswith("okengine-write-"), "prefix matchers downstream key on this"
    for tool in _write_tools():
        literal = f"mcp__{writer.replace('-', '_')}__{tool}"
        assert _dispatched_name(writer, tool) == literal, (
            f"{tool} clamps on {writer}: a prompt naming {literal} would name a tool the model "
            f"never sees")


def test_a_short_lane_keeps_its_existing_writer_name():
    """Bounding must not rename a server that already works -- that would churn live config
    for lanes that were never affected."""
    m = _mod()
    assert m.contract_writer_name("okengine.lacuna") == "okengine-write-okengine-lacuna"
    at_limit = "a" * (m.WRITER_NAME_TARGET - len("okengine-write-"))
    assert m.contract_writer_name(at_limit) == f"okengine-write-{at_limit}"


def test_bounded_names_are_deterministic_and_distinct():
    """Two lanes sharing a long common prefix must not collapse onto one write identity."""
    m = _mod()
    a = "okengine.predictions:prediction-structural-backfill"
    b = "okengine.predictions:prediction-structural-backfill-v2"
    assert m.contract_writer_name(a) == m.contract_writer_name(a)
    assert m.contract_writer_name(a) != m.contract_writer_name(b)


def test_two_lanes_bound_to_one_writer_are_rejected():
    """A writer name is an identity: one server carries one OKENGINE_WRITE_ACTOR. Two lanes that
    derive the same name would silently write as each other."""
    m = _mod()
    jobs = [{"name": "a.b", "output_contract": {"api": 1}},
            {"name": "a-b", "output_contract": {"api": 1}}]      # both slug to "a-b"
    errors = m.validate_contract_writer_names(jobs)
    assert any("collide" in e for e in errors), errors


def test_an_explicit_over_length_writer_toolset_is_rejected():
    """A pack can name a writer directly in enabled_toolsets, bypassing the derivation."""
    m = _mod()
    long_writer = "okengine-write-" + "x" * 60
    errors = m.validate_contract_writer_names(
        [{"name": "short", "enabled_toolsets": [long_writer]}])
    assert len(errors) == 1 and long_writer in errors[0], errors


def test_the_fleet_derives_no_over_length_writer():
    """Every contracted lane on this host's live fleet now passes -- the bound, not a rename."""
    m = _mod()
    # the live artifact predates the bound; regen re-binds it, so validate what regen produces
    assert m.validate_contract_writer_names(m.bind_contract_writers(_live_jobs(m))) == []


def test_rebinding_a_pre_bound_artifact_leaves_one_writer_per_lane():
    """A dumped pre-#840 artifact names the old writer explicitly; rebinding must REPLACE it."""
    m = _mod()
    name = "okengine.predictions:candidate-watch"
    stale = m.unbounded_writer_name(name)
    job = {"name": name, "output_contract": {"api": 1},
           "enabled_toolsets": ["file_read", stale, "okengine"]}
    (bound,) = m.bind_contract_writers([job])
    writers = [t for t in bound["enabled_toolsets"] if t.startswith("okengine-write")]
    assert writers == [m.contract_writer_name(name)], writers
    assert bound["enabled_toolsets"][0] == "file_read", "toolset order must be preserved"


def test_a_job_naming_an_unregistered_writer_is_reported():
    """The stage-only deploy hazard: regen renamed the writer, ensure-runtime has not run."""
    m = _mod()
    jobs = [{"name": "lane-a", "enabled_toolsets": ["file_read", "okengine-write-lane-a"]},
            {"name": "lane-b", "enabled_toolsets": ["okengine-write-lane-a"]}]
    errors = m.unregistered_writers(jobs, {"mcp_servers": {"okengine-write": {}}})
    assert len(errors) == 1, errors
    assert "okengine-write-lane-a" in errors[0] and "['lane-a', 'lane-b']" in errors[0], errors[0]


def test_a_non_dict_job_entry_is_ignored_by_the_registration_check():
    """A malformed entry is the shape validators' verdict; it must not crash this one."""
    m = _mod()
    assert m.unregistered_writers(["not-a-job", None, 7],
                                  {"mcp_servers": {"okengine-write": {}}}) == []


def test_registered_writers_and_non_writer_toolsets_pass():
    m = _mod()
    jobs = [{"name": "a", "enabled_toolsets": ["okengine-write", "okengine-write-a", "okengine",
                                               "file_read"]}]
    assert m.unregistered_writers(
        jobs, {"mcp_servers": {"okengine-write": {}, "okengine-write-a": {}}}) == []


@pytest.mark.parametrize("config", [{}, {"mcp_servers": None}, {"mcp_servers": ["a"]}, None])
def test_a_config_without_a_server_map_registers_nothing(config):
    """No readable mcp_servers map means no writer is registered -- never a silent pass."""
    m = _mod()
    assert m.unregistered_writers([{"name": "a", "enabled_toolsets": ["okengine-write"]}], config)


def _promptfile_pack(m, tmp_path, prompt_text="Write the brief.\n"):
    """An engine half with one lane, and a pack whose domain lane takes its prompt from a file."""
    m.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    m.PACK_DIR = tmp_path / "pack"
    m.JOBS = tmp_path / "cron-plus-jobs.json"
    (m.PACK_DIR / "crons").mkdir(parents=True)
    (m.PACK_DIR / "prompts").mkdir()
    (m.PACK_DIR / "pack.yaml").write_text("name: pack\n")
    (m.PACK_DIR / "prompts" / "brief.md").write_text(prompt_text)
    m.ENGINE_CRONS_FILE.write_text(m._dump_list([{
        "name": "eng-a", "schedule": {"expr": "0 7 * * *"}, "max_iterations": 2, "prompt": "p",
    }]))
    (m.PACK_DIR / "crons" / m.DOMAIN_CRONS).write_text(json.dumps([{
        "id": "b1", "name": "pack-brief", "schedule": {"expr": "5 20 * * 0"}, "max_iterations": 4,
        "prompt_file": "prompts/brief.md",
    }]))


def test_regen_resolves_a_domain_lanes_prompt_file(tmp_path):
    """A pack domain lane that names a prompt_file must deploy WITH that prompt. Single-pack regen
    read domain crons with a bare json.loads, so the file never resolved and four model lanes on
    one deployment ran for a week on their selector output alone (the composed path, via
    discover_packs, always resolved it)."""
    m = _mod()
    _promptfile_pack(m, tmp_path)
    merged = {j["name"]: j for j in m.regen()}
    assert merged["pack-brief"]["prompt"] == "Write the brief."
    deployed = {j["name"]: j for j in json.loads(m.JOBS.read_text())["jobs"]}
    assert deployed["pack-brief"]["prompt"].startswith("Write the brief.\n")   # + engine stamps


def test_regen_refuses_a_model_lane_whose_prompt_is_empty(tmp_path):
    m = _mod()
    _promptfile_pack(m, tmp_path, prompt_text="   \n")
    with pytest.raises(SystemExit) as raised:
        m.regen()
    assert "'pack-brief': model lane has an empty prompt" in str(raised.value)
    assert "prompt_file 'prompts/brief.md' did not resolve" in str(raised.value)
    assert not m.JOBS.exists(), "a refused regen must write nothing"


def test_validate_agent_prompts_scope():
    m = _mod()
    jobs = [{"name": "ok", "prompt": "do it"},
            {"name": "script", "no_agent": True},
            {"name": "off", "enabled": False},
            {"name": "blank", "prompt": "  "},
            {"name": "bare"}]
    assert m.validate_agent_prompts(jobs) == [
        "job 'blank': model lane has an empty prompt (no prompt or prompt_file); "
        "it would run on its selector output alone",
        "job 'bare': model lane has an empty prompt (no prompt or prompt_file); "
        "it would run on its selector output alone",
    ]


# --- okengine#994 D: the live-fleet round trip seeds the generating pack's prompts/ -------------
# The live-fleet tests above skip in CI (no config/cron-plus-jobs.json). These rebuild the deploy-
# checkout case from a synthetic deployment, so the seeding is exercised everywhere.

def _synthetic_deployment(m, tmp_path):
    """A deployment that supplies an engine-template lane's prompt as a FILE (the house shape),
    regenerated into a fleet file the way a roll's deploy.sh leaves one in the checkout."""
    src = tmp_path / "deployment"
    (src / "crons").mkdir(parents=True)
    (src / "prompts").mkdir()
    (src / "pack.yaml").write_text("name: okpack-synthetic\n")
    (src / "prompts" / "raw-backfill.md").write_text("Backfill this pack's raw captures.\n")
    (src / "crons" / m.DOMAIN_PROMPTS).write_text(json.dumps(
        {"raw-backfill": {"prompt_file": "prompts/raw-backfill.md"}}))
    m.PACK_DIR = src
    m.JOBS = tmp_path / "checkout-config" / "cron-plus-jobs.json"
    m.JOBS.parent.mkdir()
    m.regen()
    return src


def test_a_round_trip_seeds_the_generating_packs_prompt_files(tmp_path):
    m = _mod()
    _synthetic_deployment(m, tmp_path)
    live = m.JOBS
    jobs = _seed_slice2(m, tmp_path / "scratch")
    assert (tmp_path / "scratch" / "pack" / "prompts" / "raw-backfill.md").is_file()
    raw = next(j for j in jobs if j["name"] == "raw-backfill")
    assert raw["prompt_file"] == "prompts/raw-backfill.md"
    assert raw["prompt"].startswith("Backfill this pack's raw captures.")
    m.dump_from_live(str(live))             # raised FileNotFoundError before the seeding
    out = json.loads(m.JOBS.read_text())["jobs"]
    ne = lambda js: [j for j in js if not j.get("extension")]
    assert m._canon(ne(out)) == m._canon(ne(jobs))


def test_the_round_trip_still_fails_when_the_pack_lost_a_prompt_file(tmp_path):
    """Strictness: seeding copies the pack's prompts/, it does not invent them. A fleet whose
    prompt file is gone from the named pack is real drift, and the round trip must fail."""
    m = _mod()
    src = _synthetic_deployment(m, tmp_path)
    (src / "prompts" / "raw-backfill.md").unlink()
    (src / "prompts" / "unrelated.md").write_text("x\n")
    live = m.JOBS
    _seed_slice2(m, tmp_path / "scratch")
    with pytest.raises(FileNotFoundError, match="raw-backfill.md"):
        m.dump_from_live(str(live))


def test_a_fleet_needing_pack_prompts_with_no_pack_named_skips_loudly(tmp_path):
    m = _mod()
    src = _synthetic_deployment(m, tmp_path)
    m.PACK_DIR = Path(m.PACK_DIR_PLACEHOLDER)
    with pytest.raises(pytest.skip.Exception, match=r"prompts/raw-backfill\.md.*CRON_PACK_DIR"):
        _seed_slice2(m, tmp_path / "scratch")
    assert src.is_dir()


def test_a_fleet_whose_prompt_files_are_all_engine_owned_needs_no_pack(tmp_path):
    m = _mod()
    m.PACK_DIR = tmp_path / "no-such-pack"
    jobs = [{"name": "orphans-drain", "prompt_file": "prompts/cron/orphans-drain.md"},
            {"name": "inline", "prompt": "p"}]
    assert (m.REPO / "prompts/cron/orphans-drain.md").is_file()
    _seed_pack_prompts(m, jobs, tmp_path / "pack")      # neither copies nor skips
    assert not (tmp_path / "pack").exists()


def test_a_named_pack_without_prompts_fails_the_round_trip_rather_than_skipping(tmp_path):
    """A NAMED pack with no prompts/ is the wrong pack or real drift: the round trip must run and
    fail at regen. Only an unset CRON_PACK_DIR (the placeholder) makes the inputs unknowable."""
    m = _mod()
    _synthetic_deployment(m, tmp_path)
    live = m.JOBS
    wrong = tmp_path / "other-deployment"
    wrong.mkdir()
    (wrong / "pack.yaml").write_text("name: okpack-other\n")
    m.PACK_DIR = wrong
    _seed_slice2(m, tmp_path / "scratch")              # must not skip
    with pytest.raises(FileNotFoundError, match="raw-backfill.md"):
        m.dump_from_live(str(live))


def test_runs_is_the_single_definition_of_the_deployed_set():
    """okengine#1029: `runs` is the filter the deployed artifact and ensure-runtime's lane writers
    share. Enabled is the default; only an explicit disable keeps a lane out of the deployment."""
    m = _mod()
    assert m.runs({"name": "a"}) is True
    assert m.runs({"name": "a", "enabled": True}) is True
    assert m.runs({"name": "a", "enabled": False}) is False
    dumped = json.loads(m._dump_jobs([{"name": "b", "enabled": False}, {"name": "a"}]))["jobs"]
    assert [j["name"] for j in dumped] == ["a"]


# ---------------------------------------------------------------------------
# okengine#1044 [8]/[18]: a lane's `write_tools` is validated, never silently widened
# ---------------------------------------------------------------------------
# `write_tools` narrows a lane's server-bound writer (okengine#664). A string, an empty list, a list
# of blanks or a misspelled tool used to collapse to "no narrowing" (the full tool surface) or to
# an allowlist that matches nothing (zero tools), and every gate agreed because each side derived
# from the same unchecked value.

def test_the_known_write_tools_are_exactly_what_write_server_registers():
    """The allowlist a lane may name is read against write_server.py's own @mcp.tool registrations,
    so a tool added or renamed there fails here instead of making a valid lane unvalidatable."""
    assert _mod().WRITE_TOOL_NAMES == frozenset(_write_tools())


@pytest.mark.parametrize("tools", [
    ["update_entity"],
    ["create_entity", " append_to_section "],
    ["record_machine_review", "converge_source", "converge_concept"],
])
def test_a_well_formed_write_tools_declaration_is_accepted(tools):
    m = _mod()
    job = {"name": "okpack-alpha-lane", "write_tools": tools}
    assert m.validate_write_tools([job, {"name": "okpack-alpha-free"}]) == []
    assert m.write_tools_declaration(job) == (",".join(t.strip() for t in tools), None)


def test_an_absent_write_tools_means_no_narrowing():
    assert _mod().write_tools_declaration({"name": "l"}) == (None, None)


@pytest.mark.parametrize("value,needle", [
    ("update_entity", "must be a list"),
    (None, "must be a list"),
    ({"update_entity": True}, "must be a list"),
    ([], "names no tool"),
    ([" ", ""], "names no tool"),
    (["update_entity", 7], "not a tool name"),
    (["update_entiy"], "unknown write tool 'update_entiy'"),
    (["update_entity", "link_pages"], "unknown write tool 'link_pages'"),
    (["zz_typo", "update_entity", "aa_typo"], "unknown write tool 'aa_typo', 'zz_typo' in"),
])
def test_a_malformed_write_tools_declaration_is_a_fleet_error(value, needle):
    m = _mod()
    job = {"name": "okpack-alpha-lane", "write_tools": value}
    tools, error = m.write_tools_declaration(job)
    assert tools is None and needle in error, error
    errors = dict(m.fleet_errors([job]))["cron write-tools errors"]
    assert len(errors) == 1 and "okpack-alpha-lane" in errors[0] and needle in errors[0], errors
