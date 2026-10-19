"""okengine#1027: write servers ran with no pack principal.

Hermes starts a stdio MCP server with a filtered environment: a safe baseline (PATH, HOME, ...)
plus the server's own `env:` map (`tools/mcp_tool_config._build_safe_env` at the pinned tag). The
gateway's OKENGINE_PACK never reached a writer and no rendered map set it, so every lane wrote as
nobody: a non-owner of its own pack's pages, and able to name any owner through
`converge_entity(pack=...)`, because without the env the argument was the only identity. The
writer now derives its pack from the vault's pack.yaml and refuses to start without one."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "okengine-mcp" / "write_server.py"
SCHEMA = (
    "types:\n"
    "  attack-pattern:\n"
    "    required: [type]\n"
    "    id_authority: mitre\n"
    "    id_field: technique_id\n"
    "    owner: atk\n"
    "  vendor: {required: [type]}\n"
    "partitioning:\n"
    "  namespaces: {attack-pattern: {strategy: flat}}\n"
)
#: What Hermes hands a stdio writer besides its env: map (the safe baseline), never the gateway's.
_HERMES_BASELINE = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR")


def _load(vault: Path, monkeypatch, pack: str | None, actor: str | None = None):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-16")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    monkeypatch.delenv("OKENGINE_PACK", raising=False)   # as Hermes spawns it
    if actor is None:
        monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    else:
        monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    if pack is not None:
        (vault / "pack.yaml").write_text(f"name: {pack}\n", encoding="utf-8")
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    (vault / "wiki" / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("write_server_pack_principal_1027", WS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["write_server_pack_principal_1027"] = module
    spec.loader.exec_module(module)
    return module


def _seed(m) -> None:
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: execution")
    assert out.startswith("created"), out


def _tactic(m) -> str:
    return m._read_page(m._safe("attack-pattern/t1059.md"))[0]["tactic"]


def test_the_owning_pack_may_change_its_field_without_okengine_pack_in_its_env(
        tmp_path, monkeypatch):
    """The owner's writer, spawned without OKENGINE_PACK, is the owner: pack.yaml says so."""
    m = _load(tmp_path, monkeypatch, "atk")
    _seed(m)
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: persistence")
    assert "0 conflict(s)" in out, out
    assert _tactic(m) == "persistence"
    page = m._read_page(m._safe("attack-pattern/t1059.md"))[0]
    assert page.get("last_modified_by") == "atk"
    assert "atk" in page.get("maintained_by", [])


def test_another_pack_cannot_claim_the_owner_through_the_tool_argument(tmp_path, monkeypatch):
    """With no env, the `pack` argument used to be the only identity: any lane could name the
    owner and overwrite its fields (the okengine#662 hole, open again on every live writer)."""
    m = _load(tmp_path, monkeypatch, "atk")
    _seed(m)
    (tmp_path / "pack.yaml").write_text("name: hunt\n", encoding="utf-8")   # the hunt deployment
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: HIJACK", pack="atk")
    assert out.startswith("refused:") and "hunt" in out and "atk" in out, out
    assert _tactic(m) == "execution"


def test_a_non_owner_deployment_is_still_a_non_owner(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "atk")
    _seed(m)
    (tmp_path / "pack.yaml").write_text("name: hunt\n", encoding="utf-8")
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: HIJACK")
    assert "1 conflict(s)" in out, out
    assert _tactic(m) == "execution"


def test_a_co_installed_lane_writes_as_its_own_pack(tmp_path, monkeypatch):
    folder = tmp_path / ".okengine" / "installed-domains"
    folder.mkdir(parents=True)
    (folder / "okpack-guest.json").write_text(
        json.dumps({"pack": "okpack-guest", "cron_jobs": ["okpack-guest-weekly"]}))
    m = _load(tmp_path, monkeypatch, "host-pack", actor="cron:okpack-guest-weekly")
    assert m._prov_pack() == "okpack-guest"
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", "cron:entity-backfill")
    assert m._prov_pack() == "host-pack"


def test_the_principal_follows_the_vault_the_writer_is_pointed_at(tmp_path, monkeypatch):
    """Read per write, like every other WIKI_PATH consumer, never frozen at import."""
    (tmp_path / "first").mkdir()
    m = _load(tmp_path / "first", monkeypatch, "first-pack")
    assert m._prov_pack() == "first-pack"
    other = tmp_path / "second"
    other.mkdir()
    (other / "pack.yaml").write_text("name: second-pack\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(other))
    assert m._prov_pack() == "second-pack"


def _serve(vault: Path, env_map: dict[str, str]) -> subprocess.CompletedProcess:
    env = {k: os.environ[k] for k in _HERMES_BASELINE if k in os.environ}
    env.update(PYTHONPATH=os.pathsep.join(
        str(REPO / p) for p in ("", "src", "okengine-mcp", "scripts/cron")))
    env.update(WIKI_PATH=str(vault), **env_map)
    return subprocess.run([sys.executable, str(WS)], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize(("pack_yaml", "env_map", "reason"), [
    ("version: 1\n", {"OKENGINE_WRITE_ACTOR": "cron:entity-backfill"}, "no pack principal"),
    ("name: atk\n", {"OKENGINE_PACK": "renamed"}, "disagrees"),
])
def test_a_served_writer_without_one_principal_refuses_to_start(tmp_path, pack_yaml, env_map,
                                                                 reason):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "pack.yaml").write_text(pack_yaml, encoding="utf-8")
    result = _serve(tmp_path, env_map)
    assert result.returncode != 0, result
    assert "refusing to start" in result.stderr and reason in result.stderr, result.stderr
