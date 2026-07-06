"""okengine#90 P3 — composition write provenance.

With OKENGINE_PACK set (the deployment's pack identity, injected at deploy from pack.yaml `name`),
every enforced write stamps `maintained_by` (the list of packs that have written the page) +
`discovered_by` (the first attributor, on create). Deployment-pinned — never client-supplied. No
env => no stamp (legacy single-pack, back-compatible).
"""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "okengine-mcp" / "write_server.py"
_SCHEMA = """\
okf:
  required: [type]
types:
  entity: {required: [type, name]}
strict_types: false
permissions:
  default: {create: true, update: true, delete: false}
"""


def _load():
    spec = importlib.util.spec_from_file_location("write_server", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sys.modules.pop("write_server", None)
    return _load(), tmp_path


def _fm(root, rel):
    return yaml.safe_load((root / "wiki" / rel).read_text().split("---")[1])


def test_create_stamps_maintained_and_discovered(vault, monkeypatch):
    m, root = vault
    monkeypatch.setenv("OKENGINE_PACK", "okpack-x")
    m._create("entities/a/apt-prov", "type: entity\nname: APT-Prov", "body")
    fm = _fm(root, "entities/a/apt-prov.md")
    assert fm["maintained_by"] == ["okpack-x"]
    assert fm["discovered_by"] == "okpack-x"


def test_update_by_other_pack_unions_maintainers(vault, monkeypatch):
    m, root = vault
    monkeypatch.setenv("OKENGINE_PACK", "okpack-x")
    m._create("entities/a/apt-prov", "type: entity\nname: APT-Prov", "body")
    monkeypatch.setenv("OKENGINE_PACK", "okpack-y")
    m._update("entities/a/apt-prov", "motivation: espionage")
    fm = _fm(root, "entities/a/apt-prov.md")
    assert fm["maintained_by"] == ["okpack-x", "okpack-y"]   # unioned across packs
    assert fm["discovered_by"] == "okpack-x"                 # first attributor unchanged


def test_no_env_no_stamp(vault, monkeypatch):
    m, root = vault
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    m._create("entities/a/apt-nop", "type: entity\nname: APT-Nop", "body")
    fm = _fm(root, "entities/a/apt-nop.md")
    assert "maintained_by" not in fm and "discovered_by" not in fm
