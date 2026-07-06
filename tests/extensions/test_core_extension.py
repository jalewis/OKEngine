"""Core (default-on) extensions — okengine#142 Part C.

`core: true` on an engine-tier extension flips its default from opt-in to opt-out:
it's active unless explicitly disabled. Non-core stays 'present != enabled', and only
the engine tier may be core (a pack/operator extension can't force itself on).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
DISC = REPO / "scripts" / "extension_discovery.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not DISC.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _rec(ext_id, tier, core=False):
    return {"id": ext_id, "tier": tier, "dir": "/x",
            "manifest": {"id": ext_id, "kind": "operation", "core": core}}


# --- is_core: engine tier only ---------------------------------------------

def test_is_core_only_for_engine_tier():
    d = _load("extension_discovery", DISC)
    assert d.is_core(_rec("okengine.x", "engine", core=True)) is True
    assert d.is_core(_rec("okengine.x", "engine", core=False)) is False
    assert d.is_core(_rec("demo.x", "pack", core=True)) is False        # pack can't force on
    assert d.is_core(_rec("demo.x", "operator", core=True)) is False


# --- effective_enabled: explicit ∪ (core − disabled) -----------------------

def test_core_is_on_without_explicit_enable(tmp_path):
    d = _load("extension_discovery", DISC)
    discovered = [_rec("okengine.core", "engine", core=True),
                  _rec("okengine.optin", "engine", core=False)]
    eff, errors = d.effective_enabled(tmp_path, discovered)   # no enabled-state file at all
    assert not errors
    assert eff == {"okengine.core"}                            # core on, opt-in off


def test_explicit_enable_unioned_with_core(tmp_path):
    d = _load("extension_discovery", DISC)
    d.set_enabled(tmp_path, "okengine.optin", True)
    discovered = [_rec("okengine.core", "engine", core=True),
                  _rec("okengine.optin", "engine", core=False)]
    eff, _ = d.effective_enabled(tmp_path, discovered)
    assert eff == {"okengine.core", "okengine.optin"}


def test_disabling_a_core_extension_turns_it_off(tmp_path):
    d = _load("extension_discovery", DISC)
    discovered = [_rec("okengine.core", "engine", core=True)]
    assert d.effective_enabled(tmp_path, discovered)[0] == {"okengine.core"}
    d.set_enabled(tmp_path, "okengine.core", False)           # explicit OFF
    assert d.effective_enabled(tmp_path, discovered)[0] == set()
    # disabled marker is persisted
    assert "okengine.core" in d._load_disabled(tmp_path)


def test_re_enabling_clears_the_disable(tmp_path):
    d = _load("extension_discovery", DISC)
    discovered = [_rec("okengine.core", "engine", core=True)]
    d.set_enabled(tmp_path, "okengine.core", False)
    d.set_enabled(tmp_path, "okengine.core", True)
    assert "okengine.core" not in d._load_disabled(tmp_path)
    assert d.effective_enabled(tmp_path, discovered)[0] == {"okengine.core"}


def test_resolve_for_pack_includes_core(tmp_path):
    """A pack with NO explicit enable still resolves the engine's core extensions —
    okengine.contradictions is core/default-on (#142). Every resolved entry must be core."""
    d = _load("extension_discovery", DISC)
    resolved, errors = d.resolve_for_pack(tmp_path)
    assert not errors
    assert all(d.is_core(rec) for rec in resolved.values()), \
        "an empty pack should resolve only default-on core extensions"
    assert "okengine.contradictions" in resolved        # the house-baseline core extension


# --- manifest validation ----------------------------------------------------

def test_first_party_core_policy():
    """The deliberate house-baseline decision: okengine.contradictions is core (cheap,
    deterministic, useful on any vault); okengine.predictions stays opt-in (spends model
    budget). Guards the policy so a flip is intentional, not accidental."""
    import pytest as _pt
    y = _pt.importorskip("yaml")
    EXT = REPO / "extensions"
    if not (EXT / "okengine.contradictions" / "extension.yaml").is_file():
        _pt.skip("first-party extensions absent")
    contr = y.safe_load((EXT / "okengine.contradictions" / "extension.yaml").read_text())
    assert contr.get("core") is True, "okengine.contradictions should be core (default-on)"
    preds_f = EXT / "okengine.predictions" / "extension.yaml"
    if preds_f.is_file():
        preds = y.safe_load(preds_f.read_text())
        assert preds.get("core") is not True, "okengine.predictions must stay opt-in"


def test_manifest_core_must_be_bool():
    mod = _load("extension_manifest", MANIFEST)
    base = {"id": "okengine.x", "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
            "requires": {"engine": ">=0.4.0"},
            "capabilities": {"read": ["wiki/**"], "write": ["x/**"]},
            "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "r.py"}}
    ok, _ = mod.validate_manifest({**base, "core": True})
    assert not ok, ok
    bad, _ = mod.validate_manifest({**base, "core": "yes"})
    assert any("core must be a boolean" in e for e in bad), bad
