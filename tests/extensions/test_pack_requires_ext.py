"""Pack -> extension dependency declaration + enforcement — okengine#142 (A + D).

A pack declares `requires: [ext:<id>@>=ver]` (or annotates an `ext:<id>` schema owner);
`framework validate` FAILS before deploy if that extension isn't enabled (explicit or
core-default-on) at the version floor — instead of degrading silently at runtime.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
FV = REPO / "scripts" / "framework_validate.py"
PM = REPO / "scripts" / "pack_meta.py"
DISC = REPO / "scripts" / "extension_discovery.py"

pytestmark = pytest.mark.skipif(not FV.is_file(), reason="framework modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _pack(tmp_path, requires=None, ext_version="0.1.0", schema_owner=False):
    pack = tmp_path / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "pack.yaml").write_text(yaml.safe_dump({
        "name": "okpack-test", "version": "0.1.0", "trust": "private",
        "owns": {"types": ["thing"]}, "requires": requires or []}), encoding="utf-8")
    schema = {"okf": {"required": ["type"]}, "types": {"thing": {}}}
    if schema_owner:
        schema["owners"] = {"fields": {"thing.scored_by": "ext:demo.scorer"}}
    (pack / "schema.yaml").write_text(yaml.safe_dump(schema), encoding="utf-8")
    return pack


def _add_ext(pack, ext_id, version="0.1.0"):
    d = pack / "extensions" / ext_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "extension.yaml").write_text(yaml.safe_dump({
        "id": ext_id, "kind": "operation", "version": version, "trust": "in-gateway",
        "requires": {"engine": ">=0.4.0"},
        "capabilities": {"read": ["wiki/**"], "write": ["x/**"]},
        "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "r.py"}}),
        encoding="utf-8")
    (d / "r.py").write_text("print('{}')\n", encoding="utf-8")
    return d


def _run_check(pack):
    fv = _load("framework_validate", FV)
    r = fv.Report()
    fv.check_extension_requirements(pack, r)
    fails = [c for s, c, _ in r.rows if s == "FAIL"]
    oks = [c for s, c, _ in r.rows if s == "OK"]
    return fails, oks


# --- pack_meta parsing ------------------------------------------------------

def test_extension_requires_parsing():
    pm = _load("pack_meta", PM)
    reqs = pm.extension_requires({"requires": ["okpack-base@>=0.1.0", "ext:okengine.predictions@>=0.2.0"]})
    assert reqs == [("okengine.predictions", ">=0.2.0")]


def test_validate_composition_skips_ext_requires():
    pm = _load("pack_meta", PM)
    meta = {"name": "p", "version": "1.0.0", "trust": "private", "requires": ["ext:okengine.x@>=0.1.0"],
            "owns_types": set(), "owns_namespaces": set()}
    errors = pm.validate_composition([meta])
    assert not any("ext:okengine.x" in e for e in errors), errors   # not treated as a missing pack


# --- A: pack requires ext ---------------------------------------------------

def test_required_extension_enabled_passes(tmp_path):
    pack = _pack(tmp_path, requires=["ext:demo.thing@>=0.1.0"])
    _add_ext(pack, "demo.thing")
    _load("extension_discovery", DISC).set_enabled(pack, "demo.thing", True)
    fails, oks = _run_check(pack)
    assert not fails and any("requires ext:demo.thing" in c for c in oks)


def test_required_extension_not_enabled_fails(tmp_path):
    pack = _pack(tmp_path, requires=["ext:demo.thing@>=0.1.0"])
    _add_ext(pack, "demo.thing")                                   # present but NOT enabled
    fails, _ = _run_check(pack)
    assert any("requires ext:demo.thing" in c for c in fails), fails


def test_required_extension_version_floor_not_met_fails(tmp_path):
    pack = _pack(tmp_path, requires=["ext:demo.thing@>=0.9.0"])
    _add_ext(pack, "demo.thing", version="0.1.0")
    _load("extension_discovery", DISC).set_enabled(pack, "demo.thing", True)
    fails, _ = _run_check(pack)
    assert any("demo.thing@>=0.9.0" in c for c in fails), fails


def test_no_ext_requires_is_a_noop(tmp_path):
    pack = _pack(tmp_path, requires=["okpack-base@>=0.1.0"])       # pack->pack only
    fails, oks = _run_check(pack)
    assert not fails and not oks                                   # check short-circuits


# --- D: schema ext owner ----------------------------------------------------

def test_schema_ext_owner_not_enabled_fails(tmp_path):
    pack = _pack(tmp_path, schema_owner=True)                      # owners.fields -> ext:demo.scorer
    fails, _ = _run_check(pack)
    assert any("schema owner ext:demo.scorer" in c for c in fails), fails
