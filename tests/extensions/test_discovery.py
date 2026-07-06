"""Regression tests for the three-tier extension discovery scanner (#134).

Guards docs/design/discovery-spec.md: the three exact roots, the no-shadow /
reject-duplicate-across-tiers rule, the okengine.* tier-1 reservation, bare-id
enabled-state resolution, and present≠enabled. Roots are fully injectable, so
these are hermetic (no engine/pack on disk required).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD_PATH = REPO / "scripts" / "extension_discovery.py"

pytestmark = pytest.mark.skipif(not MOD_PATH.is_file(),
                                reason="extension_discovery.py not present")


def _mod():
    spec = importlib.util.spec_from_file_location("extension_discovery", MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["extension_discovery"] = m
    spec.loader.exec_module(m)
    return m


def _write_ext(root: Path, ext_id: str, **over):
    """Create <root>/<ext_id>/extension.yaml with a valid minimal manifest."""
    d = Path(root) / ext_id
    d.mkdir(parents=True, exist_ok=True)
    man = {
        "id": ext_id, "kind": "operation", "version": "0.1.0", "name": ext_id,
        "requires": {"engine": ">=0.3.0"}, "trust": "in-gateway",
        "capabilities": {"read": ["wiki/**"], "write": [ext_id.split(".")[-1] + "/**"]},
    }
    man.update(over)
    (d / "extension.yaml").write_text(yaml.safe_dump(man), encoding="utf-8")
    return d


def _roots(tmp_path: Path):
    engine = tmp_path / "engine"
    pack = tmp_path / "pack"
    (engine / "extensions").mkdir(parents=True)
    (pack / "extensions").mkdir(parents=True)
    (pack / ".okengine" / "extensions").mkdir(parents=True)
    return engine, pack


def test_three_roots_discovered(tmp_path):
    m = _mod()
    engine, pack = _roots(tmp_path)
    _write_ext(engine / "extensions", "okengine.alpha")
    _write_ext(pack / "extensions", "demo.bravo")
    _write_ext(pack / ".okengine" / "extensions", "demo.charlie")

    exts, errors = m.discover(pack, engine_root=engine)
    assert errors == []
    by_id = {e["id"]: e["tier"] for e in exts}
    assert by_id == {"okengine.alpha": "engine", "demo.bravo": "pack",
                     "demo.charlie": "operator"}


def test_duplicate_id_across_tiers_rejected(tmp_path):
    """The load-bearing rule: same id in two tiers is a hard FAIL (no shadowing)."""
    m = _mod()
    engine, pack = _roots(tmp_path)
    _write_ext(engine / "extensions", "demo.dup")
    _write_ext(pack / ".okengine" / "extensions", "demo.dup")

    exts, errors = m.discover(pack, engine_root=engine)
    joined = "\n".join(errors)
    assert any("demo.dup" in e and "multiple tiers" in e for e in errors), joined
    assert "engine" in joined and "operator" in joined


def test_okengine_namespace_reserved_to_engine(tmp_path):
    m = _mod()
    engine, pack = _roots(tmp_path)
    _write_ext(pack / "extensions", "okengine.squat")          # tier-2 claim -> FAIL
    _write_ext(engine / "extensions", "okengine.legit")        # tier-1 -> fine

    exts, errors = m.discover(pack, engine_root=engine)
    assert any("okengine.squat" in e and "reserved" in e for e in errors), errors
    assert not any("okengine.legit" in e for e in errors)


def test_enabled_state_resolves_bare_id(tmp_path):
    m = _mod()
    engine, pack = _roots(tmp_path)
    _write_ext(pack / "extensions", "demo.echo")
    (pack / ".okengine" / "extensions.yaml").write_text(
        yaml.safe_dump({"enabled": {"demo.echo": {"config": {"horizon_days": 90}}}}),
        encoding="utf-8")

    exts, errors = m.discover(pack, engine_root=engine)
    assert errors == []
    enabled, en_err = m.load_enabled_state(pack)
    assert en_err == []
    assert enabled["demo.echo"]["config"]["horizon_days"] == 90

    resolved, res_err = m.resolve_enabled(list(enabled), exts)
    assert res_err == []
    assert resolved["demo.echo"]["tier"] == "pack"

    # referenced-but-absent -> FAIL
    _, missing_err = m.resolve_enabled(["demo.ghost"], exts)
    assert any("demo.ghost" in e and "not discovered" in e for e in missing_err)


def test_present_not_enabled(tmp_path):
    """A discovered extension with no enabled-state entry is present, not enabled."""
    m = _mod()
    engine, pack = _roots(tmp_path)
    _write_ext(pack / "extensions", "demo.foxtrot")

    exts, errors = m.discover(pack, engine_root=engine)
    enabled, _ = m.load_enabled_state(pack)
    assert "demo.foxtrot" in {e["id"] for e in exts}
    assert enabled == {}                       # nothing enabled


def test_missing_roots_are_empty_not_error(tmp_path):
    m = _mod()
    # No dirs created at all; engine_root + pack point at empty tmp.
    exts, errors = m.discover(tmp_path / "nopack", engine_root=tmp_path / "noengine")
    assert exts == []
    assert errors == []


def test_unparseable_manifest_is_reported(tmp_path):
    m = _mod()
    engine, pack = _roots(tmp_path)
    d = pack / "extensions" / "demo.broken"
    d.mkdir(parents=True)
    (d / "extension.yaml").write_text("id: [unterminated\n", encoding="utf-8")

    exts, errors = m.discover(pack, engine_root=engine)
    assert any("demo.broken" in e or "unparseable" in e for e in errors), errors


def test_engine_only_discovery_without_pack(tmp_path):
    m = _mod()
    engine = tmp_path / "engine"
    (engine / "extensions").mkdir(parents=True)
    _write_ext(engine / "extensions", "okengine.solo")
    exts, errors = m.discover(None, engine_root=engine)
    assert errors == []
    assert [e["id"] for e in exts] == ["okengine.solo"]
