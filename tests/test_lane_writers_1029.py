"""okengine#1029: ensure-runtime renders per-lane writers from THIS deployment's own job set.

It used to read the engine checkout's shared config/cron-plus-jobs.json, which holds whichever
pack was split LAST in that checkout, so every host gained the previous host's lane writers. And
it only ever added, so the foreign identities stayed. These tests pin the per-deployment render,
the pruning of stale engine-generated writers, the survival of everything else, and the deploy
gate that refuses a config still carrying a foreign lane writer.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

CONTRACT = {
    "api": 1, "allowed_namespaces": ["briefs"], "allowed_types": ["brief"],
    "operations": ["create"], "required_fields": ["type"], "required_relationships": [],
    "body": {"required": False, "min_non_whitespace": 0}, "unknown_fields": "allow",
    "unresolved_links": "review", "placeholder_links": "reject", "completion": "run",
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lw():
    sys.path.insert(0, str(REPO / "src"))
    return _load("lane_writers_under_test", SCRIPTS / "lane_writers.py")


@pytest.fixture(scope="module")
def cps():
    sys.path.insert(0, str(REPO / "src"))
    return _load("cps_under_test_1029", SCRIPTS / "cron_pack_split.py")


def _lane(name: str, **extra) -> dict:
    return {"name": name, "schedule": {"kind": "cron", "expr": "0 3 * * *"},
            "prompt": "write it", "enabled_toolsets": ["okengine-write"],
            "output_contract": CONTRACT, "max_iterations": 10, "timeout": 600,
            "expect": {"may_idle": {"max_idle_runs": 10}}, **extra}


def _pack(root: Path, name: str, lanes: list[dict], config: str | None = None) -> Path:
    pack = root / name
    (pack / "crons").mkdir(parents=True)
    (pack / "pack.yaml").write_text(f"name: {name}\nversion: 0.1.0\n")
    (pack / "crons" / "domain-crons.json").write_text(json.dumps(lanes))
    if config is not None:
        (pack / ".hermes-data").mkdir()
        (pack / ".hermes-data" / "config.yaml").write_text(config)
    return pack


def _generated(cps, lane: str, name: str | None = None, command="/opt/hermes/.venv/bin/python",
               marker=True, enforce=True) -> str:
    """A generated writer. ``enforce`` matches what the renderer gives a contracted lane, so a
    current writer of a contracted lane is not stale against it (okengine#1042 [3])."""
    name = name or cps.contract_writer_name(lane)
    return ((f"  # Engine-managed server-bound identity for cron:{lane}.\n" if marker else "")
            + f"  {name}:\n    command: {command}\n    args:\n"
            "    - /opt/hermes/okengine-mcp/write_server.py\n    env:\n"
            f"      OKENGINE_WRITE_ACTOR: cron:{lane}\n"
            + ("      OKENGINE_OUTPUT_CONTRACT_MODE: enforce\n" if enforce else "") + "\n")


def _engine_copy(tmp_path: Path) -> Path:
    """A private engine checkout, so a test may split into ITS shared artifact freely."""
    engine = tmp_path / "engine"
    for part in ("config", "scripts", "src", "prompts", "extensions"):
        shutil.copytree(REPO / part, engine / part,
                        ignore=shutil.ignore_patterns("__pycache__", "cron-plus-jobs.json"))
    return engine


def _env(engine: Path | None = None, **extra) -> dict:
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1")
    if engine is not None:
        env["PYTHONPATH"] = str(engine / "src")
    env.update(extra)
    return env


def _ensure_runtime(engine: Path, pack: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(engine / "scripts" / "ensure-runtime.sh"), str(pack),
                           "--fix-perms"],
                          capture_output=True, text=True, timeout=120, env=_env(engine))


def _split(engine: Path, pack: Path) -> None:
    r = subprocess.run([sys.executable, str(engine / "scripts" / "cron_pack_split.py"), "regen"],
                       capture_output=True, text=True, timeout=120,
                       env=_env(engine, CRON_PACK_DIR=str(pack)))
    assert r.returncode == 0, r.stdout + r.stderr


def _servers(pack: Path) -> dict:
    return yaml.safe_load((pack / ".hermes-data" / "config.yaml").read_text())["mcp_servers"]


# --- end to end: ensure-runtime.sh ------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_after_two_packs_split_in_sequence_the_first_renders_only_its_own_lanes(tmp_path, cps):
    """The shared artifact holds beta (split last); alpha's runtime must still get alpha's lane."""
    engine = _engine_copy(tmp_path)
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")])
    beta = _pack(tmp_path, "okpack-beta", [_lane("okpack-beta-brief")])
    _split(engine, alpha)
    _split(engine, beta)
    shared = json.loads((engine / "config" / "cron-plus-jobs.json").read_text())
    assert "okpack-beta-brief" in {j["name"] for j in shared["jobs"]}, "fixture: beta split last"

    r = _ensure_runtime(engine, alpha)
    assert r.returncode == 0, r.stdout + r.stderr
    servers = _servers(alpha)
    assert cps.contract_writer_name("okpack-alpha-brief") in servers
    assert cps.contract_writer_name("okpack-beta-brief") not in servers, \
        "alpha's runtime registered a writer for beta's lane from the shared split"


@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_a_fresh_deploy_registers_its_lanes_before_any_split_exists(tmp_path, cps):
    """deploy.sh runs ensure-runtime before the jobs deploy: no shared artifact and no deployed
    jobs.json yet. The lane writer must exist before the gateway first starts anyway."""
    engine = _engine_copy(tmp_path)
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")])
    assert not (engine / "config" / "cron-plus-jobs.json").exists()
    assert not (alpha / ".hermes-data").exists()

    r = _ensure_runtime(engine, alpha)
    assert r.returncode == 0, r.stdout + r.stderr
    server = _servers(alpha)[cps.contract_writer_name("okpack-alpha-brief")]
    assert server["env"]["OKENGINE_WRITE_ACTOR"] == "cron:okpack-alpha-brief"
    assert server["env"]["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce"
    assert server["env"]["OKENGINE_PACK"] == "okpack-alpha"
    assert not (engine / "config" / "cron-plus-jobs.json").exists(), \
        "rendering writers must not depend on (or write) the shared checkout artifact"


@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_a_stale_foreign_writer_is_removed_and_hand_added_servers_survive(tmp_path, cps):
    engine = _engine_copy(tmp_path)
    foreign = cps.contract_writer_name("okpack-beta-brief")
    config = ("mcp_servers:\n"
              "  okengine-write:\n    command: /custom/python\n"
              + _generated(cps, "okpack-beta-brief")
              + _generated(cps, "okpack-beta-digest", marker=False)
              # hand-added: same derived name, an operator's own interpreter
              + _generated(cps, "okpack-beta-ops", command="/usr/local/bin/python", marker=False)
              # hand-added: a name the engine would never derive for its actor
              + _generated(cps, "okpack-beta-other", name="okengine-write-mine", marker=False)
              + "  my-tool:\n    command: /usr/bin/my-tool\n"
              "platform_toolsets:\n  cron: [no_mcp]\n")
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")], config=config)

    r = _ensure_runtime(engine, alpha)
    assert r.returncode == 0, r.stdout + r.stderr
    servers = _servers(alpha)
    assert foreign not in servers
    assert cps.contract_writer_name("okpack-beta-digest") not in servers
    text = (alpha / ".hermes-data" / "config.yaml").read_text()
    assert "identity for cron:okpack-beta-brief." not in text, "the marker goes with its block"
    for kept in ("okengine-write", "okengine-write-chat", "okengine-write-source-quality",
                 cps.contract_writer_name("okpack-beta-ops"), "okengine-write-mine", "my-tool"):
        assert kept in servers, f"{kept} must survive"
    assert cps.contract_writer_name("okpack-alpha-brief") in servers
    assert "removed 2 writer(s) for lanes this deployment does not run" in r.stdout, r.stdout

    second = _ensure_runtime(engine, alpha)
    assert second.returncode == 0, second.stderr
    assert (alpha / ".hermes-data" / "config.yaml").read_text() == text, "must be idempotent"


@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_a_broken_fleet_fails_ensure_runtime_instead_of_rendering_nothing(tmp_path):
    engine = _engine_copy(tmp_path)
    bad = _lane("okpack-alpha-brief")
    del bad["max_iterations"]
    alpha = _pack(tmp_path, "okpack-alpha", [bad], config="mcp_servers:\n")
    r = _ensure_runtime(engine, alpha)
    assert r.returncode != 0
    assert "cannot generate this deployment's cron fleet" in r.stderr, r.stderr


def test_deploy_sh_no_longer_regenerates_the_shared_split_for_ensure_runtime():
    """The step-1 regen only fed ensure-runtime the shared artifact; it must not come back."""
    deploy = (SCRIPTS / "deploy.sh").read_text()
    lines = [line for line in deploy.splitlines() if not line.lstrip().startswith("#")]
    assert not any("cron_pack_split.py" in line for line in lines), \
        "deploy.sh must not split into the shared artifact; ensure-runtime builds its own fleet"
    ensure = (SCRIPTS / "ensure-runtime.sh").read_text()
    code = [line for line in ensure.splitlines() if not line.lstrip().startswith("#")]
    assert not any("cron-plus-jobs.json" in line and "/opt/data" not in line for line in code), \
        "ensure-runtime must never read the shared checkout split"
    assert 'scripts/lane_writers.py" --pack "$PACK"' in ensure


def test_the_jobs_deploy_refuses_a_config_with_stale_lane_writers():
    t = (SCRIPTS / "deploy-cron-plus-jobs.sh").read_text()
    check = t.index("lane_writers.stale_writer_errors(runtime_config, jobs")
    assert t.index("runtime_config = yaml.safe_load") < check < t.index("cat >")
    assert "sys.exit(1)" in t[check:check + 500], "a stale lane writer must fail the deploy"


# --- the module ------------------------------------------------------------------------------------

def test_wanted_actors_union_policy_contracts_and_write_tools(lw, cps):
    jobs = ["not-a-job",
            {"name": "a", "output_contract": {}},
            {"name": "b", "output_contract": {}, "no_agent": True},
            {"name": "c", "write_tools": ["update_entity", " create_entity "]}]
    policy = {"capabilities": {"cron:p": {}, "extension:q": {}, 7: {}}}
    actors, tools = lw.wanted_actors(jobs, policy, cps)
    assert actors == {"cron:p": False, "cron:a": True, "cron:c": False}
    assert tools == {"cron:c": "update_entity,create_entity"}
    assert lw.wanted_actors(None, {}, cps) == ({}, {})


@pytest.mark.parametrize("name,server", [
    ("okengine-write", {}),
    ("okengine-write-chat", {}),
    ("okengine-write-source-quality", {}),
    ("my-tool", {}),
    ("okengine-write-x", "not-a-mapping"),
    ("okengine-write-x", {"env": "not-a-mapping"}),
    ("okengine-write-x", {"env": {"OKENGINE_WRITE_ACTOR": "chat:x"}}),
    ("okengine-write-x", {"env": {"OKENGINE_WRITE_ACTOR": 5}}),
    ("okengine-write-mine", {"command": "/opt/hermes/.venv/bin/python",
                             "args": ["/opt/hermes/okengine-mcp/write_server.py"],
                             "env": {"OKENGINE_WRITE_ACTOR": "cron:x"}}),
    ("okengine-write-x", {"command": "/custom/python",
                          "args": ["/opt/hermes/okengine-mcp/write_server.py"],
                          "env": {"OKENGINE_WRITE_ACTOR": "cron:x"}}),
    ("okengine-write-x", {"command": "/opt/hermes/.venv/bin/python",
                          "args": ["/elsewhere/write_server.py"],
                          "env": {"OKENGINE_WRITE_ACTOR": "cron:x"}}),
    ("okengine-write-x", {"command": "/opt/hermes/.venv/bin/python",
                          "args": ["/opt/hermes/okengine-mcp/write_server.py", "--extra"],
                          "env": {"OKENGINE_WRITE_ACTOR": "cron:x"}}),
    # a baseline name is reserved even when its block looks exactly like a derived lane writer
    ("okengine-write-chat", {"command": "/opt/hermes/.venv/bin/python",
                             "args": ["/opt/hermes/okengine-mcp/write_server.py"],
                             "env": {"OKENGINE_WRITE_ACTOR": "cron:chat"}}),
    ("okengine-write-source-quality", {"command": "/opt/hermes/.venv/bin/python",
                                       "args": ["/opt/hermes/okengine-mcp/write_server.py"],
                                       "env": {"OKENGINE_WRITE_ACTOR": "cron:source-quality"}}),
])
def test_a_server_that_is_not_engine_generated_is_never_claimed(lw, cps, name, server):
    assert lw.generated_lane_actor(name, server, cps) is None


def test_both_derived_names_of_a_lane_are_engine_generated(lw, cps):
    lane = "okengine.predictions:prediction-falsification-search"
    server = {"command": "/opt/hermes/.venv/bin/python",
              "args": ["/opt/hermes/okengine-mcp/write_server.py"],
              "env": {"OKENGINE_WRITE_ACTOR": f"cron:{lane}"}}
    for name in (cps.contract_writer_name(lane), cps.unbounded_writer_name(lane)):
        assert lw.generated_lane_actor(name, server, cps) == f"cron:{lane}"


def test_stale_writer_errors_name_only_generated_writers_without_a_lane(lw, cps):
    cfg = yaml.safe_load("mcp_servers:\n" + _generated(cps, "mine") + _generated(cps, "theirs")
                         + _generated(cps, "pol", enforce=False) + "  my-tool:\n    command: x\n")
    errors = lw.stale_writer_errors(cfg, [{"name": "mine", "output_contract": {}}],
                                    {"capabilities": {"cron:pol": {}}}, cps)
    assert len(errors) == 1 and "okengine-write-theirs" in errors[0] and "cron:theirs" in errors[0]
    assert lw.stale_writer_errors({}, [], {}, cps) == []
    assert lw.stale_writer_errors({"mcp_servers": []}, [], {}, cps) == []


def test_an_unknown_lane_set_adds_policy_writers_but_never_prunes(lw, cps):
    text = "mcp_servers:\n" + _generated(cps, "theirs")
    out, changes = lw.reconcile(text, None, {"capabilities": {"cron:pol": {}}}, cps)
    assert changes == {"added": ["okengine-write-pol"], "rederived": [], "retired": [], "removed": []}
    assert "okengine-write-theirs:" in out


def test_a_config_without_mcp_servers_is_left_alone(lw, cps):
    text = "model: x\n"
    assert lw.reconcile(text, [{"name": "a", "output_contract": {}}], {}, cps) == (
        text, {"added": [], "rederived": [], "retired": [], "removed": []})


def test_the_pre_bound_name_of_a_wanted_lane_is_retired_and_its_marker_with_it(lw, cps):
    lane = "okengine.predictions:prediction-falsification-search"
    stale = cps.unbounded_writer_name(lane)
    text = "mcp_servers:\n" + _generated(cps, lane, name=stale) + "web:\n  backend: x\n"
    out, changes = lw.reconcile(text, [{"name": lane, "output_contract": {}}], {}, cps)
    assert changes == {"added": [cps.contract_writer_name(lane)], "rederived": [], "retired": [stale],
                       "removed": []}
    assert out.count(f"identity for cron:{lane}.") == 1
    assert f"  {stale}:" not in out and out.endswith("web:\n  backend: x\n")
    parsed = yaml.safe_load(out)
    assert parsed["web"] == {"backend": "x"}
    assert parsed["mcp_servers"][cps.contract_writer_name(lane)]["env"]["OKENGINE_WRITE_ACTOR"] \
        == f"cron:{lane}"


def test_removal_takes_exactly_the_block_and_its_own_marker(lw, cps):
    """Byte-exact: a blank line before an unmarked block, a column-0 comment, a body-less server
    and the next top-level section all survive; each removed block takes only its own marker."""
    text = ("mcp_servers:\n"
            "  first:\n    command: x\n\n"
            + _generated(cps, "theirs", marker=False)
            + "# operator note\n"
            + _generated(cps, "gone")
            + "  empty:\n"
            + _generated(cps, "also-gone", marker=False)
            + "web:\n  x: 1\n")
    out, changes = lw.reconcile(text, [], {}, cps)
    assert changes == {"added": [], "rederived": [], "retired": [],
                       "removed": ["okengine-write-theirs", "okengine-write-gone",
                                   "okengine-write-also-gone"]}
    assert out == ("mcp_servers:\n  first:\n    command: x\n\n# operator note\n  empty:\n"
                   "web:\n  x: 1\n")


def test_a_writer_is_added_inside_an_empty_mcp_servers_section(lw, cps):
    out, changes = lw.reconcile("model: m\nmcp_servers:\nweb:\n  x: 1\n",
                                [{"name": "a", "output_contract": {}}], {}, cps)
    assert changes["added"] == ["okengine-write-a"]
    assert out == ("model: m\nmcp_servers:\n" + lw._render("okengine-write-a", "cron:a", True, None)
                   + "web:\n  x: 1\n")


def test_an_unparseable_config_is_never_pruned(lw, cps):
    text = "mcp_servers:\n" + _generated(cps, "theirs") + "  bad: [unterminated\n"
    out, changes = lw.reconcile(text, [], {}, cps)
    assert (out, changes) == (text, {"added": [], "rederived": [], "retired": [], "removed": []})


def test_rendered_writer_carries_enforcement_and_write_tools(lw, cps):
    out, changes = lw.reconcile(
        "mcp_servers:\n",
        [{"name": "a", "output_contract": {}, "write_tools": ["update_entity"]},
         {"name": "b", "no_agent": True, "write_tools": ["flag_for_review"]}], {}, cps)
    servers = yaml.safe_load(out)["mcp_servers"]
    assert changes["added"] == ["okengine-write-a", "okengine-write-b"]
    assert servers["okengine-write-a"]["env"]["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce"
    assert servers["okengine-write-a"]["env"]["OKENGINE_WRITE_TOOLS"] == "update_entity"
    assert "OKENGINE_OUTPUT_CONTRACT_MODE" not in servers["okengine-write-b"]["env"]
    assert servers["okengine-write-b"]["tools"] == {"resources": False, "prompts": False}


def test_policy_loading(lw, tmp_path):
    assert lw.load_policy(tmp_path) == {}
    (tmp_path / ".okengine").mkdir()
    path = tmp_path / ".okengine" / "effective-policy.json"
    path.write_text('["not", "a", "mapping"]')
    assert lw.load_policy(tmp_path) == {}
    path.write_text('{"capabilities": {"cron:a": {}}}')
    assert lw.load_policy(tmp_path) == {"capabilities": {"cron:a": {}}}
    path.write_text("{")
    with pytest.raises(SystemExit, match="effective policy"):
        lw.load_policy(tmp_path)


def test_deployment_jobs(lw, cps, tmp_path):
    assert lw.deployment_jobs(tmp_path, cps) is None, "a bare runtime dir has no lane set"
    crons_only = tmp_path / "crons-only"
    (crons_only / "crons").mkdir(parents=True)
    assert isinstance(lw.deployment_jobs(crons_only, cps), list)
    yaml_only = tmp_path / "yaml-only"
    yaml_only.mkdir()
    (yaml_only / "pack.yaml").write_text("name: okpack-y\nversion: 0.1.0\n")
    assert isinstance(lw.deployment_jobs(yaml_only, cps), list)
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")])
    assert "okpack-alpha-brief" in {j["name"] for j in lw.deployment_jobs(alpha, cps)}
    (alpha / "pack.yaml").write_text("name: [unterminated\n")
    with pytest.raises(SystemExit, match="RuntimeError"):
        lw.deployment_jobs(alpha, cps)


def test_an_unloadable_split_module_is_fatal(lw, tmp_path, monkeypatch):
    broken = tmp_path / "cron_pack_split.py"
    broken.write_text("raise ImportError('broken')\n")
    monkeypatch.setattr(lw, "CPS_PATH", broken)
    with pytest.raises(SystemExit, match="cannot load the writer-name derivation"):
        lw.load_split()


def test_main_dry_run_writes_nothing_and_reports(lw, cps, tmp_path, capsys):
    config = "mcp_servers:\n" + _generated(cps, "okpack-beta-brief")
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")], config=config)
    cfg = alpha / ".hermes-data" / "config.yaml"
    assert lw.main(["--pack", str(alpha), "--dry-run"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert cfg.read_text() == config
    assert report["lane_set_known"] is True
    assert report["removed"] == [cps.contract_writer_name("okpack-beta-brief")]
    assert cps.contract_writer_name("okpack-alpha-brief") in report["added"]
    assert capsys.readouterr().out == ""
    lw.main(["--pack", str(alpha), "--dry-run"])
    assert capsys.readouterr().out == json.dumps(report, indent=2) + "\n"


def test_main_requires_the_deployment(lw, capsys):
    with pytest.raises(SystemExit) as exc:
        lw.main([])
    assert exc.value.code == 2 and "--pack" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_the_script_runs_as_a_command(cps, tmp_path):
    config = "mcp_servers:\n" + _generated(cps, "okpack-beta-brief")
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")], config=config)
    r = subprocess.run([sys.executable, str(SCRIPTS / "lane_writers.py"), "--pack", str(alpha),
                        "--dry-run"], capture_output=True, text=True, timeout=120,
                       env=_env(REPO))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["removed"] == [cps.contract_writer_name("okpack-beta-brief")]


def test_main_writes_and_says_what_changed(lw, cps, tmp_path, capsys):
    lane = "okengine.predictions:prediction-falsification-search"
    config = ("mcp_servers:\n" + _generated(cps, "okpack-beta-brief")
              + _generated(cps, lane, name=cps.unbounded_writer_name(lane)))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(config)
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "effective-policy.json").write_text(
        json.dumps({"capabilities": {f"cron:{lane}": {}}}))
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")])
    (alpha / ".okengine").mkdir()
    (alpha / ".okengine" / "effective-policy.json").write_text(
        json.dumps({"capabilities": {f"cron:{lane}": {}}}))
    assert lw.main(["--pack", str(alpha), "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "retired 1 pre-bound name(s)" in out and "removed 1 writer(s)" in out
    servers = yaml.safe_load(cfg.read_text())["mcp_servers"]
    assert cps.contract_writer_name(lane) in servers
    assert cps.contract_writer_name("okpack-beta-brief") not in servers
    assert lw.main(["--pack", str(alpha), "--config", str(cfg)]) == 0
    assert capsys.readouterr().out == "", "an unchanged config is not rewritten or reported"


def test_hand_added_blocks_are_kept_in_place(lw, cps):
    text = ("mcp_servers:\n  my-tool:\n    command: /usr/bin/my-tool\n"
            + _generated(cps, "theirs") + "  last:\n    command: x\n")
    out, changes = lw.reconcile(text, [], {}, cps)
    assert changes == {"added": [], "rederived": [], "retired": [], "removed": ["okengine-write-theirs"]}
    assert out == ("mcp_servers:\n  my-tool:\n    command: /usr/bin/my-tool\n"
                   "  last:\n    command: x\n")


def test_a_fleet_gate_failure_is_reported_as_a_render_failure(lw, cps, tmp_path):
    bad = _lane("okpack-alpha-brief")
    del bad["max_iterations"]
    alpha = _pack(tmp_path, "okpack-alpha", [bad])
    with pytest.raises(SystemExit, match="cannot generate this deployment's cron fleet") as exc:
        lw.deployment_jobs(alpha, cps)
    assert "max_iterations" in str(exc.value)


def test_main_reports_additions_alone(lw, cps, tmp_path, capsys):
    alpha = _pack(tmp_path, "okpack-alpha", [_lane("okpack-alpha-brief")],
                  config="mcp_servers:\n")
    assert lw.main(["--pack", str(alpha)]) == 0
    out = capsys.readouterr().out
    assert "policy-bound cron writer(s))" in out
    assert "retired" not in out and "removed" not in out


def test_a_correctly_named_wanted_writer_is_left_byte_for_byte(lw, cps):
    text = "mcp_servers:\n" + _generated(cps, "mine") + "web:\n  x: 1\n"
    assert lw.reconcile(text, [{"name": "mine", "output_contract": {}}], {}, cps) == (
        text, {"added": [], "rederived": [], "retired": [], "removed": []})


def test_a_pre_bound_name_that_sorts_below_its_bound_name_is_retired_too(lw, cps):
    lane = "okpack-alpha-ab-0-zz-long-tail-name"
    stale, bound = cps.unbounded_writer_name(lane), cps.contract_writer_name(lane)
    assert stale < bound, "fixture: the comparison must not be order-dependent"
    text = "mcp_servers:\n" + _generated(cps, lane, name=stale)
    out, changes = lw.reconcile(text, [], {"capabilities": {f"cron:{lane}": {}}}, cps)
    assert changes == {"added": [bound], "rederived": [], "retired": [stale], "removed": []}


def test_leading_lines_of_the_section_are_kept(lw, cps):
    text = "mcp_servers:\n\n    # stray\n" + _generated(cps, "gone") + "web: {}\n"
    out, changes = lw.reconcile(text, [], {}, cps)
    assert changes["removed"] == ["okengine-write-gone"]
    assert out == "mcp_servers:\n\n    # stray\nweb: {}\n"


# --- disabled lanes: one definition of "the lanes this deployment runs" ---------------------------
# ensure-runtime renders from the in-memory fleet, which keeps `enabled: false` lanes; the jobs
# deploy gate judges against the deployed artifact, which drops them. Both sides must agree, or a
# disabled contracted lane gets a writer the gate then rejects on every deploy (a deadlock).

def _disabled_fleet_pack(root: Path, config: str | None = None) -> Path:
    """An enabled contracted lane, a disabled contracted lane and a disabled write_tools lane."""
    tools = _lane("okpack-alpha-tools", enabled=False, write_tools=["update_entity"],
                  enabled_toolsets=["web"])
    del tools["output_contract"]
    return _pack(root, "okpack-alpha", [_lane("okpack-alpha-brief"),
                                        _lane("okpack-alpha-watch", enabled=False), tools],
                 config=config)


def _deployed_jobs(cps, pack: Path) -> list:
    """The jobs the deploy gate reads: the fleet as _dump_jobs writes cron-plus-jobs.json."""
    fleet, _ = cps.build_fleet(pack)
    return json.loads(cps._dump_jobs(fleet))["jobs"]


def test_disabled_lanes_get_no_writer_and_the_gate_accepts_what_ensure_runtime_renders(
        lw, cps, tmp_path):
    pack = _disabled_fleet_pack(tmp_path)
    jobs = lw.deployment_jobs(pack, cps)
    assert {"okpack-alpha-watch", "okpack-alpha-tools"} <= {j["name"] for j in jobs}, \
        "fixture: the in-memory fleet keeps the disabled lanes"
    actors, tools = lw.wanted_actors(jobs, {}, cps)
    assert "cron:okpack-alpha-brief" in actors
    assert "cron:okpack-alpha-watch" not in actors, "a disabled contracted lane got a writer"
    assert "cron:okpack-alpha-tools" not in actors and tools == {}, \
        "a disabled write_tools lane got a writer"

    rendered, changes = lw.reconcile("mcp_servers:\n", jobs, {}, cps)
    assert cps.contract_writer_name("okpack-alpha-watch") not in changes["added"]
    errors = lw.stale_writer_errors(yaml.safe_load(rendered), _deployed_jobs(cps, pack), {}, cps)
    assert errors == [], "the jobs gate rejects a writer ensure-runtime just rendered"


def test_a_writer_for_a_lane_that_was_later_disabled_is_flagged_then_pruned(lw, cps, tmp_path):
    stale = cps.contract_writer_name("okpack-alpha-watch")
    text = "mcp_servers:\n" + _generated(cps, "okpack-alpha-brief") + _generated(
        cps, "okpack-alpha-watch")
    pack = _disabled_fleet_pack(tmp_path)
    deployed = _deployed_jobs(cps, pack)
    errors = lw.stale_writer_errors(yaml.safe_load(text), deployed, {}, cps)
    assert len(errors) == 1 and stale in errors[0], errors

    pruned, changes = lw.reconcile(text, lw.deployment_jobs(pack, cps), {}, cps)
    assert changes["removed"] == [stale] and stale not in changes["added"], changes
    assert lw.stale_writer_errors(yaml.safe_load(pruned), deployed, {}, cps) == []


def test_a_policy_capability_for_a_disabled_lane_still_gets_its_writer(lw, cps, tmp_path):
    """Policy `cron:` capabilities are not lane-derived and stay as they are on both sides."""
    pack = _disabled_fleet_pack(tmp_path)
    policy = {"capabilities": {"cron:okpack-alpha-watch": {}}}
    rendered, changes = lw.reconcile("mcp_servers:\n", lw.deployment_jobs(pack, cps), policy, cps)
    assert cps.contract_writer_name("okpack-alpha-watch") in changes["added"]
    env = yaml.safe_load(rendered)["mcp_servers"][cps.contract_writer_name("okpack-alpha-watch")]
    assert "OKENGINE_OUTPUT_CONTRACT_MODE" not in env["env"], "a disabled lane's contract leaked"
    assert lw.stale_writer_errors(yaml.safe_load(rendered), _deployed_jobs(cps, pack), policy,
                                  cps) == []


def _deploy_jobs(engine: Path, pack: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """deploy-cron-plus-jobs.sh exactly as deploy.sh runs it: bash, from the engine root, with
    CRON_PACK_DIR and no test PYTHONPATH. A stub `docker` that finds no container stands in for the gateway, so
    the run ends at the container lookup right AFTER the gate and never touches a live stack."""
    stub = tmp_path / "stub-bin"
    stub.mkdir(exist_ok=True)
    (stub / "docker").write_text("#!/bin/sh\nexit 0\n")
    (stub / "docker").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(PATH=f"{stub}:{os.environ['PATH']}", CRON_PACK_DIR=str(pack),
               OKENGINE_HOST_PYTHON=sys.executable, HERMES_UID=str(os.getuid()),
               HERMES_GID=str(os.getgid()))
    return subprocess.run(["bash", str(engine / "scripts" / "deploy-cron-plus-jobs.sh")],
                          cwd=engine, capture_output=True, text=True, timeout=180, env=env)


_GATE_ERROR = "write server(s) for lanes this deployment does not run"


@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_the_jobs_deploy_gate_accepts_ensure_runtime_with_a_disabled_contracted_lane(
        tmp_path, cps):
    """Real invocation of both sides: ensure-runtime.sh renders, deploy-cron-plus-jobs.sh gates."""
    engine = _engine_copy(tmp_path)
    pack = _disabled_fleet_pack(tmp_path)
    r = _ensure_runtime(engine, pack)
    assert r.returncode == 0, r.stdout + r.stderr

    d = _deploy_jobs(engine, pack, tmp_path)
    assert _GATE_ERROR not in d.stderr, d.stderr
    assert "expanded 0 @morning brief(s)" in d.stdout, d.stdout + d.stderr
    assert "no running gateway container found" in d.stderr, "must stop only at the stub gateway"
    servers = _servers(pack)
    assert cps.contract_writer_name("okpack-alpha-brief") in servers
    assert cps.contract_writer_name("okpack-alpha-watch") not in servers
    assert cps.contract_writer_name("okpack-alpha-tools") not in servers


@pytest.mark.integration
@pytest.mark.timeout(120)   # real ensure-runtime/jobs-deploy subprocesses; one hit the 30s default on a loaded runner (main 13016)
def test_the_jobs_deploy_gate_refuses_a_writer_for_a_disabled_lane_until_ensure_runtime_prunes(
        tmp_path, cps):
    """Negative fixture for the gate in its production invocation, then the producer's repair."""
    engine = _engine_copy(tmp_path)
    stale = cps.contract_writer_name("okpack-alpha-watch")
    pack = _disabled_fleet_pack(tmp_path)
    assert _ensure_runtime(engine, pack).returncode == 0
    cfg = pack / ".hermes-data" / "config.yaml"
    if stale not in _servers(pack):      # the writer an earlier deploy left for the lane
        cfg.write_text(cfg.read_text().replace(
            "mcp_servers:\n", "mcp_servers:\n" + _generated(cps, "okpack-alpha-watch"), 1))
    assert stale in _servers(pack), "fixture: a writer for the now-disabled lane is registered"
    d = _deploy_jobs(engine, pack, tmp_path)
    assert d.returncode == 1 and _GATE_ERROR in d.stderr and stale in d.stderr, d.stderr
    assert "expanded" not in d.stdout

    r = _ensure_runtime(engine, pack)
    assert r.returncode == 0, r.stdout + r.stderr
    assert stale not in _servers(pack)
    d = _deploy_jobs(engine, pack, tmp_path)
    assert _GATE_ERROR not in d.stderr, d.stderr
    assert "expanded 0 @morning brief(s)" in d.stdout, d.stdout + d.stderr
