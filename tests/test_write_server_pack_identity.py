"""okengine#662: converge ownership arbitration trusted a caller-supplied `pack`.

`_converge` did `pack = pack or _prov_pack()`, so the tool argument won over the deployment-pinned
`OKENGINE_PACK`, while `_prov_pack`'s docstring promised the identity is "never client/agent-
supplied". In a composed vault any lane with `converge_entity` could overwrite another pack's owned
fields with "0 conflicts" just by naming the owner. The deployment identity now wins whenever it is
set; a differing caller value is refused loudly, not silently ignored."""
from __future__ import annotations

import importlib.util
import os
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


def _load(wiki_path: Path, monkeypatch, deployment_pack: str | None):
    monkeypatch.setenv("WIKI_PATH", str(wiki_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-16")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config" / "base-schema.yaml"))
    if deployment_pack is None:
        monkeypatch.delenv("OKENGINE_PACK", raising=False)
    else:
        monkeypatch.setenv("OKENGINE_PACK", deployment_pack)
    (wiki_path / "wiki").mkdir(parents=True, exist_ok=True)
    (wiki_path / "wiki" / "schema.yaml").write_text(SCHEMA)
    spec = importlib.util.spec_from_file_location("write_server_pack_identity", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server_pack_identity"] = m
    spec.loader.exec_module(m)
    return m


def _fm(m, rel: str) -> dict:
    return m._read_page(m._safe(rel))[0]


def _seed_owned_page(m) -> None:
    # the OWNING deployment (atk) creates the technique with an owned field
    os.environ["OKENGINE_PACK"] = "atk"
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: execution")
    assert out.startswith("created"), out


def test_caller_cannot_impersonate_the_owner(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "hunt")
    _seed_owned_page(m)
    monkeypatch.setenv("OKENGINE_PACK", "hunt")      # this gateway is the hunt deployment
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: HIJACK", pack="atk")
    assert out.startswith("refused:"), out
    assert "deployment-pinned" in out and "hunt" in out and "atk" in out
    assert _fm(m, "attack-pattern/t1059.md")["tactic"] == "execution", "owned field untouched"
    assert _fm(m, "attack-pattern/t1059.md").get("last_modified_by") != "atk"


def test_deployment_identity_governs_ownership_when_pack_is_omitted(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "hunt")
    _seed_owned_page(m)
    monkeypatch.setenv("OKENGINE_PACK", "hunt")
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: HIJACK")
    assert "conflict" in out, out                     # non-owner write is flagged, not applied
    fm = _fm(m, "attack-pattern/t1059.md")
    assert fm["tactic"] == "execution"
    assert fm.get("last_modified_by") == "hunt"


def test_matching_caller_value_is_accepted(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "atk")
    _seed_owned_page(m)
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: persistence", pack="atk")
    assert not out.startswith("refused:"), out
    assert _fm(m, "attack-pattern/t1059.md")["tactic"] == "persistence"


def test_legacy_deploy_without_env_still_honours_the_argument(tmp_path, monkeypatch):
    """No OKENGINE_PACK (a pre-#90 single-pack deploy): the argument is the only identity there is."""
    m = _load(tmp_path, monkeypatch, None)
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: execution", pack="atk")
    assert out.startswith("created"), out
    out = m._converge("attack-pattern/t1059.md",
                      "type: attack-pattern\ntechnique_id: T1059\ntactic: persistence", pack="atk")
    assert not out.startswith("refused:"), out
    assert _fm(m, "attack-pattern/t1059.md")["tactic"] == "persistence"
