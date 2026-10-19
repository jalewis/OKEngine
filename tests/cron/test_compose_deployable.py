"""okengine#968: the N-way compose path produces the same deployable job store regen does.

`regen_composed` and the `compose` CLI used to skip the extension pass and writer binding and kept
their own shorter validator lists. So every composed engine-template instance named the generic,
actor-less (admin) writer, bypassing the lane fence and capability checks; the writer placeholder
stayed unexpanded; and two packs driving one lane collided on the inherited engine id while
sharing one selector state and selection manifest. Now a lane has exactly one driver (model C,
as install-domain already enforces), the instance keeps the engine id, and both entry points run
regen's own finalize step.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron_pack_split.py"
PLACEHOLDER = "mcp__OKENGINE_WRITER__"

# Pack A drives three engine-template lanes, pack B two others: disjoint drivers (model C).
DRIVES = {
    "okpack-a": ("entity-backfill", "page-quality-enrich", "source-quality-backfill"),
    "okpack-b": ("concept-backfill", "raw-backfill"),
}


def _load(name: str = "cps_968"):
    spec = importlib.util.spec_from_file_location(name, MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _mkpack(root: Path, name: str, owns: str, lanes, domain=None) -> Path:
    pack = root / name
    (pack / "crons").mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        f"name: {name}\nversion: 0.1.0\ntrust: public\nowns: {owns}\n")
    (pack / "crons" / "domain-crons.json").write_text(json.dumps(domain or []))
    (pack / "crons" / "engine-template-prompts.json").write_text(
        json.dumps({lane: f"Run {lane} for {name}." for lane in lanes}))
    return pack


@pytest.fixture
def packs(tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    _mkpack(root, "okpack-a", "{types: [alpha], namespaces: [alphas]}", DRIVES["okpack-a"])
    _mkpack(root, "okpack-b", "{types: [beta], namespaces: [betas]}", DRIVES["okpack-b"])
    host = tmp_path / "host"
    host.mkdir()
    return root, host


def _compose(cps, root: Path, host: Path, out: Path) -> list[dict]:
    cps.JOBS, cps.PACK_DIR = out, host     # the host deployment, as regen reads it
    return cps.regen_composed(root)


def _stored(path: Path) -> list[dict]:
    return json.loads(path.read_text())["jobs"]


def _writers(job: dict) -> list[str]:
    return [t for t in job.get("enabled_toolsets") or []
            if t == "okengine-write" or str(t).startswith("okengine-write-")]


def _composed(jobs):
    return [job for job in jobs if "@" in str(job.get("name"))]


# --- one driver per lane -----------------------------------------------------------------------


def test_two_packs_driving_one_lane_is_a_loud_error_naming_both(tmp_path):
    cps = _load()
    root = tmp_path / "packs"
    root.mkdir()
    _mkpack(root, "okpack-a", "{types: [alpha]}", ["entity-backfill"])
    _mkpack(root, "okpack-b", "{types: [beta]}", ["entity-backfill"])
    _, errors = cps.compose(root)
    clash = [e for e in errors if "entity-backfill" in e and "driven by both" in e]
    assert clash, errors
    assert "'okpack-a'" in clash[0] and "'okpack-b'" in clash[0]
    cps.JOBS, cps.PACK_DIR = tmp_path / "jobs.json", tmp_path
    with pytest.raises(SystemExit, match="driven by both"):
        cps.regen_composed(root)
    assert not cps.JOBS.exists(), "a rejected composition must write nothing"


def test_the_same_pack_repeating_a_lane_is_still_a_name_collision_not_a_driver_clash():
    cps = _load()
    packs = [{"name": "a", "prompts": {"tmpl": "x"}, "domain": []},
             {"name": "a", "prompts": {"tmpl": "y"}, "domain": []}]
    _, errors = cps.merge_packs([{"name": "tmpl"}], packs, {"tmpl": "engine-template"})
    assert errors == ["job-id collision: tmpl@a"]


def test_a_composed_instance_keeps_the_engine_jobs_id(packs, tmp_path):
    cps = _load()
    engine = {j["name"]: j for j in cps._load_jobs(cps.ENGINE_CRONS_FILE)}
    jobs = _compose(cps, *packs, tmp_path / "jobs.json")
    for job in _composed(jobs):
        lane = cps.template_lane(job["name"])
        assert job["id"] == engine[lane]["id"], job["name"]
    ids = [job["id"] for job in _stored(tmp_path / "jobs.json")]
    assert len(ids) == len(set(ids)), "no two jobs in the composed store may share an id"


# --- the writer each composed lane runs as -----------------------------------------------------


def test_every_composed_lane_names_its_own_actor_bound_writer(packs, tmp_path):
    cps = _load()
    jobs = _compose(cps, *packs, tmp_path / "jobs.json")
    composed = _composed(jobs)
    assert sorted(j["name"] for j in composed) == sorted(
        f"{lane}@{pack}" for pack, lanes in DRIVES.items() for lane in lanes)
    for job in composed:
        writers = _writers(job)
        assert "okengine-write" not in writers, f"{job['name']} names the generic admin writer"
        assert cps.contract_writer_name(job["name"]) in writers, job["name"]


def test_no_writer_placeholder_survives_in_the_composed_store(packs, tmp_path):
    cps = _load()
    _compose(cps, *packs, tmp_path / "jobs.json")
    text = (tmp_path / "jobs.json").read_text()
    assert PLACEHOLDER not in text


def test_a_pure_engine_lanes_writer_is_unchanged_on_a_composed_host(packs, tmp_path):
    """Composition renames only the lanes a pack drives; an engine lane binds exactly as under
    regen, so the composed host gives it the same writer server."""
    cps = _load()
    tier_of = cps._tier_map(cps.TIERS)
    jobs = {j["name"]: j for j in _compose(cps, *packs, tmp_path / "jobs.json")}
    engine = cps._load_jobs(cps.ENGINE_CRONS_FILE)
    alone = {j["name"]: j for j in cps.bind_contract_writers(
        [dict(j) for j in engine if tier_of.get(j["name"]) == "engine"])}
    contracted = [n for n, j in alone.items()
                  if isinstance(j.get("output_contract"), dict) and not j.get("no_agent")]
    assert contracted, "the engine ships contracted model lanes; the comparison would be vacuous"
    for name in contracted:
        assert _writers(jobs[name]) == _writers(alone[name]), name


def test_extension_jobs_are_composed_in(packs, tmp_path):
    cps = _load()
    expected, errors = cps._extension_pass(packs[1], [])
    assert errors == [] and expected, "core extensions resolve for an empty host"
    names = {j["name"] for j in _compose(cps, *packs, tmp_path / "jobs.json")}
    assert {j["name"] for j in expected} <= names


def test_composed_store_passes_every_gate_regen_runs(packs, tmp_path):
    cps = _load()
    jobs = _compose(cps, *packs, tmp_path / "jobs.json")
    assert [(label, errs) for label, errs in cps.fleet_errors(jobs) if errs] == []


def test_ids_and_output_are_stable_across_two_regens(packs, tmp_path):
    cps = _load()
    _compose(cps, *packs, tmp_path / "one.json")
    _compose(cps, *packs, tmp_path / "two.json")
    assert (tmp_path / "one.json").read_bytes() == (tmp_path / "two.json").read_bytes()


def test_the_compose_cli_and_regen_composed_write_the_same_store(packs, tmp_path, monkeypatch):
    cps = _load()
    root, host = packs
    _compose(cps, root, host, tmp_path / "direct.json")
    cps.JOBS = tmp_path / "cli.json"
    assert cps.main(["compose", "--packs", str(root)]) == 0
    assert (tmp_path / "cli.json").read_bytes() == (tmp_path / "direct.json").read_bytes()


# --- selection manifests -----------------------------------------------------------------------


def test_no_two_composed_lanes_share_a_selection_manifest(packs, tmp_path):
    cps = _load()
    _compose(cps, *packs, tmp_path / "jobs.json")
    manifests = [cps.selection_manifest(j) for j in _stored(tmp_path / "jobs.json")]
    manifests = [m for m in manifests if m]
    assert manifests, "the composed store has selector lanes; the guard would be vacuous"
    assert len(manifests) == len(set(manifests))


def test_a_shared_selection_manifest_fails_the_fleet_gate():
    cps = _load()
    shared = "/opt/data/cron-plus/selections/entity-backfill.json"
    jobs = [{"name": "one", "selection_manifest": shared},
            {"name": "two", "selection_manifest": shared},
            {"name": "three", "selection_manifest": "/elsewhere.json"}]
    assert cps.validate_unique_selection_manifests(jobs) == [
        f"jobs 'one' and 'two' share selection manifest {shared!r}"]
    gates = dict(cps.fleet_errors(jobs))
    assert gates["cron selection-manifest collisions"]


def test_the_selection_manifest_rule_is_the_one_the_stamp_exports():
    cps = _load()
    per_item = {"name": "lane", "output_contract": {"completion": "per-selected-item"}}
    assert cps.selection_manifest(per_item) == "/opt/data/cron-plus/selections/lane.json"
    assert cps._stamp_output_contract(per_item)["selection_manifest"] == \
        cps.selection_manifest(per_item)
    explicit = {**per_item, "selection_manifest": "/x.json"}
    assert cps.selection_manifest(explicit) == "/x.json"
    assert cps.selection_manifest({"name": "run", "output_contract": {"completion": "run"}}) == ""
    assert cps.selection_manifest({"name": "plain"}) == ""


# --- through the lane-writer renderer ensure-runtime runs -----------------------------------------


def test_ensure_runtime_registers_every_composed_writer_its_job_names(packs, tmp_path):
    """The composed store, reconciled by ensure-runtime's renderer, leaves every lane naming a
    registered server whose bound actor is that lane: `cron:<lane>@<pack>` for a composed one.

    Since okengine#1029 ensure-runtime builds the job set itself instead of reading the checkout's
    shared cron-plus-jobs.json, so the composed set is handed to the same renderer directly."""
    cps = _load()
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("lane_writers_968",
                                                  REPO / "scripts" / "lane_writers.py")
    lane_writers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lane_writers)
    jobs = _compose(cps, *packs, tmp_path / "cron-plus-jobs.json")
    text, _changes = lane_writers.reconcile(
        "mcp_servers:\n  okengine-write:\n    command: /custom/python\n", jobs, {}, cps)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    servers = yaml.safe_load(cfg.read_text())["mcp_servers"]
    composed = _composed(jobs)
    assert composed
    for job in jobs:
        if job.get("no_agent") or not isinstance(job.get("output_contract"), dict):
            continue
        bound = cps.contract_writer_name(job["name"])
        assert bound in _writers(job), job["name"]
        assert bound in servers, f"{job['name']} names {bound}, which is not registered"
        actor = servers[bound]["env"]["OKENGINE_WRITE_ACTOR"]
        assert actor == f"cron:{job['name']}", (job["name"], actor)
    for job in composed:
        actor = servers[cps.contract_writer_name(job["name"])]["env"]["OKENGINE_WRITE_ACTOR"]
        lane, pack = job["name"].split("@", 1)
        assert actor == f"cron:{lane}@{pack}"


# --- exact-comparison pins (mutation) ----------------------------------------------------------


def _dynamic(value: str) -> str:
    """Equal to `value` without sharing its identity."""
    return "".join(list(value))


def test_a_driver_clash_is_reported_whatever_order_the_packs_sort_in():
    cps = _load()
    tier_of = {"lane": "engine-template", "other": "engine-template"}
    engine = [{"name": "lane"}, {"name": "other"}]
    packs = [{"name": "okpack-z", "prompts": {"lane": "z"}, "domain": []},
             {"name": "okpack-a", "prompts": {"lane": "a", "other": "a"}, "domain": []}]
    jobs, errors = cps.merge_packs(engine, packs, tier_of)
    assert len(errors) == 1 and "'okpack-z' and 'okpack-a'" in errors[0], errors
    # The clash skips only that lane: the second pack's other, uncontested lane still ships.
    assert {j["name"] for j in jobs} == {"lane@okpack-z", "other@okpack-a"}


def test_a_pack_name_equal_but_not_identical_is_the_same_driver():
    cps = _load()
    packs = [{"name": "okpack-a", "prompts": {"lane": "x"}, "domain": []},
             {"name": _dynamic("okpack-a"), "prompts": {"lane": "y"}, "domain": []}]
    _, errors = cps.merge_packs([{"name": "lane"}], packs, {"lane": "engine-template"})
    assert errors == ["job-id collision: lane@okpack-a"]


def test_the_manifest_gate_reports_a_share_in_either_order_after_unselected_jobs():
    cps = _load()
    shared = "/s.json"
    jobs = [{"name": "plain"}, {"name": "zeta", "selection_manifest": shared},
            {"name": "alpha", "selection_manifest": shared}]
    assert cps.validate_unique_selection_manifests(jobs) == [
        f"jobs 'zeta' and 'alpha' share selection manifest {shared!r}"]


def test_the_manifest_gate_leaves_a_repeated_job_to_the_id_gate():
    cps = _load()
    jobs = [{"name": "lane", "selection_manifest": "/s.json"},
            {"name": _dynamic("lane"), "selection_manifest": "/s.json"}]
    assert cps.validate_unique_selection_manifests(jobs) == []


def test_only_a_per_selected_item_contract_gets_a_default_manifest():
    cps = _load()
    for completion in ("batch", "run", "zzz"):
        job = {"name": "lane", "output_contract": {"completion": completion}}
        assert cps.selection_manifest(job) == "", completion


def test_regen_reports_how_many_extension_jobs_it_folded_in(tmp_path, monkeypatch, capsys):
    cps = _load()
    cps.JOBS, cps.PACK_DIR = tmp_path / "jobs.json", tmp_path
    expected, errors = cps._extension_pass(tmp_path, [])
    assert errors == [] and expected
    cps.regen()
    assert f" + {len(expected)} extension -> " in capsys.readouterr().out
