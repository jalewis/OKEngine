"""okengine#1042 [3]/[18] and [23]: a generated lane writer follows its lane's CURRENT definition.

ensure-runtime registered `okengine-write-<lane>` once and never revisited it: a pack release that
widened or narrowed the lane's `write_tools`, or added or dropped its `output_contract`, left the
registered env as it was. write_server takes OKENGINE_WRITE_TOOLS as the lane's whole tool surface,
so the new tool was silently unavailable (or a narrowing silently unapplied), and the jobs deploy
gate did not notice. These tests pin the re-derivation, what it must leave alone, and the gate.
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
PY, WS = "/opt/hermes/.venv/bin/python", "/opt/hermes/okengine-mcp/write_server.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lw():
    sys.path.insert(0, str(REPO / "src"))
    return _load("lane_writers_under_test_1042", SCRIPTS / "lane_writers.py")


@pytest.fixture(scope="module")
def cps():
    sys.path.insert(0, str(REPO / "src"))
    return _load("cps_under_test_1042", SCRIPTS / "cron_pack_split.py")


def _block(cps, lane: str, *env_lines: str, marker: bool = True) -> str:
    """A generated writer as a live config holds it after ensure-runtime's later steps."""
    name = cps.contract_writer_name(lane)
    return ((f"  # Engine-managed server-bound identity for cron:{lane}.\n" if marker else "")
            + f"  {name}:\n    command: {PY}\n    args:\n    - {WS}\n"
            "    tools:\n      resources: false\n      prompts: false\n    env:\n"
            "      OKENGINE_PACK: okpack-alpha\n"
            f"      OKENGINE_WRITE_ACTOR: cron:{lane}\n"
            + "".join(f"      {line}\n" for line in env_lines)
            + "      OKENGINE_CRON_JOBS: /opt/data/cron-plus/jobs.json\n\n")


def _env_of(cps, text: str, lane: str) -> dict:
    return yaml.safe_load(text)["mcp_servers"][cps.contract_writer_name(lane)]["env"]


def _job(name: str, contract: bool = True, tools=None) -> dict:
    job = {"name": name}
    if contract:
        job["output_contract"] = {}
    if tools is not None:
        job["write_tools"] = tools
    return job


# --- the acceptance set: what must change ---------------------------------------------------------

def test_a_widened_write_tools_reaches_the_registered_writer(lw, cps):
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce",
                                     "OKENGINE_WRITE_TOOLS: create_entity,update_entity")
    out, changes = lw.reconcile(
        text, [_job("l", tools=["create_entity", "update_entity", "append_to_section"])], {}, cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")], changes
    assert _env_of(cps, out, "l")["OKENGINE_WRITE_TOOLS"] == \
        "create_entity,update_entity,append_to_section", "the lane's new tool never reaches its writer"


def test_a_narrowing_added_to_a_full_surface_writer_is_applied(lw, cps):
    """Pack v1 had no write_tools (every tool); v2 narrows the lane. The fence must land."""
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce")
    out, changes = lw.reconcile(text, [_job("l", tools=["update_entity"])], {}, cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")]
    assert _env_of(cps, out, "l")["OKENGINE_WRITE_TOOLS"] == "update_entity"


def test_a_dropped_write_tools_removes_the_stale_fence(lw, cps):
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce",
                                     "OKENGINE_WRITE_TOOLS: update_entity")
    out, changes = lw.reconcile(text, [_job("l")], {}, cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")]
    assert "OKENGINE_WRITE_TOOLS" not in _env_of(cps, out, "l")


def test_a_contract_added_to_a_policy_registered_lane_turns_enforcement_on(lw, cps):
    """The live okcti-test shape: registered from a policy cron: capability, later contracted."""
    text = "mcp_servers:\n" + _block(cps, "l")
    out, changes = lw.reconcile(text, [_job("l")], {"capabilities": {"cron:l": {}}}, cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")]
    assert _env_of(cps, out, "l")["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce"


def test_a_dropped_contract_drops_the_enforce_line(lw, cps):
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce",
                                     "OKENGINE_WRITE_TOOLS: flag_for_review")
    out, changes = lw.reconcile(text, [_job("l", contract=False, tools=["flag_for_review"])], {}, cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")]
    assert _env_of(cps, out, "l") == {
        "OKENGINE_PACK": "okpack-alpha", "OKENGINE_WRITE_ACTOR": "cron:l",
        "OKENGINE_WRITE_TOOLS": "flag_for_review", "OKENGINE_CRON_JOBS": "/opt/data/cron-plus/jobs.json"}


def test_an_operator_report_mode_on_a_generated_writer_is_re_derived(lw, cps):
    """A generated block is engine-owned: a hand-set mode on it is drift like any other."""
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: report")
    out, changes = lw.reconcile(text, [_job("l")], {}, cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")]
    assert _env_of(cps, out, "l")["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce"


# --- the acceptance set: what must NOT change --------------------------------------------------

def test_a_current_writer_is_left_byte_for_byte(lw, cps):
    text = ("mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce",
                                      "OKENGINE_WRITE_TOOLS: update_entity,create_entity")
            + _block(cps, "p") + "web:\n  x: 1\n")
    jobs = [_job("l", tools=["create_entity", "update_entity"])]
    assert lw.reconcile(text, jobs, {"capabilities": {"cron:p": {}}}, cps) == (
        text, {"added": [], "rederived": [], "retired": [], "removed": []}), \
        "a reordered tool list is the same surface; nothing to rewrite"


def test_re_derivation_keeps_every_other_line_in_place(lw, cps):
    """The marker, OKENGINE_PACK, TZ (bound by a later ensure-runtime step), an operator's own key
    and the neighbours stay exactly put."""
    text = ("mcp_servers:\n  my-tool:\n    command: /usr/bin/my-tool\n"
            + _block(cps, "l", "OKENGINE_WRITE_TOOLS: old", "MY_OWN_KEY: kept",
                     'TZ: "America/New_York"')
            + "  last:\n    command: x\nweb: {}\n")
    out, _ = lw.reconcile(text, [_job("l", tools=["patch_entity"])], {}, cps)
    assert out == text.replace(
        "      OKENGINE_WRITE_ACTOR: cron:l\n      OKENGINE_WRITE_TOOLS: old\n",
        "      OKENGINE_WRITE_ACTOR: cron:l\n      OKENGINE_OUTPUT_CONTRACT_MODE: enforce\n"
        "      OKENGINE_WRITE_TOOLS: patch_entity\n")


def test_duplicate_managed_lines_collapse_to_the_derived_one(lw, cps):
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_WRITE_TOOLS: a",
                                     "OKENGINE_WRITE_TOOLS: b")
    out, changes = lw.reconcile(text, [_job("l", tools=["patch_entity"])], {}, cps)
    assert out.count("OKENGINE_WRITE_TOOLS") == 1 and _env_of(cps, out, "l")[
        "OKENGINE_WRITE_TOOLS"] == "patch_entity"


def test_a_flow_style_env_block_is_rendered_afresh_with_one_marker(lw, cps):
    name = cps.contract_writer_name("l")
    text = ("mcp_servers:\n  # Engine-managed server-bound identity for cron:l.\n"
            f"  {name}:\n    command: {PY}\n    args: [{WS}]\n"
            "    env: {OKENGINE_WRITE_ACTOR: 'cron:l'}\n")
    out, changes = lw.reconcile(text, [_job("l", tools=["flag_for_review"])], {}, cps)
    assert changes["rederived"] == [name]
    assert out.count("Engine-managed server-bound identity for cron:l.") == 1, out
    env = _env_of(cps, out, "l")
    assert env["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce" and env["OKENGINE_WRITE_TOOLS"] == "flag_for_review"


def test_a_flow_style_block_without_its_marker_gains_exactly_one(lw, cps):
    name = cps.contract_writer_name("l")
    text = (f"mcp_servers:\n  {name}:\n    command: {PY}\n    args: [{WS}]\n"
            "    env: {OKENGINE_WRITE_ACTOR: 'cron:l'}\n")
    out, _ = lw.reconcile(text, [_job("l")], {}, cps)
    assert out.count("Engine-managed server-bound identity for cron:l.") == 1, out


@pytest.mark.parametrize("before", [
    "  my-tool:\n    command: /usr/bin/my-tool\n",      # a neighbour's last line sorts above the marker
    "  # 1 operator note\n",                            # a comment that sorts below it
])
@pytest.mark.parametrize("marker", [True, False])
def test_a_fresh_render_takes_only_its_own_marker_never_a_neighbours_line(lw, cps, before, marker):
    name = cps.contract_writer_name("l")
    text = ("mcp_servers:\n" + before
            + ("  # Engine-managed server-bound identity for cron:l.\n" if marker else "")
            + f"  {name}:\n    command: {PY}\n    args: [{WS}]\n"
            "    env: {OKENGINE_WRITE_ACTOR: 'cron:l'}\n")
    out, _ = lw.reconcile(text, [_job("l")], {}, cps)
    assert out.startswith("mcp_servers:\n" + before), out
    assert out.count("Engine-managed server-bound identity for cron:l.") == 1, out


@pytest.mark.parametrize("server", [
    {"command": "/usr/local/bin/python", "args": [WS],
     "env": {"OKENGINE_WRITE_ACTOR": "cron:l", "OKENGINE_WRITE_TOOLS": "old"}},
    {"command": PY, "args": [WS], "env": {"OKENGINE_WRITE_ACTOR": "chat:api_server"}},
])
def test_a_server_that_is_not_engine_generated_is_never_re_derived(lw, cps, server):
    name = cps.contract_writer_name("l")
    text = "mcp_servers:\n  " + yaml.safe_dump({name: server}).replace("\n", "\n  ").rstrip() + "\n"
    out, changes = lw.reconcile(text, [_job("l", tools=["patch_entity"])], {}, cps)
    assert changes["rederived"] == []
    assert yaml.safe_load(out)["mcp_servers"][name] == server


def test_an_unknown_lane_set_never_re_derives_on_a_guess(lw, cps):
    """jobs None (a bare runtime): the lane definitions are unknown, so a writer's contract mode
    and tools cannot be judged, just as nothing is pruned."""
    text = "mcp_servers:\n" + _block(cps, "p", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce")
    assert lw.reconcile(text, None, {"capabilities": {"cron:p": {}}}, cps) == (
        text, {"added": [], "rederived": [], "retired": [], "removed": []})


# --- the deploy gate --------------------------------------------------------------------------

def test_the_gate_names_a_stale_writer_and_what_differs(lw, cps):
    config = yaml.safe_load("mcp_servers:\n" + _block(
        cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce", "OKENGINE_WRITE_TOOLS: create_entity"))
    [error] = lw.stale_writer_errors(
        config, [_job("l", tools=["create_entity", "update_entity"])], {}, cps)
    assert cps.contract_writer_name("l") in error and "'cron:l'" in error
    assert ("OKENGINE_WRITE_TOOLS is 'create_entity', the lane declares "
            "'create_entity,update_entity'") in error, error
    assert "re-derive" in error


def test_the_gate_reports_a_missing_enforce_line(lw, cps):
    config = yaml.safe_load("mcp_servers:\n" + _block(cps, "l"))
    [error] = lw.stale_writer_errors(config, [_job("l")], {}, cps)
    assert "OKENGINE_OUTPUT_CONTRACT_MODE is None, the lane declares 'enforce'" in error


@pytest.mark.parametrize("lane", ["okpack-alpha-ab-0-zz-long-tail-name",
                                  "okpack-alpha-abcdefghijklmnopqrstuvwxyz"])
def test_the_gate_ignores_retired_names_unwanted_lanes_and_odd_configs(lw, cps, lane):
    """The pre-bound name sorts below its bound name for one lane and above it for the other, so
    the comparison cannot be order-dependent."""
    stale = cps.unbounded_writer_name(lane)
    assert stale != cps.contract_writer_name(lane), "fixture: the pre-bound name differs"
    servers = {stale: {"command": PY, "args": [WS], "env": {"OKENGINE_WRITE_ACTOR": f"cron:{lane}"}},
               "mine": {"command": "x"}}
    assert lw.drifted_lane_writers({"mcp_servers": servers}, {f"cron:{lane}": True}, {}, cps) == []
    assert lw.drifted_lane_writers({"mcp_servers": servers}, {}, {}, cps) == []
    assert lw.drifted_lane_writers({"mcp_servers": None}, {"cron:l": True}, {}, cps) == []
    assert lw.drifted_lane_writers(None, {"cron:l": True}, {}, cps) == []


def test_env_drift_reads_a_missing_or_malformed_env_as_empty(lw):
    want = {lw.CONTRACT_KEY: "enforce", lw.TOOLS_KEY: None}
    assert lw.env_drift({"env": "nope"}, want) == [(lw.CONTRACT_KEY, None, "enforce")]
    assert lw.env_drift(None, want) == [(lw.CONTRACT_KEY, None, "enforce")]
    assert lw.env_drift({"env": {lw.CONTRACT_KEY: "enforce", lw.TOOLS_KEY: " , "}}, want) == []


def _deployed_jobs(cps, pack: Path) -> list:
    fleet, _ = cps.build_fleet(pack)
    return json.loads(cps._dump_jobs(fleet))["jobs"]


def _lane(name: str, **extra) -> dict:
    return {"name": name, "schedule": {"kind": "cron", "expr": "0 3 * * *"},
            "prompt": "write it", "enabled_toolsets": ["okengine-write"],
            "output_contract": CONTRACT, "max_iterations": 10, "timeout": 600,
            "expect": {"may_idle": {"max_idle_runs": 10}}, **extra}


def _pack(root: Path, lanes: list[dict]) -> Path:
    pack = root / "okpack-alpha"
    (pack / "crons").mkdir(parents=True, exist_ok=True)
    (pack / "pack.yaml").write_text("name: okpack-alpha\nversion: 0.1.0\n")
    (pack / "crons" / "domain-crons.json").write_text(json.dumps(lanes))
    return pack


def test_the_gate_accepts_what_reconcile_renders_from_the_same_pack(lw, cps, tmp_path):
    """Both sides read the lane: ensure-runtime the in-memory fleet, the gate the deployed
    artifact. A tool list or contract they read differently would deadlock every deploy."""
    tools = _lane("okpack-alpha-tools", write_tools=["update_entity", "append_to_section"],
                  enabled_toolsets=["web"])
    del tools["output_contract"]
    pack = _pack(tmp_path, [_lane("okpack-alpha-brief", write_tools=["create_entity"]), tools])
    stale = ("mcp_servers:\n" + _block(cps, "okpack-alpha-brief")
             + _block(cps, "okpack-alpha-tools", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce"))
    deployed = _deployed_jobs(cps, pack)
    assert len(lw.stale_writer_errors(yaml.safe_load(stale), deployed, {}, cps)) == 2
    rendered, changes = lw.reconcile(stale, lw.deployment_jobs(pack, cps), {}, cps)
    assert len(changes["rederived"]) == 2
    assert lw.stale_writer_errors(yaml.safe_load(rendered), deployed, {}, cps) == []
    assert lw.reconcile(rendered, lw.deployment_jobs(pack, cps), {}, cps)[0] == rendered


def test_main_says_which_writers_it_re_derived(lw, cps, tmp_path, capsys):
    pack = _pack(tmp_path, [_lane("okpack-alpha-brief", write_tools=["create_entity"])])
    (pack / ".hermes-data").mkdir()
    cfg = pack / ".hermes-data" / "config.yaml"
    cfg.write_text("mcp_servers:\n" + _block(cps, "okpack-alpha-brief"))
    assert lw.main(["--pack", str(pack), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["rederived"] == [
        cps.contract_writer_name("okpack-alpha-brief")]
    assert lw.main(["--pack", str(pack)]) == 0
    out = capsys.readouterr().out
    assert "re-derived 1 writer(s)" in out and cps.contract_writer_name("okpack-alpha-brief") in out


# --- real invocation: ensure-runtime.sh renders, deploy-cron-plus-jobs.sh gates ---------------

def _engine_copy(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    for part in ("config", "scripts", "src", "prompts", "extensions"):
        shutil.copytree(REPO / part, engine / part,
                        ignore=shutil.ignore_patterns("__pycache__", "cron-plus-jobs.json"))
    return engine


def _ensure_runtime(engine: Path, pack: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1", PYTHONPATH=str(engine / "src"))
    return subprocess.run(["bash", str(engine / "scripts" / "ensure-runtime.sh"), str(pack),
                           "--fix-perms"], capture_output=True, text=True, timeout=120, env=env)


def _deploy_jobs(engine: Path, pack: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """As deploy.sh runs it: bash, the engine root as cwd, no test PYTHONPATH. A stub docker that
    finds no gateway ends the run right after the gate, so no live stack is touched."""
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


def _widened_after_first_render(tmp_path, cps):
    """A deployment rendered at pack v1 (`write_tools: [create_entity]`), then upgraded to pack v2,
    which adds append_to_section to the lane. Returns ``(engine, pack, writer name)``."""
    engine = _engine_copy(tmp_path)
    lane = "okpack-alpha-brief"
    pack = _pack(tmp_path, [_lane(lane, write_tools=["create_entity"])])
    assert _ensure_runtime(engine, pack).returncode == 0
    name = cps.contract_writer_name(lane)
    cfg = pack / ".hermes-data" / "config.yaml"
    assert yaml.safe_load(cfg.read_text())["mcp_servers"][name]["env"][
        "OKENGINE_WRITE_TOOLS"] == "create_entity"
    _pack(tmp_path, [_lane(lane, write_tools=["create_entity", "append_to_section"])])   # pack v2
    return engine, pack, name


@pytest.mark.integration
@pytest.mark.timeout(120)   # ~8s per ensure-runtime and ~6s per jobs deploy on CI (pipeline 12975)
def test_the_jobs_deploy_refuses_a_writer_its_lane_has_outgrown(tmp_path, cps):
    """Negative fixture for the gate in its production invocation. Split from the repair test
    below: one test doing both overran the suite's 30s per-test budget on CI."""
    engine, pack, name = _widened_after_first_render(tmp_path, cps)
    d = _deploy_jobs(engine, pack, tmp_path)
    assert d.returncode == 1 and "is stale against its lane" in d.stderr and name in d.stderr, \
        d.stdout + d.stderr
    assert "expanded" not in d.stdout, "the live store must not be written past a stale writer"


@pytest.mark.integration
@pytest.mark.timeout(120)   # ~8s per ensure-runtime and ~6s per jobs deploy on CI (pipeline 12975)
def test_ensure_runtime_re_derives_the_writer_and_the_jobs_deploy_then_passes(tmp_path, lw, cps):
    engine, pack, name = _widened_after_first_render(tmp_path, cps)
    r = _ensure_runtime(engine, pack)
    assert r.returncode == 0 and "re-derived 1 writer(s)" in r.stdout, r.stdout + r.stderr
    cfg = pack / ".hermes-data" / "config.yaml"
    env = yaml.safe_load(cfg.read_text())["mcp_servers"][name]["env"]
    assert env["OKENGINE_WRITE_TOOLS"] == "create_entity,append_to_section"
    assert env["OKENGINE_PACK"] == "okpack-alpha" and env["OKENGINE_OUTPUT_CONTRACT_MODE"] == "enforce"
    text = cfg.read_text()
    assert lw.reconcile(text, lw.deployment_jobs(pack, cps), lw.load_policy(pack), cps)[0] == text, \
        "re-derivation must be idempotent over what ensure-runtime leaves"
    d = _deploy_jobs(engine, pack, tmp_path)
    assert "is stale against its lane" not in d.stderr, d.stderr
    assert "expanded 0 @morning brief(s)" in d.stdout, d.stdout + d.stderr


# --- okengine#1042 [23]: the #969 ownership gate, executed as deploy.sh runs it ----------------
# test_the_jobs_deploy_checks_ownership_before_the_live_store_is_written only reads the script's
# text, so a renamed heredoc variable or a broken import would skip the gate unnoticed.

def _foreign_server() -> str:
    """An operator-added server (its own interpreter, so ensure-runtime never claims it) that is
    registered for ANOTHER identity."""
    return ("  okengine-write-okpack-other-x:\n    command: /usr/local/bin/python\n    args:\n"
            f"    - {WS}\n    env:\n      OKENGINE_WRITE_ACTOR: cron:okpack-other:x\n")


@pytest.mark.integration
@pytest.mark.timeout(120)   # ~8s per ensure-runtime and ~6s per jobs deploy on CI (pipeline 12975)
def test_the_ownership_gate_refuses_a_lane_binding_another_identitys_server(tmp_path):
    engine = _engine_copy(tmp_path)
    lane = _lane("okpack-alpha-brief")
    pack = _pack(tmp_path, [lane])
    assert _ensure_runtime(engine, pack).returncode == 0
    cfg = pack / ".hermes-data" / "config.yaml"
    cfg.write_text(cfg.read_text().replace("mcp_servers:\n", "mcp_servers:\n" + _foreign_server(), 1))
    d = _deploy_jobs(engine, pack, tmp_path)
    assert "owned by another identity" not in d.stderr, "fixture: nothing binds it yet"
    assert "expanded 0 @morning brief(s)" in d.stdout, d.stdout + d.stderr

    _pack(tmp_path, [_lane("okpack-alpha-brief", enabled_toolsets=[
        "okengine-write", "okengine-write-okpack-other-x"])])
    d = _deploy_jobs(engine, pack, tmp_path)
    assert d.returncode == 1, d.stdout + d.stderr
    assert "owned by another identity" in d.stderr and "okengine-write-okpack-other-x" in d.stderr \
        and "'cron:okpack-other:x'" in d.stderr, d.stderr
    assert "expanded" not in d.stdout, "the live store must not be written past a foreign binding"


# --- okengine#1044 [8]/[18]: a malformed write_tools never re-derives a writer to the full surface --

@pytest.mark.parametrize("value", ["update_entity", [], [" "], ["update_entiy"]])
def test_a_malformed_write_tools_refuses_instead_of_widening_the_writer(lw, cps, value):
    """The finding's reproduction: a narrowed writer, then a pack release whose `write_tools` is a
    bare string, an empty list, blanks or a typo. Reconcile used to read all four as "no narrowing"
    (or narrow to nothing) and strip OKENGINE_WRITE_TOOLS; it must refuse and name the lane."""
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_OUTPUT_CONTRACT_MODE: enforce",
                                     "OKENGINE_WRITE_TOOLS: update_entity")
    with pytest.raises(SystemExit) as raised:
        lw.reconcile(text, [_job("l", tools=value)], {}, cps)
    assert "'l'" in str(raised.value) and "write_tools" in str(raised.value), raised.value


def test_the_jobs_gate_refuses_a_malformed_write_tools_too(lw, cps):
    """The deploy gate reads the same declaration; it may not agree with a widened writer."""
    config = yaml.safe_load("mcp_servers:\n" + _block(cps, "l", "OKENGINE_WRITE_TOOLS: update_entity"))
    with pytest.raises(SystemExit):
        lw.stale_writer_errors(config, [_job("l", contract=False, tools="update_entity")], {}, cps)


def test_an_absent_write_tools_still_drops_the_fence(lw, cps):
    """Negative fixture: removing the key is a legitimate widening, not a malformed declaration."""
    text = "mcp_servers:\n" + _block(cps, "l", "OKENGINE_WRITE_TOOLS: update_entity")
    out, changes = lw.reconcile(text, [_job("l", contract=False)], {"capabilities": {"cron:l": {}}},
                                cps)
    assert changes["rederived"] == [cps.contract_writer_name("l")]
    assert "OKENGINE_WRITE_TOOLS" not in _env_of(cps, out, "l")
