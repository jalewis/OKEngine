"""okengine#969: a pack domain job may not put `@` in its name.

The `<lane>@<pack>` spelling is reserved for the engine-template instances merge_packs mints, and
the #961 normalizers (cron_pack_split.template_lane, write_services.state.lane_actor) strip
everything after `@`. On a single-pack regen nothing rejected a domain job named
`entity-backfill@shadow`, so the write path ran it AS entity-backfill: that lane's tool fence,
lane-specific checks and capability. The rule now sits in the one fleet gate list (regen and
compose alike) and in `framework validate`, the earliest gate a pack author runs.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron_pack_split.py"
VAL = REPO / "scripts" / "framework_validate.py"
INIT = REPO / "scripts" / "framework_init.py"
STATE = REPO / "src" / "okengine" / "write_services" / "state.py"


def _load(name: str, path: Path = MOD):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _domain_job(name: str) -> dict:
    return {"id": f"id-{name}", "name": name, "enabled": True, "no_agent": True,
            "script": "probe.py", "schedule": {"kind": "cron", "expr": "0 * * * *"}}


def _single_pack(tmp_path: Path, domain_name: str) -> Path:
    """The #969 repro: a pack drives entity-backfill AND ships a domain job named after it."""
    pack = tmp_path / "okpack-single"
    (pack / "crons").mkdir(parents=True)
    (pack / "crons" / "domain-crons.json").write_text(json.dumps([_domain_job(domain_name)]))
    (pack / "crons" / "engine-template-prompts.json").write_text(
        json.dumps({"entity-backfill": "Run entity-backfill for this pack."}))
    return pack


def _regen(tmp_path: Path, domain_name: str):
    cps = _load("cps_969_regen")
    cps.PACK_DIR = _single_pack(tmp_path, domain_name)
    cps.JOBS = tmp_path / "jobs.json"
    return cps, cps.regen


# --- the deploy gate: single-pack regen ------------------------------------------------------


def test_regen_rejects_a_domain_job_that_would_run_as_an_engine_lane(tmp_path):
    cps, regen = _regen(tmp_path, "entity-backfill@shadow")
    with pytest.raises(SystemExit) as raised:
        regen()
    message = str(raised.value)
    assert message.startswith("cron domain job-name errors (not deploying):"), message
    assert "'entity-backfill@shadow'" in message and "'entity-backfill'" in message, message
    assert not cps.JOBS.exists(), "a rejected regen must write nothing"


def test_regen_still_ships_a_plain_domain_job_next_to_the_lane_it_drives(tmp_path):
    cps, regen = _regen(tmp_path, "entity-shadow")
    names = {job["name"] for job in regen()}
    assert {"entity-shadow", "entity-backfill"} <= names
    assert cps.JOBS.is_file()


# --- the deploy gate: N-way compose ----------------------------------------------------------


def _packs_root(tmp_path: Path, domain_name: str) -> Path:
    root = tmp_path / "packs"
    pack = root / "okpack-a"
    (pack / "crons").mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        "name: okpack-a\nversion: 0.1.0\ntrust: public\nowns: {types: [alpha]}\n")
    (pack / "crons" / "domain-crons.json").write_text(json.dumps([_domain_job(domain_name)]))
    (pack / "crons" / "engine-template-prompts.json").write_text(
        json.dumps({"entity-backfill": "Run entity-backfill for okpack-a."}))
    return root


def test_regen_composed_rejects_an_at_in_a_domain_job_name(tmp_path):
    cps = _load("cps_969_composed")
    cps.JOBS, cps.PACK_DIR = tmp_path / "jobs.json", tmp_path
    with pytest.raises(SystemExit, match="cron domain job-name errors"):
        cps.regen_composed(_packs_root(tmp_path, "entity-backfill@shadow"))
    assert not cps.JOBS.exists()


def test_the_engine_minted_lane_at_pack_instance_stays_legal(tmp_path):
    """Negative fixture: the composed `<lane>@<pack>` name is the reserved spelling's owner."""
    cps = _load("cps_969_instance")
    cps.JOBS, cps.PACK_DIR = tmp_path / "jobs.json", tmp_path
    jobs = cps.regen_composed(_packs_root(tmp_path, "alpha-sweep"))
    names = {job["name"] for job in jobs}
    assert {"entity-backfill@okpack-a", "okpack-a:alpha-sweep"} <= names
    assert dict(cps.fleet_errors(jobs))["cron domain job-name errors"] == []


# --- the rule itself -------------------------------------------------------------------------


def test_only_jobs_carrying_the_pack_marker_are_held_to_the_rule():
    cps = _load("cps_969_rule")
    jobs = [{"name": "entity-backfill@okpack-a"},                 # engine-minted instance
            {"name": "okpack-a:sweep", "pack": "okpack-a"},       # plain domain job
            {"name": "x@y", "pack": "okpack-a"}]                  # the defect
    errors = cps.validate_domain_job_names(jobs)
    assert len(errors) == 1 and errors[0].startswith("domain job 'x@y' contains '@'"), errors


def test_an_empty_pack_marker_does_not_opt_a_job_out():
    cps = _load("cps_969_empty_marker")
    assert cps.validate_domain_job_names([{"name": "x@y", "pack": ""}]), \
        "the marker's presence, not its value, makes a job pack-authored"


@pytest.mark.parametrize("name", [None, "", 7, "plain"])
def test_a_missing_or_non_string_name_is_not_an_at_error(name):
    cps = _load("cps_969_odd_names")
    assert cps.domain_job_name_errors([{"name": name}]) == []


def test_the_error_names_the_lane_the_write_path_would_use():
    """The message's lane is the write path's lane_actor, not a guess."""
    cps = _load("cps_969_lane")
    state = _load("state_969", STATE)
    [error] = cps.domain_job_name_errors([{"name": "entity-backfill@shadow"}])
    assert f"lane {state.lane_actor('entity-backfill@shadow')!r}" in error, error


# --- the earliest gate: framework validate ---------------------------------------------------


def _scaffolded(tmp_path: Path, domain_name: str) -> list:
    job = _domain_job(domain_name)
    job["expect"] = {"may_idle": "test fixture"}
    return _scaffolded_with(tmp_path, job)


def _scaffolded_with(tmp_path: Path, job: dict) -> list:
    init = _load("framework_init_969", INIT)
    pack = tmp_path / "pack"
    assert init.main([str(pack), "--domain", "Test Domain", "--no-compose"]) == 0
    path = pack / "crons" / "domain-crons.json"
    jobs = json.loads(path.read_text()) if path.is_file() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    (pack / "crons" / "scripts").mkdir(exist_ok=True)
    (pack / "crons" / "scripts" / "probe.py").write_text("print('ok')\n")
    path.write_text(json.dumps(jobs + [job]))
    return _load("framework_validate_969", VAL).validate(pack).rows


def test_framework_validate_fails_an_at_in_a_domain_job_name(tmp_path):
    rows = _scaffolded(tmp_path, "entity-backfill@shadow")
    assert any(s == "FAIL" and c == "cron domain job name" and "entity-backfill@shadow" in d
               for s, c, d in rows), rows


def test_framework_validate_passes_a_plain_domain_job_name(tmp_path):
    rows = _scaffolded(tmp_path, "entity-shadow")
    assert not any(c == "cron domain job name" for _s, c, _d in rows), rows


def test_an_unloadable_domain_name_rule_reports_undetectable_not_clean(monkeypatch):
    fvc = _load("fvc_969", REPO / "scripts" / "framework_validate_crons.py")

    def boom(*_args, **_kwargs):
        raise ImportError("x" * 100)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", boom)
    [error] = fvc._domain_name_errors([{"name": "plain"}])
    assert error.startswith("cannot verify domain job name (cron_pack_split.py:"), error
    assert f"(cron_pack_split.py: {'x' * 80}) — " in error, "the cause is cut at 80 characters"
    assert error.endswith("UNDETECTABLE, not a pass"), error


# --- the exact-name twin: a domain job named like an engine lane -----------------------------


@pytest.mark.parametrize("lane", [
    "concept-backfill",   # engine-template lane this pack does NOT drive: merge drops the stub
    "reshelve",           # pure engine lane: used to surface only as an id/name collision
])
def test_regen_rejects_a_domain_job_named_like_an_engine_lane(tmp_path, lane):
    cps, regen = _regen(tmp_path, lane)
    with pytest.raises(SystemExit) as raised:
        regen()
    message = str(raised.value)
    assert message.startswith("cron domain job-name errors (not deploying):"), message
    assert f"okpack-single: domain job {lane!r} shadows an engine/engine-template lane" in message
    assert not cps.JOBS.exists(), "a rejected regen must write nothing"


def test_merge_packs_and_regen_share_one_shadow_rule():
    cps = _load("cps_969_shadow_rule")
    engine = [{"name": "tmpl"}, {"name": "eng"}]
    packs = [{"name": "acme", "prompts": {"tmpl": "drive it"},
              "domain": [{"name": "tmpl"}, {"name": "sweep"}]}]
    _, errors = cps.merge_packs(engine, packs, {"tmpl": "engine-template", "eng": "engine"})
    assert cps.domain_shadow_errors("acme", packs[0]["domain"], ["tmpl", "eng"])[0] in errors


def test_the_shadow_rule_ignores_non_string_names_and_reports_in_sorted_order():
    cps = _load("cps_969_shadow_odd")
    domain = [{"name": ["unhashable"]}, {"name": None}, {"name": "b"}, {"name": "a"}]
    errors = cps.domain_shadow_errors("", domain, {"a", "b", "c"})
    assert [e.split("'")[1] for e in errors] == ["a", "b"], errors
    assert errors[0].startswith("domain job 'a' shadows"), "no pack prefix without a pack name"


def test_the_pack_gate_checks_against_this_engines_own_cron_half():
    cps = _load("cps_969_engine_half")
    errors = cps.engine_shadow_errors([{"name": "entity-backfill"}, {"name": "entity-shadow"}])
    assert len(errors) == 1 and "'entity-backfill'" in errors[0], errors


def test_framework_validate_fails_a_domain_job_named_like_an_engine_lane(tmp_path):
    rows = _scaffolded(tmp_path, "concept-backfill")
    assert any(s == "FAIL" and c == "cron domain job shadows engine lane"
               and "'concept-backfill'" in d for s, c, d in rows), rows


def test_framework_validate_passes_a_plain_name_through_the_shadow_check(tmp_path):
    rows = _scaffolded(tmp_path, "entity-shadow")
    assert not any(c == "cron domain job shadows engine lane" for _s, c, _d in rows), rows


def test_an_unreadable_engine_cron_list_warns_undetectable_not_clean(tmp_path):
    """An engine checkout without config/engine-crons.json cannot answer the question; it WARNs,
    never passes and never FAILs the pack for the engine's own gap."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("cron_pack_split.py", "framework_validate_crons.py"):
        (scripts / name).write_text((REPO / "scripts" / name).read_text())
    fvc = _load("fvc_969_no_engine_list", scripts / "framework_validate_crons.py")

    class Report:
        def __init__(self):
            self.rows = []

        def fail(self, check, detail):
            self.rows.append(("FAIL", check, detail))

        def warn(self, check, detail):
            self.rows.append(("WARN", check, detail))

    report = Report()
    fvc._check_engine_shadows([{"name": "concept-backfill"}, "not-a-dict"], report)
    assert [(severity, check) for severity, check, _ in report.rows] == [
        ("WARN", "cron domain job shadows engine lane"), ("WARN", "cron write-server identity")]
    shadow, identity = (detail for _, _, detail in report.rows)
    assert shadow.startswith("cannot verify engine-lane shadowing"), shadow
    assert identity.startswith("cannot verify write-server identity"), identity
    assert shadow.endswith(fvc.UNDETECTABLE) and identity.endswith(fvc.UNDETECTABLE)


# --- write identity: the server name, not the job name ------------------------------------------
# QA of !1233: contract_writer_name lowercases and folds non-[a-z0-9] runs to '-', so a domain job
# named `Trends-Refresh` or `trends_refresh` derived `okengine-write-trends-refresh` -- the engine
# lane's own server, which ensure-runtime keeps registered as `cron:trends-refresh`. A job could
# also list that server explicitly, with no contract.


def _engine_def(name: str) -> dict:
    cps = _load("cps_969_engine_def")
    return next(j for j in cps._load_jobs(cps.ENGINE_CRONS_FILE) if j["name"] == name)


def _contracted(name: str) -> dict:
    """The QA repro: the engine trends-refresh def, renamed, as a pack domain job."""
    job = {k: v for k, v in _engine_def("trends-refresh").items() if k not in ("id", "script")}
    job.update(id=f"id-{name}", name=name, prompt="Refresh the trends.", script="probe.py")
    return job


def _explicit(server: str = "okengine-write-trends-refresh") -> dict:
    job = _domain_job("trend-peek")
    job.update(no_agent=False, prompt="Peek.", enabled_toolsets=["file_read", server])
    return job


def _regen_with(tmp_path: Path, job: dict):
    cps = _load("cps_969_identity")
    pack = tmp_path / "okpack-single"
    (pack / "crons").mkdir(parents=True)
    (pack / "crons" / "domain-crons.json").write_text(json.dumps([job]))
    cps.PACK_DIR, cps.JOBS = pack, tmp_path / "jobs.json"
    return cps


@pytest.mark.parametrize("name", ["Trends-Refresh", "trends_refresh", "TRENDS refresh"])
def test_regen_rejects_a_domain_job_whose_name_folds_to_an_engine_writer(tmp_path, name):
    cps = _regen_with(tmp_path, _contracted(name))
    with pytest.raises(SystemExit) as raised:
        cps.regen()
    message = str(raised.value)
    assert message.startswith("cron write-identity errors (not deploying):"), message
    assert (f"pack job {name!r} binds write server 'okengine-write-trends-refresh', the identity "
            "of engine lane 'trends-refresh'") in message, message
    assert not cps.JOBS.exists(), "a rejected regen must write nothing"


def test_regen_rejects_an_explicit_foreign_engine_writer(tmp_path):
    cps = _regen_with(tmp_path, _explicit())
    with pytest.raises(SystemExit, match="cron write-identity errors") as raised:
        cps.regen()
    assert "'trend-peek' binds write server 'okengine-write-trends-refresh'" in str(raised.value)
    assert not cps.JOBS.exists()


def test_regen_rejects_the_pre_bound_spelling_of_an_engine_writer(tmp_path):
    """The unbounded (pre-#840) server name is the same identity as the bounded one."""
    cps = _load("cps_969_unbounded")
    long_lane = "a-very-long-engine-lane-name-that-the-writer-derivation-must-bound"
    unbounded = cps.unbounded_writer_name(long_lane)
    assert unbounded != cps.contract_writer_name(long_lane), "fixture must exercise bounding"
    cps.ENGINE_CRONS_FILE = tmp_path / "engine-crons.json"
    cps.ENGINE_CRONS_FILE.write_text(json.dumps({"jobs": [{"name": long_lane}, {}]}))
    reserved = cps.reserved_writers()
    assert reserved[unbounded] == reserved[cps.contract_writer_name(long_lane)] == \
        f"engine lane {long_lane!r}"
    job = {"name": "peek", "pack": "p", "enabled_toolsets": [unbounded]}
    [error] = cps.writer_identity_errors([job], reserved)
    assert f"the identity of engine lane {long_lane!r}:" in error, error


def test_a_pack_may_not_bind_a_composed_instance_or_a_sibling_writer():
    cps = _load("cps_969_siblings")
    jobs = cps.bind_contract_writers([
        {"name": "entity-backfill@okpack-a", "output_contract": {}, "enabled_toolsets": []},
        {"name": "okpack-a:sweep", "pack": "okpack-a", "output_contract": {}, "enabled_toolsets": []},
        {"name": "okpack-b:thief", "pack": "okpack-b", "enabled_toolsets": [
            "okengine-write-entity-backfill-okpack-a", "okengine-write-okpack-a-sweep"]}])
    errors = cps.writer_identity_errors(jobs)
    assert len(errors) == 2, errors
    assert "the identity of lane 'entity-backfill@okpack-a'" in errors[0]
    assert "the identity of lane 'okpack-a:sweep'" in errors[1]


def test_a_pack_jobs_own_writer_and_the_generic_writer_are_its_to_bind():
    cps = _load("cps_969_own_writer")
    jobs = cps.bind_contract_writers([
        {"name": "trend-digest", "pack": "p", "output_contract": {},
         "enabled_toolsets": ["okengine-write", "okengine-write-trend-digest"]},
        {"name": "plain", "pack": "p", "no_agent": True, "enabled_toolsets": ["okengine-write"]},
        {"name": "", "pack": "p", "enabled_toolsets": []}])
    assert cps.writer_identity_errors(jobs, cps.reserved_writers()) == []


def test_engine_and_composed_lanes_are_not_held_to_the_pack_rule():
    """Only pack-marked jobs are checked: an engine lane or composed instance owns its server."""
    cps = _load("cps_969_engine_owns")
    jobs = cps.bind_contract_writers([
        {"name": "trends-refresh", "output_contract": {}, "enabled_toolsets": []},
        {"name": "trends-refresh@okpack-a", "output_contract": {}, "enabled_toolsets": []}])
    assert cps.writer_identity_errors(jobs, cps.reserved_writers()) == []


def test_regen_still_ships_a_pack_that_drives_the_engine_template_lane(tmp_path):
    """Negative fixture: the sanctioned way to run trends-refresh is its prompt, not a twin."""
    cps = _regen_with(tmp_path, _domain_job("trend-digest"))
    (cps.PACK_DIR / "crons" / "engine-template-prompts.json").write_text(
        json.dumps({"trends-refresh": "Refresh the trends for this pack."}))
    jobs = {job["name"]: job for job in cps.regen()}
    assert "okengine-write-trends-refresh" in jobs["trends-refresh"]["enabled_toolsets"]


def test_the_composed_fleet_with_driven_instances_passes_the_identity_gate(tmp_path):
    cps = _load("cps_969_composed_identity")
    cps.JOBS, cps.PACK_DIR = tmp_path / "jobs.json", tmp_path
    jobs = cps.regen_composed(_packs_root(tmp_path, "alpha-sweep"))
    assert dict(cps.fleet_errors(jobs))["cron write-identity errors"] == []


def test_framework_validate_fails_a_name_that_folds_to_an_engine_writer(tmp_path):
    rows = _scaffolded_with(tmp_path, _contracted("Trends-Refresh"))
    assert any(s == "FAIL" and c == "cron write-server identity"
               and "'okengine-write-trends-refresh'" in d for s, c, d in rows), rows


def test_framework_validate_fails_an_explicit_foreign_engine_writer(tmp_path):
    rows = _scaffolded_with(tmp_path, _explicit())
    assert any(s == "FAIL" and c == "cron write-server identity" and "'trend-peek'" in d
               for s, c, d in rows), rows


def test_framework_validate_passes_a_pack_jobs_own_writer(tmp_path):
    rows = _scaffolded_with(tmp_path, _contracted("trend-digest"))
    assert not any(c == "cron write-server identity" for _s, c, _d in rows), rows


def test_the_pack_gate_binds_defs_as_regen_will():
    cps = _load("cps_969_pack_gate")
    assert cps.engine_writer_errors([_contracted("Trends-Refresh")]), "derived writer is checked"
    assert cps.engine_writer_errors([{"name": "x", "enabled_toolsets": [
        "okengine-write-trends-refresh"]}]), "an explicit writer is checked without a contract"
    assert cps.engine_writer_errors([{"name": "x", "pack": "declared"}]) == []


# --- identities the fleet does not carry: baseline and tier-1 extension servers --------------
# QA of !1233 (e903266e): a lane could name a LIVE server that belongs to a lane outside this
# fleet and engine-crons.json: ensure-runtime's fixed `okengine-write-source-quality`
# (cron:source-quality-backfill), or a disabled extension's server.


def test_regen_rejects_the_baseline_source_quality_server(tmp_path):
    job = _contracted("trend-digest")
    job["enabled_toolsets"] = list(job["enabled_toolsets"]) + ["okengine-write-source-quality"]
    cps = _regen_with(tmp_path, job)
    with pytest.raises(SystemExit, match="cron write-identity errors") as raised:
        cps.regen()
    assert ("binds write server 'okengine-write-source-quality', the identity of the baseline "
            "server ensure-runtime registers as cron:source-quality-backfill") in str(raised.value)
    assert not cps.JOBS.exists()


def test_every_tier1_extension_operation_is_reserved_enabled_or_not():
    cps = _load("cps_969_ext_reserved")
    reserved = cps.reserved_writers()
    assert reserved["okengine-write-okengine-predictions-grade"] == \
        "extension lane 'okengine.predictions:grade'"
    ids, names = cps._tier1_extensions()
    assert "okengine.predictions:grade" in names and len(names) > 10, names
    assert "okengine.predictions" in ids and all(i.startswith("okengine.") for i in ids), ids


def test_regen_rejects_a_disabled_extensions_server(tmp_path):
    cps = _regen_with(tmp_path, _explicit("okengine-write-okengine-predictions-grade"))
    with pytest.raises(SystemExit) as raised:
        cps.regen()
    assert "the identity of extension lane 'okengine.predictions:grade'" in str(raised.value)


def test_an_enabled_extensions_own_lane_keeps_its_server():
    """Negative fixture: only pack-marked jobs are judged; the extension owns its server."""
    cps = _load("cps_969_ext_own")
    jobs = cps.bind_contract_writers([{"name": "okengine.predictions:grade",
                                       "extension": "okengine.predictions",
                                       "output_contract": {}, "enabled_toolsets": []}])
    assert cps.validate_untrusted_writers(jobs) == []


def test_without_the_composer_no_extension_lane_is_reserved(monkeypatch):
    cps = _load("cps_969_no_composer")
    monkeypatch.setattr(cps, "_extension_compose", lambda: None)
    assert cps._tier1_extensions() == (frozenset(), [])
    assert cps._extension_pass(Path("."), []) == ([], [])
    with pytest.raises(RuntimeError, match="extension_compose.py is absent"):
        cps.pack_extension_writer_errors(Path("."))


def test_framework_validate_fails_the_baseline_server(tmp_path):
    rows = _scaffolded_with(tmp_path, _explicit("okengine-write-source-quality"))
    assert any(s == "FAIL" and c == "cron write-server identity"
               and "'okengine-write-source-quality'" in d for s, c, d in rows), rows


# --- the generic, actor-less writer on a pack model lane --------------------------------------


@pytest.mark.parametrize("extra", [{}, {"output_contract_exempt": "reason"}])
def test_regen_rejects_a_pack_model_lane_on_the_generic_writer(tmp_path, extra):
    job = _explicit("okengine-write")
    job.update(extra)
    cps = _regen_with(tmp_path, job)
    with pytest.raises(SystemExit) as raised:
        cps.regen()
    assert ("pack model lane 'trend-peek' binds the generic, actor-less okengine-write server"
            in str(raised.value)), raised.value
    assert not cps.JOBS.exists()


def test_the_agent_writer_rule():
    cps = _load("cps_969_agent_rule")
    own = "okengine-write-own"
    errors = cps.validate_pack_agent_writers([
        {"name": "own", "pack": "p", "enabled_toolsets": [own]},                  # no contract
        {"name": "exempt", "pack": "p", "enabled_toolsets": ["okengine-write-exempt"],
         "output_contract_exempt": "why"},                                      # exempt: own ok
        {"name": "c", "pack": "p", "output_contract": {}, "enabled_toolsets": [own]},
        {"name": "s", "pack": "p", "no_agent": True, "enabled_toolsets": ["okengine-write"]},
        {"name": "t", "pack": "p", "no_agent": 1, "enabled_toolsets": ["okengine-write"]},
        {"name": "r", "pack": "p", "enabled_toolsets": ["okengine"]},              # reads only
        {"name": "e", "enabled_toolsets": ["okengine-write"]},                    # not a pack job
    ])
    assert errors == ["pack model lane 'own' binds ['okengine-write-own'] but declares no "
                      "output_contract -- a model-writing lane must declare one"], errors


def test_a_contracted_pack_lane_listing_the_generic_writer_is_rebound_and_passes(tmp_path):
    cps = _regen_with(tmp_path, _contracted("trend-digest"))
    [job] = [j for j in cps.regen() if j["name"] == "trend-digest"]
    assert "okengine-write" not in job["enabled_toolsets"]
    assert "okengine-write-trend-digest" in job["enabled_toolsets"]


# --- deploy-time ownership against the live runtime config ------------------------------------


def _server(actor):
    return {"command": "python", "env": {"OKENGINE_WRITE_ACTOR": actor}}


def test_a_live_server_registered_for_another_identity_fails_the_deploy():
    cps = _load("cps_969_foreign")
    config = {"mcp_servers": {
        "okengine-write-okpack-other-x": _server("cron:okpack-other:x"),
        "okengine-write-mine": _server("cron:mine"),
        "okengine-write-bare": {"command": "python", "env": "not-a-mapping"},
        "okengine-write": {"command": "python"}}}
    jobs = ["not-a-job",
            {"name": "engine-lane", "enabled_toolsets": ["okengine-write-mine"]},
            {"name": "mine", "pack": "p", "enabled_toolsets": [
                "okengine-write", "okengine-write-not-yet", "okengine-write-mine",
                "okengine-write-okpack-other-x", "okengine-write-bare"]}]
    assert cps.foreign_writer_bindings(jobs, config) == [
        "pack job 'mine' binds write server 'okengine-write-okpack-other-x', which this "
        "deployment registers as 'cron:okpack-other:x', not 'cron:mine': the job would "
        "write as that identity -- rename the job or drop the writer",
        "pack job 'mine' binds write server 'okengine-write-bare', which this deployment "
        "registers as None, not 'cron:mine': the job would write as that identity -- rename "
        "the job or drop the writer"]


@pytest.mark.parametrize("config", [None, {}, {"mcp_servers": None}, {"mcp_servers": []}])
def test_a_fresh_deployment_with_no_servers_has_no_foreign_bindings(config):
    cps = _load("cps_969_fresh")
    jobs = [{"name": "mine", "pack": "p", "enabled_toolsets": ["okengine-write-mine"]}]
    assert cps.foreign_writer_bindings(jobs, config) == []


def test_the_jobs_deploy_checks_ownership_before_the_live_store_is_written():
    t = (REPO / "scripts" / "deploy-cron-plus-jobs.sh").read_text()
    check = t.index("cron_pack_split.foreign_writer_bindings(jobs, runtime_config)")
    assert t.index("cron_pack_split.unregistered_writers(jobs, runtime_config)") < check \
        < t.index("cat >"), "ownership must be checked before the live jobs store is written"
    assert "sys.exit(1)" in t[check:check + 300], "a foreign binding must fail the deploy"


# --- pack- and operator-tier extension operations ---------------------------------------------
# QA of !1233 (7c6eb72b): the rules keyed on the `pack` marker, which extension jobs never carry,
# so a pack-tier copy of okengine.lacuna listing okengine-write-source-quality in its toolsets
# regenerated clean and wrote as cron:source-quality-backfill. Trust is the DISCOVERY tier: only
# engine lanes and tier-1 extension operations may bind anything but their own server.

EXT_COMPOSE = REPO / "scripts" / "extension_compose.py"
FRAMEWORK_EXT = REPO / "scripts" / "framework_extensions.py"


def _sneak(pack: Path, tier: str = "pack", ext_id: str = "acme.sneak",
           toolsets=("todo", "okengine-write-source-quality", "okengine")) -> Path:
    """QA's pext repro: okengine.lacuna copied to a pack (or operator) tier under a new id."""
    import shutil

    import yaml
    root = pack / "extensions" if tier == "pack" else pack / ".okengine" / "extensions"
    dest = root / ext_id
    shutil.copytree(REPO / "extensions" / "okengine.lacuna", dest,
                    ignore=shutil.ignore_patterns("__pycache__"))
    manifest = yaml.safe_load((dest / "extension.yaml").read_text())
    manifest["id"] = ext_id
    manifest.pop("core", None)
    manifest.pop("schema", None)                 # no second owner for the lacuna type
    manifest["capabilities"]["write"] = ["sneak/**"]
    op = manifest["operation"]
    op.pop("prompt_file", None)
    op["prompt"] = "Write whatever you like."
    op["toolsets"] = list(toolsets)
    (dest / "extension.yaml").write_text(yaml.safe_dump(manifest))
    (pack / ".okengine").mkdir(parents=True, exist_ok=True)
    # an operator-tier in-gateway extension also needs a recorded trust acceptance (okengine#994
    # [14]); record it so these tests keep exercising the foreign-writer gate, not the trust gate
    entry = ("{trust_accepted: {trust: in-gateway}}" if tier == "operator" else "{}")
    (pack / ".okengine" / "extensions.yaml").write_text(f"enabled:\n  {ext_id}: {entry}\n")
    return dest


def _record(pack: Path, ext_id: str = "acme.sneak") -> dict:
    comp = _load("ext_compose_969", EXT_COMPOSE)
    records, _ = comp._discovery_mod().discover(pack)
    return next(r for r in records if r["id"] == ext_id)


@pytest.mark.parametrize("tier", ["pack", "operator"])
def test_the_composer_refuses_a_foreign_server_in_an_untrusted_operation(tmp_path, tier):
    _sneak(tmp_path, tier)
    record = _record(tmp_path)
    assert record["tier"] == tier
    comp = _load("ext_compose_969_refuse", EXT_COMPOSE)
    [error] = comp.foreign_writer_errors(record)
    assert error.startswith(f"acme.sneak: {tier}-tier operation 'acme.sneak' lists write server(s) "
                            "['okengine-write-source-quality'] that are not its own"), error
    jobs, errors, _ = comp.synthesize_ops(record)
    assert jobs == [] and errors == [error]


@pytest.mark.parametrize("tier", ["pack", "operator"])
def test_regen_refuses_an_untrusted_extension_on_a_foreign_server(tmp_path, tier):
    pack = tmp_path / "okpack-single"
    pack.mkdir()
    _sneak(pack, tier)
    cps = _load("cps_969_pext")
    cps.PACK_DIR, cps.JOBS = pack, tmp_path / "jobs.json"
    with pytest.raises(SystemExit) as raised:
        cps.regen()
    message = str(raised.value)
    assert message.startswith("extension composition errors (not deploying):"), message
    assert "'okengine-write-source-quality'" in message
    assert not cps.JOBS.exists()


def test_framework_validate_fails_an_untrusted_extension_on_a_foreign_server(tmp_path):
    rows = _scaffolded_with(tmp_path, _domain_job("plain"))
    _sneak(tmp_path / "pack")
    rows = _load("framework_validate_969_pext", VAL).validate(tmp_path / "pack").rows
    assert any(s == "FAIL" and c == "extension write servers"
               and "'okengine-write-source-quality'" in d for s, c, d in rows), rows


def test_framework_validate_reports_extension_writers_even_without_crons(tmp_path):
    """The check runs before the no-crons/ early return."""
    fvc = _load("fvc_969_nocrons", REPO / "scripts" / "framework_validate_crons.py")
    _sneak(tmp_path)

    class Report:
        def __init__(self):
            self.rows = []

        def fail(self, check, detail):
            self.rows.append(("FAIL", check))

        def warn(self, check, detail):
            self.rows.append(("WARN", check))

        def ok(self, check, detail):
            self.rows.append(("OK", check))

        def info(self, check, detail):
            self.rows.append(("INFO", check))

    report = Report()
    fvc.check_crons(tmp_path, report, validator_dir=REPO / "scripts",
                    engine_cron_dir=REPO / "scripts" / "cron", load_yaml=None,
                    cron_expr=None, dst_schedule_problem=None)
    assert ("FAIL", "extension write servers") in report.rows, report.rows


def test_framework_extensions_validate_fails_an_untrusted_extension_on_a_foreign_server(
        tmp_path, capsys):
    _sneak(tmp_path)
    fx = _load("framework_extensions_969", FRAMEWORK_EXT)
    assert fx.main(["validate", str(tmp_path)]) == 1
    assert "'okengine-write-source-quality'" in capsys.readouterr().out


def test_an_untrusted_extension_on_its_own_server_is_fine(tmp_path, capsys):
    """Negative fixture: a pack-tier operation may name its own derived server."""
    pack = tmp_path / "okpack-single"
    pack.mkdir()
    _sneak(pack, toolsets=("todo", "okengine-write-acme-sneak", "okengine"))
    comp = _load("ext_compose_969_own", EXT_COMPOSE)
    assert comp.foreign_writer_errors(_record(pack)) == []
    cps = _load("cps_969_pext_own")
    cps.PACK_DIR, cps.JOBS = pack, tmp_path / "jobs.json"
    [job] = [j for j in cps.regen() if j["name"] == "acme.sneak"]
    assert "okengine-write-acme-sneak" in job["enabled_toolsets"]
    fx = _load("framework_extensions_969_own", FRAMEWORK_EXT)
    assert fx.main(["validate", str(pack)]) == 0


def test_tier1_extensions_keep_their_toolsets_as_written():
    comp = _load("ext_compose_969_tier1", EXT_COMPOSE)
    records, _ = comp._discovery_mod().discover(None)
    assert records and all(comp.foreign_writer_errors(r) == [] for r in records)
    record = dict(records[0], tier="engine", manifest={
        "id": "okengine.x", "kind": "operation",
        "operation": {"toolsets": ["okengine-write-source-quality"]}})
    assert comp.foreign_writer_errors(record) == []


def test_an_untrusted_record_with_a_malformed_operation_set_is_left_to_the_composer(tmp_path):
    comp = _load("ext_compose_969_malformed", EXT_COMPOSE)
    record = {"id": "acme.bad", "tier": "pack", "dir": str(tmp_path),
              "manifest": {"id": "acme.bad", "kind": "operation"}}
    assert comp.foreign_writer_errors(record) == []
    no_tier = {"id": "acme.none", "dir": str(tmp_path), "manifest": {
        "id": "acme.none", "operations": {"a": {"toolsets": "okengine-write-x"},
                                          "b": {"toolsets": ["okengine-write-y"]}}}}
    [error] = comp.foreign_writer_errors(no_tier)
    assert error.startswith("acme.none: None-tier operation 'acme.none:b'"), error


def test_the_fleet_gate_judges_untrusted_extension_jobs():
    """The regen-layer backstop, if a job ever reached the fleet past the composer."""
    cps = _load("cps_969_fleet_ext")
    jobs = [
        {"name": "acme.sneak", "extension": "acme.sneak", "output_contract": {},
         "enabled_toolsets": ["okengine-write-acme-sneak", "okengine-write-source-quality"]},
        {"name": "acme.model", "extension": "acme.model", "enabled_toolsets": ["okengine-write"]},
        {"name": "okengine.lacuna", "extension": "okengine.lacuna",
         "enabled_toolsets": ["okengine-write", "okengine-write-source-quality"]},
    ]
    errors = cps.validate_untrusted_writers(jobs)
    assert len(errors) == 2, errors
    assert errors[0].startswith("pack/operator-tier extension job 'acme.sneak' binds write server "
                                "'okengine-write-source-quality'"), errors[0]
    assert errors[1].startswith("pack/operator-tier extension model lane 'acme.model' binds the "
                                "generic"), errors[1]


def test_deploy_ownership_judges_untrusted_extension_jobs():
    cps = _load("cps_969_deploy_ext")
    config = {"mcp_servers": {
        "okengine-write-acme-sneak": _server("cron:source-quality-backfill"),
        "okengine-write-okengine-lacuna": _server("cron:somebody-else")}}
    jobs = [{"name": "acme.sneak", "extension": "acme.sneak",
             "enabled_toolsets": ["okengine-write-acme-sneak"]},
            {"name": "okengine.lacuna", "extension": "okengine.lacuna",
             "enabled_toolsets": ["okengine-write-okengine-lacuna"]},
            {"name": "okengine.predictions:grade", "extension": "okengine.predictions",
             "enabled_toolsets": ["okengine-write-okengine-lacuna"]}]
    assert "okengine.predictions" not in cps._tier1_extensions()[1], "an id, not an op name"
    [error] = cps.foreign_writer_bindings(jobs, config)       # tier-1 ids discovered by default
    assert error.startswith("pack/operator-tier extension job 'acme.sneak' binds write server "
                            "'okengine-write-acme-sneak', which this deployment registers as "
                            "'cron:source-quality-backfill'"), error
    others = cps.foreign_writer_bindings(jobs, config, frozenset({"acme.sneak"}))
    assert [e.split("'")[1] for e in others] == ["okengine.lacuna", "okengine.predictions:grade"], \
        "trust is the given tier-1 set, not the id's spelling"
