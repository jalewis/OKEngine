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
