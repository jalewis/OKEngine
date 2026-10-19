"""okengine#991 [8]: the running gateway's BAKED policy catalog must declare the chat capability
whenever ensure-runtime has bound the actor-bound chat writer.

The two halves ride different deploy surfaces: ensure-runtime reconciles config.yaml (stage +
recreate) while the `chat:api_server` capability lives in the catalog baked into the gateway
image. A stage-only roll onto a pre-#983 image binds the chat writer to an actor the running
write path has no capability for, so every chat write-back is rejected, and every other gate
stays green. Only an engine-catalog chat capability exists in the effective policy (a pack may
replace one, never introduce one: policy_plane.compose_documents), so the baked catalog is the
whole answer.
"""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"
CATALOG = REPO / "config" / "policy" / "catalog.yaml"

_WRITER = {"command": "/opt/hermes/.venv/bin/python",
           "args": ["/opt/hermes/okengine-mcp/write_server.py"]}


def _load():
    spec = importlib.util.spec_from_file_location("deployment_checks_991", MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deployment_checks_991"] = module
    spec.loader.exec_module(module)
    return module


def _config(api_server=("okengine", "okengine-write-chat"), actor="chat:api_server") -> dict:
    cfg = {"mcp_servers": {
        "okengine": {"url": "http://okengine-mcp:8730/mcp"},
        "okengine-write-chat": dict(_WRITER, env={"OKENGINE_WRITE_ACTOR": actor}),
    }}
    if api_server is not None:
        cfg["platform_toolsets"] = {"api_server": list(api_server)}
    return cfg


def _run(tmp_path, cfg, catalog: str | None):
    """Run the check against a scratch deployment. catalog: 'repo' copies the shipped catalog,
    a string is written as the baked catalog's text, None leaves the image without one."""
    C = _load()
    data, hermes = tmp_path / "data", tmp_path / "hermes"
    data.mkdir()
    if cfg is not None:
        (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    if catalog is not None:
        (hermes / "config" / "policy").mkdir(parents=True)
        target = hermes / "config" / "policy" / "catalog.yaml"
        if catalog == "repo":
            shutil.copy(CATALOG, target)
        else:
            target.write_text(catalog)
    C.configure(tmp_path, data=data, hermes=hermes)
    return C.run(["chat-capability"])


def _pre_983_catalog() -> str:
    doc = yaml.safe_load(CATALOG.read_text())
    doc["capabilities"].pop("chat:api_server")
    return yaml.safe_dump(doc)


def test_a_bound_chat_writer_on_an_image_without_the_capability_fails(tmp_path):
    found = _run(tmp_path, _config(), _pre_983_catalog())
    fails = [m for lvl, area, m in found if lvl == "FAIL"]
    assert len(fails) == 1 and "chat:api_server" in fails[0], found
    assert "rebuild" in fails[0]


def test_the_shipped_catalog_satisfies_the_bound_chat_writer(tmp_path):
    found = _run(tmp_path, _config(), "repo")
    assert [lvl for lvl, _a, _m in found] == ["INFO"], found
    assert "chat:api_server" in found[0][2]


def test_an_unbound_chat_writer_needs_no_capability(tmp_path):
    """Editing off: ensure-runtime drops the writer from the toolset, so nothing can call it."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _run(tmp_path / "a", _config(api_server=("okengine",)), _pre_983_catalog()) == []
    assert _run(tmp_path / "b", _config(api_server=None), _pre_983_catalog()) == []


def test_no_runtime_config_is_not_this_checks_finding(tmp_path):
    assert _run(tmp_path, None, _pre_983_catalog()) == []


def test_a_missing_baked_catalog_is_undetectable_not_a_pass(tmp_path):
    found = _run(tmp_path, _config(), None)
    assert [lvl for lvl, _a, _m in found] == ["WARN"], found
    assert "undetectable" in found[0][2]


def test_the_actor_the_writer_actually_carries_is_the_one_checked(tmp_path):
    found = _run(tmp_path, _config(actor="chat:other"), "repo")
    fails = [m for lvl, _a, m in found if lvl == "FAIL"]
    assert len(fails) == 1 and "chat:other" in fails[0], found


@pytest.mark.parametrize("text", ["capabilities: [chat:api_server]\n", "- not a mapping\n",
                                  "capabilities: [unclosed\n", "", "# comment only\n"])
def test_a_malformed_baked_catalog_fails(tmp_path, text):
    found = _run(tmp_path, _config(), text)
    assert any(lvl == "FAIL" for lvl, _a, _m in found), found


def _verifier_step(tmp_path, cfg, catalog, dcx: str, scripts: Path | None = None) -> str:
    """Run post_deploy_verify's chat-capability step for real, with `dcx` (docker compose exec)
    stubbed to run the in-gateway command locally against a scratch deployment."""
    import os
    import subprocess
    text = (REPO / "scripts" / "post_deploy_verify.sh").read_text()
    start = text.index("chatcap=$(dcx")
    end = text.index("\nfi\n", text.index('done <<< "$chatcap"', start)) + len("\nfi\n")
    step = (text[start:end]
            .replace("/opt/data/scripts", str(scripts or REPO / "scripts" / "cron"))
            .replace("/opt/hermes/.venv/bin/python", sys.executable))
    _run(tmp_path, cfg, catalog)                      # lays out data/ and hermes/
    script = ('ok(){ echo "PASS $1"; }; bad(){ echo "FAIL $1"; }; wn(){ echo "WARN $1"; }\n'
              f"GW=gw\n{dcx}\n{step}\n")
    env = dict(os.environ, OKENGINE_DATA=str(tmp_path / "data"),
               OKENGINE_HERMES_DIR=str(tmp_path / "hermes"), WIKI_PATH=str(tmp_path))
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                            timeout=120)
    return result.stdout + result.stderr


_LOCAL = 'dcx() { shift; "$@"; }'


def test_the_verifier_step_fails_a_stale_image(tmp_path):
    out = _verifier_step(tmp_path, _config(), _pre_983_catalog(), _LOCAL)
    assert out.startswith("FAIL [chat-capability]") and "chat:api_server" in out, out


def test_the_verifier_step_passes_a_current_image(tmp_path):
    out = _verifier_step(tmp_path, _config(), "repo", _LOCAL)
    assert out.startswith("PASS [chat-capability]"), out


def test_the_verifier_step_with_nothing_bound_passes(tmp_path):
    out = _verifier_step(tmp_path, _config(api_server=("okengine",)), "repo", _LOCAL)
    assert out.strip() == "PASS no bound chat writer needs a baked capability", out


def test_an_unreachable_gateway_is_undetectable_not_a_pass(tmp_path):
    out = _verifier_step(tmp_path, _config(), "repo", "dcx() { return 1; }")
    assert out.startswith("WARN cannot run the chat-capability check"), out


def test_a_staged_copy_that_predates_the_check_fails_not_passes(tmp_path):
    """okengine#994 [15]: a deployment_checks.py staged before the chat-capability check existed
    returned no findings for the unknown name, and the step read that as "nothing bound"."""
    stale = tmp_path / "staged"
    stale.mkdir()
    (stale / "deployment_checks.py").write_text(
        "CHECKS = {'pins': lambda: None}\n"
        "def run(names=None):\n    return []\n")
    out = _verifier_step(tmp_path, _config(), "repo", _LOCAL, scripts=stale)
    assert out.startswith("FAIL [chat-capability] the staged deployment_checks.py predates"), out
    assert "no bound chat writer" not in out


def test_run_fails_an_unknown_check_name(tmp_path):
    """okengine#994 [15] negative fixture: a requested check this copy does not register never
    ran, so run() must report it as a FAIL rather than return an empty (passing) result."""
    C = _load()
    C.configure(tmp_path, data=tmp_path / "data", hermes=tmp_path / "hermes")
    found = C.run(["chat-capability-from-the-future"])
    assert found == [("FAIL", "validator", "unknown check 'chat-capability-from-the-future': this "
                      "deployment_checks.py does not register it (stale staged copy?), so it "
                      "never ran")]


def test_run_reports_each_unknown_name_and_still_runs_the_known_ones(tmp_path):
    C = _load()
    C.configure(tmp_path, data=tmp_path / "data", hermes=tmp_path / "hermes")
    calls = []
    C.CHECKS["probe"] = lambda: calls.append("probe")
    try:
        found = C.run(["zz-missing", "probe", "aa-missing"])
    finally:
        del C.CHECKS["probe"]
    assert calls == ["probe"]
    assert [m.split()[2] for _l, _a, m in found] == ["'aa-missing':", "'zz-missing':"]
    assert {(lvl, area) for lvl, area, _m in found} == {("FAIL", "validator")}


def test_an_unparseable_baked_catalog_is_reported_once(tmp_path):
    found = _run(tmp_path, _config(), "capabilities: [unclosed\n")
    fails = [m for lvl, _a, m in found if lvl == "FAIL"]
    assert len(fails) == 1 and "unparseable" in fails[0], found


# --- okengine#993 N2: a chat writer bound BY DEFAULT is bound ----------------------------------
# With no actor-less writer registered, a platform with no platform_toolsets list (or a list that
# names no registered MCP server) binds every MCP server, the chat writer included, so the check
# must require its capability. Reading "bound" as "named in a list" passed these silently.


def _run_env(tmp_path, monkeypatch, cfg, catalog, *, dotenv="", environ=None):
    for key in ("API_SERVER_ENABLED", "API_SERVER_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (environ or {}).items():
        monkeypatch.setenv(key, value)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".env").write_text(dotenv)
    return _run_existing(tmp_path, cfg, catalog)


def _run_existing(tmp_path, cfg, catalog):
    C = _load()
    data, hermes = tmp_path / "data", tmp_path / "hermes"
    (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    (hermes / "config" / "policy").mkdir(parents=True)
    (hermes / "config" / "policy" / "catalog.yaml").write_text(catalog)
    C.configure(tmp_path, data=data, hermes=hermes)
    return C.run(["chat-capability"])


def _fails(found):
    return [m for lvl, _a, m in found if lvl == "FAIL"]


def test_a_token_enabled_platform_with_no_list_binds_the_chat_writer(tmp_path, monkeypatch):
    found = _run_env(tmp_path, monkeypatch, _config(api_server=("okengine",)),
                     _pre_983_catalog(), dotenv="TELEGRAM_BOT_TOKEN=t\n")
    assert len(_fails(found)) == 1 and "chat:api_server" in _fails(found)[0], found


def test_chat_enabled_with_no_api_server_list_binds_the_chat_writer(tmp_path, monkeypatch):
    found = _run_env(tmp_path, monkeypatch, _config(api_server=None), _pre_983_catalog(),
                     environ={"API_SERVER_ENABLED": "true"})
    assert len(_fails(found)) == 1, found


def test_a_config_enabled_platform_with_no_list_binds_the_chat_writer(tmp_path, monkeypatch):
    cfg = dict(_config(api_server=("okengine",)), telegram={"enabled": True})
    found = _run_env(tmp_path, monkeypatch, cfg, _pre_983_catalog())
    assert len(_fails(found)) == 1, found


def test_a_list_naming_no_registered_server_binds_the_chat_writer(tmp_path, monkeypatch):
    found = _run_env(tmp_path, monkeypatch, _config(api_server=("web",)), _pre_983_catalog())
    assert len(_fails(found)) == 1, found


def test_no_mcp_keeps_a_default_platform_off_the_chat_writer(tmp_path, monkeypatch):
    cfg = _config(api_server=("okengine",))
    cfg["platform_toolsets"]["telegram"] = ["no_mcp"]
    found = _run_env(tmp_path, monkeypatch, cfg, _pre_983_catalog(),
                     dotenv="TELEGRAM_BOT_TOKEN=t\n")
    assert found == [], found


def test_a_default_bound_chat_writer_passes_on_the_shipped_catalog(tmp_path, monkeypatch):
    found = _run_env(tmp_path, monkeypatch, _config(api_server=None), CATALOG.read_text(),
                     environ={"API_SERVER_ENABLED": "true"})
    assert [lvl for lvl, _a, _m in found] == ["INFO"], found
