"""scripts/engine_meta.py — patch-tolerant version-pin compatibility (okengine#104).

A pack pins the engine release it targets. Before #104 the validator required an EXACT
match, so every v0.3.0-pinned pack hard-FAILed (and blocked deploy) against a v0.3.2
engine. satisfies_pin() makes a patch-newer engine compatible while still catching real
generation drift.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _em():
    spec = importlib.util.spec_from_file_location("engine_meta", REPO / "scripts" / "engine_meta.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("pin,engine,expected", [
    ("v0.3.0", "v0.3.2", True),    # patch-newer engine satisfies the pin — the #104 bug
    ("v0.3.0", "v0.3.0", True),    # exact
    ("v0.3.2", "v0.3.0", False),   # engine is an OLDER patch than the pin -> not satisfied
    ("v0.3.0", "v0.4.0", False),   # 0.x minor bump is a new (breaking) series
    ("v0.2.0", "v0.3.2", False),   # older 0.x series
    ("v1.3.0", "v1.5.0", True),    # 1.x: a newer minor is backward-compatible
    ("v1.3.0", "v2.0.0", False),   # 1.x: major bump is breaking
    ("v1.3.0", "v1.3.0", True),
])
def test_satisfies_pin(pin, engine, expected):
    assert _em().satisfies_pin(pin, engine) is expected


def test_satisfies_pin_unparseable_is_none():
    em = _em()
    assert em.satisfies_pin("not-a-version", "v0.3.2") is None
    assert em.satisfies_pin("v0.3.0", "") is None
    assert em.satisfies_pin(None, "v0.3.2") is None


def test_manifest_load_release_and_runtime_edge_shapes(tmp_path, monkeypatch):
    em = _em()
    monkeypatch.setattr(em, "MANIFEST", tmp_path / "missing.yaml")
    assert em._load() == {} and em.engine_release() is None and em.hermes_pin() is None
    em.MANIFEST.write_text("[]")
    assert em._load() == {}
    em.MANIFEST.write_text("[")
    assert em._load() == {}
    em.MANIFEST.write_text("engine_release: v1.2.3\nruntime: nope\n")
    assert em.engine_release() == "v1.2.3" and em.hermes_pin() is None
    em.MANIFEST.write_text("runtime: {pinned_tag: v9.8.7}\n")
    assert em.hermes_pin() == "v9.8.7"


@pytest.mark.parametrize("pin,engine,expected", [
    ("v0.15.12", "v0.15.12-rc1", False),   # okengine#1042 [5]: an rc is older than its final
    ("v0.15.12-rc1", "v0.15.12", True),
    ("v0.15.12-rc1", "v0.15.12-rc2", True),
    ("v0.15.12-rc2", "v0.15.12-rc1", False),
    ("v0.15.11", "v0.15.12-rc1", True),
    ("v1.3.0", "v1.4.0-rc1", True),
])
def test_satisfies_pin_orders_a_prerelease_before_its_final(pin, engine, expected):
    assert _em().satisfies_pin(pin, engine) is expected


def test_release_key_is_the_single_prerelease_aware_order():
    em = _em()
    assert em.release_key("v0.15.12-rc1") < em.release_key("v0.15.12")
    assert em.release_key("0.4.0") == em.release_key("engine-v0.4.0")
    assert em.release_key("main") is None


@pytest.mark.parametrize("version,key", [
    ("v1.2.3", (1, 2, 3, 9, 0)),
    ("engine-v1.2.3", (1, 2, 3, 9, 0)),
    ("v1.2.3.dev4", (1, 2, 3, 0, 4)),
    ("v1.2.3a4", (1, 2, 3, 1, 4)),
    ("v1.2.3-alpha.4", (1, 2, 3, 1, 4)),
    ("v1.2.3b4", (1, 2, 3, 2, 4)),
    ("v1.2.3-beta4", (1, 2, 3, 2, 4)),
    ("v1.2.3c4", (1, 2, 3, 3, 4)),
    ("v1.2.3-pre4", (1, 2, 3, 3, 4)),
    ("v1.2.3-RC4", (1, 2, 3, 3, 4)),
    ("v1.2.3-rc", (1, 2, 3, 3, 0)),
    ("v1.2.3-hotfix", (1, 2, 3, 9, 0)),
])
def test_release_key_spells_every_stage_exactly(version, key):
    """The exact key, not only an order: the stage numbers are a persisted comparison contract."""
    assert _em().release_key(version) == key


def test_release_key_of_none_is_none():
    assert _em().release_key(None) is None


@pytest.mark.parametrize("version,parsed", [
    ("v1.2.3", (1, 2, 3)), ("engine-v4.5.6-rc1", (4, 5, 6)), ("7.8.9", (7, 8, 9)),
    ("main", None), ("", None), (None, None),
])
def test_semver_parses_the_first_release_or_none(version, parsed):
    assert _em()._semver(version) == parsed
