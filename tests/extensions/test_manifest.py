"""Regression tests for extension.yaml parse + validation (#134).

Guards the §6 structural floor that discovery relies on: a strict `id`, known
kind/trust/scope, semver, required-key presence, and the unknown-key severity
split (FAIL under requires/capabilities/operation; WARN for descriptive top-level).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD_PATH = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not MOD_PATH.is_file(),
                                reason="extension_manifest.py not present")


def _mod():
    spec = importlib.util.spec_from_file_location("extension_manifest", MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["extension_manifest"] = m
    spec.loader.exec_module(m)
    return m


def _valid(**over):
    man = {
        "id": "demo.alpha", "kind": "operation", "version": "0.1.0", "name": "Alpha",
        "requires": {"engine": ">=0.3.0"}, "trust": "in-gateway",
        "capabilities": {"read": ["wiki/**"], "write": ["alpha/**"]},
    }
    man.update(over)
    return man


def test_valid_manifest_passes():
    m = _mod()
    errors, warnings = m.validate_manifest(_valid())
    assert errors == []
    assert warnings == []


@pytest.mark.parametrize("bad_id", ["Demo.Alpha", "demo_alpha", "a", "-demo", "demo-"])
def test_invalid_id_fails(bad_id):
    m = _mod()
    errors, _ = m.validate_manifest(_valid(id=bad_id))
    assert any("id" in e for e in errors), (bad_id, errors)


def test_missing_required_keys_fail():
    m = _mod()
    errors, _ = m.validate_manifest({"name": "x"})
    for k in ("id", "kind", "version", "requires", "trust", "capabilities"):
        assert any(k in e for e in errors), (k, errors)


def test_unknown_kind_fails_known_nonmvp_warns():
    m = _mod()
    errors, _ = m.validate_manifest(_valid(kind="frobnicator"))
    assert any("kind" in e for e in errors)
    errors2, warnings2 = m.validate_manifest(_valid(kind="importer"))
    assert errors2 == []
    assert any("reserved" in w or "not-yet" in w for w in warnings2)


def test_bad_version_and_trust_fail():
    m = _mod()
    assert any("version" in e for e in m.validate_manifest(_valid(version="1.0"))[0])
    assert any("trust" in e for e in m.validate_manifest(_valid(trust="root"))[0])


def test_workspace_scope_warns_not_fails():
    m = _mod()
    errors, warnings = m.validate_manifest(_valid(scope="workspace"))
    assert errors == []
    assert any("workspace" in w for w in warnings)
    # an unknown scope still fails
    assert any("scope" in e for e in m.validate_manifest(_valid(scope="galaxy"))[0])


def test_unknown_subkeys_fail_unknown_toplevel_warns():
    m = _mod()
    errors, _ = m.validate_manifest(_valid(requires={"engine": ">=0.3.0", "bogus": 1}))
    assert any("requires" in e and "bogus" in e for e in errors)
    errors2, _ = m.validate_manifest(
        _valid(capabilities={"read": ["wiki/**"], "nope": True}))
    assert any("capabilities" in e and "nope" in e for e in errors2)
    _, warnings = m.validate_manifest(_valid(flavour="spicy"))
    assert any("flavour" in w for w in warnings)


def test_is_reserved_id():
    m = _mod()
    assert m.is_reserved_id("okengine.contradictions")
    assert not m.is_reserved_id("demo.alpha")


def test_load_manifest_absent_and_parse_error(tmp_path):
    m = _mod()
    assert m.load_manifest(tmp_path) is None          # no extension.yaml
    (tmp_path / "extension.yaml").write_text("123\n", encoding="utf-8")
    with pytest.raises(m.ManifestError):
        m.load_manifest(tmp_path)                      # not a mapping
