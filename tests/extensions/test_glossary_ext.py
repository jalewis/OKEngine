"""okengine.glossary — the first extension that OWNS a schema type (bring-your-own-schema,
#133 Own path) and writes its own namespace (#132). Exercises areas contradictions
(schema-excluded dashboards) and predictions (reuses the pack type) don't.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.glossary"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
SCHEMA_LIB = REPO / "scripts" / "cron" / "schema_lib.py"

pytestmark = pytest.mark.skipif(not EXT.is_dir(), reason="okengine.glossary absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest():
    return yaml.safe_load((EXT / "extension.yaml").read_text(encoding="utf-8"))


# --- manifest -------------------------------------------------------------

def test_manifest_valid_first_party_opt_in():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.glossary" and mod.is_reserved_id(m["id"])
    assert m.get("core") is not True                  # opt-in (spends model budget)
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


def test_composes_into_one_agent_job():
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "okengine.glossary", "tier": "engine", "dir": str(EXT), "manifest": _manifest()}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    j = jobs[0]
    assert j["name"] == "okengine.glossary" and j["no_agent"] is False
    assert j["prompt"].strip() and j["tier"] == "concepts"
    assert j["script"].endswith("select_undefined_terms.py")


def test_config_block_present():
    m = _manifest()
    assert m["config"]["min_references"]["default"] == 3


# --- bring-your-own-schema: OWN a type (#133) -----------------------------

def test_fragment_owns_term_type_and_namespace(tmp_path):
    sl = _load("schema_lib", SCHEMA_LIB)
    frag = yaml.safe_load((EXT / "schema" / "glossary.schema.yaml").read_text())
    root = tmp_path
    (root / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "types": {"entity": {"required": ["type"]}},
        "partitioning": {"namespaces": {"entities": {}}}}), encoding="utf-8")
    composed, errors = sl.compose_schema(root, [("ext:okengine.glossary", frag)])
    assert not errors, errors
    assert "term" in composed["types"]                                   # new type folded in
    assert composed["owners"]["types"]["term"] == "ext:okengine.glossary"
    assert "glossary" in composed["partitioning"]["namespaces"]
    assert composed["owners"]["namespaces"]["glossary"] == "ext:okengine.glossary"


def test_own_collides_with_a_pack_that_already_has_term(tmp_path):
    """Own = new ids only: a pack already owning `term` makes the fragment fail loud."""
    sl = _load("schema_lib", SCHEMA_LIB)
    frag = yaml.safe_load((EXT / "schema" / "glossary.schema.yaml").read_text())
    root = tmp_path
    (root / "schema.yaml").write_text(yaml.safe_dump({
        "okf": {"required": ["type"]},
        "types": {"term": {"required": ["type"]}},               # pack already owns `term`
        "partitioning": {"namespaces": {"glossary": {}}}}), encoding="utf-8")
    _, errors = sl.compose_schema(root, [("ext:okengine.glossary", frag)])
    assert any("term" in e and "already owned" in e for e in errors), errors


# --- wake-gate selector ---------------------------------------------------

def _run_gate(vault: Path, min_refs="3"):
    env = {**os.environ, "WIKI_PATH": str(vault), "OKENGINE_GLOSSARY_MIN_REFS": min_refs}
    out = subprocess.run([sys.executable, str(EXT / "select_undefined_terms.py")],
                         capture_output=True, text=True, env=env)
    return out.stdout


def _page(vault: Path, rel: str, body: str):
    p = vault / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_gate_wakes_on_enough_references(tmp_path):
    for i in range(3):
        _page(tmp_path, f"entities/e{i}.md", "Uses [[glossary/latency]] heavily.\n")
    out = _run_gate(tmp_path)
    assert '"wakeAgent": true' in out and "latency" in out


def test_gate_stays_quiet_below_threshold_or_when_defined(tmp_path):
    _page(tmp_path, "entities/a.md", "One mention of [[glossary/latency]].\n")     # 1 < 3
    assert '"wakeAgent": false' in _run_gate(tmp_path)
    # now 3 refs but the term IS defined -> still quiet
    for i in range(3):
        _page(tmp_path, f"entities/b{i}.md", "[[glossary/throughput]]\n")
    _page(tmp_path, "glossary/throughput.md", "---\ntype: term\nterm: Throughput\n---\nx\n")
    assert "throughput" not in _run_gate(tmp_path)
