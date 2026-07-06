"""okengine.frontier-watch — capability-frontier / demand-supply whitespace (okengine#147).

An applied, schema-OWNING extension built on the #63 cron drop-in model (ops live in
crons/*.cron.json, no manifest operations: block). Guards: it ships well-formed, composes its
drop-in crons, owns its schema, and its wake-gate fires only on demand-rich + supply-thin
capabilities.
"""
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.frontier-watch"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
SCHEMA_LIB = REPO / "scripts" / "cron" / "schema_lib.py"
SELECTOR = EXT / "select_whitespace.py"

pytestmark = pytest.mark.skipif(not EXT.is_dir(), reason="okengine.frontier-watch absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest():
    return yaml.safe_load((EXT / "extension.yaml").read_text(encoding="utf-8"))


def _frag():
    return yaml.safe_load((EXT / "schema" / "frontier.schema.yaml").read_text())


# --- manifest + drop-in compose -------------------------------------------

def test_manifest_valid_opt_in_dropin():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.frontier-watch" and mod.is_reserved_id(m["id"])
    assert m.get("core") is not True                      # opt-in (spends model budget)
    assert "operation" not in m and "operations" not in m  # ops are drop-in crons (#63 P1)
    assert m["requires"]["schema_refs"] == ["concept", "entity", "source"]
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


def test_dropin_crons_compose_to_two_jobs():
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "okengine.frontier-watch", "tier": "engine", "dir": str(EXT), "manifest": _manifest()}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    names = sorted(j["name"] for j in jobs)
    assert names == ["okengine.frontier-watch:frontier-brief",
                     "okengine.frontier-watch:whitespace-sweep"]
    by = {j["name"]: j for j in jobs}
    sweep = by["okengine.frontier-watch:whitespace-sweep"]
    assert sweep["script"].endswith("select_whitespace.py") and sweep["tier"] == "analyze"


def test_soft_lacuna_edge_not_a_hard_require():
    """The lacuna integration is a SOFT convention (write into lacuna/**), never a hard dep —
    frontier-watch must run with lacuna absent."""
    m = _manifest()
    assert "lacuna/**" in m["capabilities"]["write"]
    assert "lacuna" not in str((m["requires"].get("extensions") or ""))


# --- bring-your-own-schema ------------------------------------------------

def _base_pack(root: Path, extra_types=None):
    types = {"entity": {"required": ["type"]}, "concept": {"required": ["type"]},
             "source": {"required": ["type"]}}
    types.update(extra_types or {})
    (root / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]}, "types": types,
        "partitioning": {"namespaces": {"entities": {}, "concepts": {}, "sources": {}}}}))


def test_fragment_owns_whitespace_thesis_type_and_namespace(tmp_path):
    sl = _load("schema_lib", SCHEMA_LIB)
    _base_pack(tmp_path)
    composed, errors = sl.compose_schema(tmp_path, [("ext:okengine.frontier-watch", _frag())])
    assert not errors, errors
    assert composed["owners"]["types"]["whitespace-thesis"] == "ext:okengine.frontier-watch"
    assert composed["owners"]["namespaces"]["frontier"] == "ext:okengine.frontier-watch"
    assert set(composed["types"]["whitespace-thesis"]["required"]) == {
        "type", "capability", "demand_signal", "supply_state", "thesis"}


def test_own_collides_with_a_pack_that_already_has_the_type(tmp_path):
    sl = _load("schema_lib", SCHEMA_LIB)
    _base_pack(tmp_path, extra_types={"whitespace-thesis": {"required": ["type"]}})
    _, errors = sl.compose_schema(tmp_path, [("ext:okengine.frontier-watch", _frag())])
    assert any("whitespace-thesis" in e and "already owned" in e for e in errors), errors


# --- wake-gate selector ---------------------------------------------------

def _run_gate(vault: Path, **env):
    e = {**os.environ, "WIKI_PATH": str(vault), "OKENGINE_MCP_WRITE_DATE": "2026-06-28", **env}
    return subprocess.run([sys.executable, str(SELECTOR)],
                          capture_output=True, text=True, env=e).stdout


def _page(vault: Path, rel: str, body: str):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _capability(vault: Path, slug="agentic-soc", demand=6, supply=1):
    _page(vault, f"concepts/{slug}.md", f"---\ntype: concept\n---\nThe {slug} capability.\n")
    for i in range(demand):
        _page(vault, f"sources/2026/06/want-{slug}-{i}.md", f"The market wants [[concepts/{slug}]].\n")
    for i in range(supply):
        _page(vault, f"entities/{slug}-vendor-{i}.md", f"Supplies [[concepts/{slug}]].\n")


def test_gate_wakes_on_demand_rich_supply_thin(tmp_path):
    _capability(tmp_path, demand=6, supply=1)             # 6 >= 5 demand, 1 <= 2 supply
    out = _run_gate(tmp_path)
    assert '"wakeAgent": true' in out and "agentic-soc" in out
    assert "demand 6" in out and "supply 1" in out


def test_gate_quiet_when_demand_thin(tmp_path):
    _capability(tmp_path, demand=2, supply=0)             # 2 < 5 demand -> not wanted enough
    assert '"wakeAgent": false' in _run_gate(tmp_path)


def test_gate_quiet_when_supply_ample(tmp_path):
    _capability(tmp_path, demand=8, supply=5)             # 5 > 2 supply -> already served
    assert '"wakeAgent": false' in _run_gate(tmp_path)


def test_gate_excludes_recently_thesised(tmp_path):
    _capability(tmp_path, demand=6, supply=1)
    _page(tmp_path, "frontier/agentic-soc-gap.md",
          "---\ntype: whitespace-thesis\nupdated: 2026-06-25\n---\nGap in [[concepts/agentic-soc]].\n")
    assert '"wakeAgent": false' in _run_gate(tmp_path)    # thesised 3 days ago -> quiet


# --- isolation ------------------------------------------------------------

def test_selector_is_self_contained():
    src = SELECTOR.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", src, re.M)
    allowed = {"__future__", "json", "os", "re", "sys", "collections", "dataclasses",
               "datetime", "pathlib", "yaml", "typing", "itertools", "math", "functools"}
    foreign = [i for i in imports if i.split(".")[0] not in allowed]
    assert not foreign, f"selector imports non-stdlib siblings: {foreign}"
