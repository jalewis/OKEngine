"""okengine#1027: `write-principal` FAILs a deployment whose write server would start without a
pack principal (and so refuse to start: its lane gets no write tools).

Hermes hands a stdio writer only a safe baseline plus its own `env:` map, so the check judges each
writer's env map against the vault's pack.yaml with the writer's own startup rule."""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"
_ARGS = ["/opt/hermes/okengine-mcp/write_server.py"]


def _load():
    spec = importlib.util.spec_from_file_location("deployment_checks_1027", MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deployment_checks_1027"] = module
    spec.loader.exec_module(module)
    return module


def _run(tmp_path, servers, pack_yaml="name: okpack-a\n", config=True):
    C = _load()
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    if pack_yaml is not None:
        (vault / "pack.yaml").write_text(pack_yaml)
    if config is True:
        (data / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}))
    elif config is not None:
        (data / "config.yaml").write_text(config)
    C.configure(vault, data=data, hermes=tmp_path / "hermes", env={})
    C.check_write_principal()
    return [(level, msg) for level, area, msg in C.F if area == "write-principal"], C


def _writers(**envs):
    return {name: {"command": "python", "args": list(_ARGS), "env": env}
            for name, env in envs.items()}


def test_writers_that_derive_the_pack_from_pack_yaml_pass(tmp_path):
    servers = _writers(**{"okengine-write": {}, "okengine-write-chat": {"OKENGINE_PACK": "okpack-a"}})
    servers["okengine"] = {"url": "http://okengine-mcp:8730/mcp"}       # a reader is not judged
    findings, _ = _run(tmp_path, servers)
    assert findings == [("INFO", "2 write server(s) start with a pack principal")]


def test_a_writer_with_no_principal_fails(tmp_path):
    """The negative fixture: pack.yaml names no pack and the env map carries none."""
    findings, _ = _run(tmp_path, _writers(**{"okengine-write-entity-backfill": {
        "OKENGINE_WRITE_ACTOR": "cron:entity-backfill"}}), pack_yaml="version: 1\n")
    assert len(findings) == 1 and findings[0][0] == "FAIL", findings
    assert "okengine-write-entity-backfill would refuse to start" in findings[0][1]
    assert "no pack principal" in findings[0][1]


def test_a_writer_whose_env_disagrees_with_pack_yaml_fails(tmp_path):
    servers = _writers(**{"okengine-write": {"OKENGINE_PACK": "okpack-a"},
                          "custom-writer": {"OKENGINE_PACK": "okpack-old"}})
    findings, _ = _run(tmp_path, servers)
    assert [level for level, _ in findings] == ["FAIL"], findings
    assert "custom-writer would refuse to start" in findings[0][1]


def test_an_env_map_in_another_shape_counts_as_empty(tmp_path):
    servers = {"okengine-write": {"args": list(_ARGS), "env": "OKENGINE_PACK=okpack-a"},
               "odd": "not-a-mapping"}
    findings, _ = _run(tmp_path, servers, pack_yaml=None)
    assert [level for level, _ in findings] == ["FAIL"], findings
    assert "okengine-write would refuse" in findings[0][1]


def test_a_legacy_env_pin_without_pack_yaml_passes(tmp_path):
    findings, _ = _run(tmp_path, _writers(**{"okengine-write": {"OKENGINE_PACK": "okpack-a"}}),
                       pack_yaml=None)
    assert [level for level, _ in findings] == ["INFO"], findings


@pytest.mark.parametrize("config", [None, "- a list\n"])
def test_no_readable_config_is_left_to_the_other_checks(tmp_path, config):
    findings, _ = _run(tmp_path, {}, config=config)
    assert findings == []


def test_a_config_with_no_writers_reports_nothing(tmp_path):
    findings, _ = _run(tmp_path, {"okengine": {"url": "http://okengine-mcp:8730/mcp"}})
    assert findings == []
    findings, _ = _run(tmp_path, None, config="mcp_servers: [a]\n")
    assert findings == []


def test_an_image_without_the_principal_module_is_undetectable_not_a_pass(tmp_path, monkeypatch):
    C = _load()

    def missing():
        raise ImportError("no okengine.pack_principal")

    monkeypatch.setattr(C, "_pack_principal", missing)
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir()
    data.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": _writers(**{"okengine-write": {}})}))
    C.configure(vault, data=data, hermes=tmp_path / "hermes", env={})
    C.check_write_principal()
    findings = [(level, msg) for level, area, msg in C.F if area == "write-principal"]
    assert [level for level, _ in findings] == ["WARN"], findings
    assert "undetectable" in findings[0][1]


def test_the_check_runs_in_the_daily_validator():
    C = _load()
    assert C.CHECKS["write-principal"] is C.check_write_principal
    assert "write-principal" in C.VALIDATE_CHECKS


def test_post_deploy_verify_runs_the_check():
    """Step [9] runs every registered check outside POST_DEPLOY_HOST_EXCLUDED (okengine#1032 [22];
    proven by behavior in tests/cron/test_post_deploy_verify_checks.py)."""
    C = _load()
    assert "write-principal" in C.CHECKS
    assert "write-principal" not in C.POST_DEPLOY_HOST_EXCLUDED



def test_the_rule_comes_from_this_engine_checkout_not_an_installed_okengine(monkeypatch):
    """!1305 QA B1: on the host an installed or editable `okengine` (another worktree's) may
    predate okengine#1027. The check loads the rule from its own checkout's src/."""
    C = _load()
    monkeypatch.setitem(sys.modules, "okengine.pack_principal", None)   # a stale install
    module = C._pack_principal()
    assert Path(module.__file__) == REPO / "src" / "okengine" / "pack_principal.py"
    assert module.__name__ == "okengine_pack_principal_checkout", "loaded from the checkout by path"


def test_a_staged_copy_without_a_checkout_uses_the_baked_engine(tmp_path, monkeypatch):
    """In the gateway the staged copy (/opt/data/scripts) has no src/ beside it: the baked wheel
    is the rule."""
    C = _load()
    monkeypatch.setattr(C, "__file__", str(tmp_path / "opt" / "data" / "scripts" / "deployment_checks.py"))
    monkeypatch.syspath_prepend(str(REPO / "src"))
    import okengine.pack_principal as baked
    assert C._pack_principal() is baked
