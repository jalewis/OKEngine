"""Regression: okengine-reader public-deployment limit policy (#25).

Unit-tests the pure limit logic (no fastapi needed). The endpoint glue in app.py
(403 on disabled export, 429/503 from _guard) wires these into the routes.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIM = REPO / "okengine-reader" / "limits.py"


def _load():
    spec = importlib.util.spec_from_file_location("reader_limits", LIM)
    m = importlib.util.module_from_spec(spec)
    sys.modules["reader_limits"] = m
    spec.loader.exec_module(m)
    return m


def test_flag(monkeypatch):
    m = _load()
    monkeypatch.delenv("OK_TEST_FLAG", raising=False)
    assert m.flag("OK_TEST_FLAG", False) is False
    assert m.flag("OK_TEST_FLAG", True) is True            # unset -> default
    monkeypatch.setenv("OK_TEST_FLAG", "")
    assert m.flag("OK_TEST_FLAG", True) is True             # blank -> default
    monkeypatch.setenv("OK_TEST_FLAG", "1")
    assert m.flag("OK_TEST_FLAG", False) is True
    monkeypatch.setenv("OK_TEST_FLAG", "0")
    assert m.flag("OK_TEST_FLAG", True) is False


def test_intenv(monkeypatch):
    m = _load()
    monkeypatch.delenv("OK_TEST_INT", raising=False)
    assert m.intenv("OK_TEST_INT", 7) == 7                  # unset -> default
    monkeypatch.setenv("OK_TEST_INT", "5")
    assert m.intenv("OK_TEST_INT", 7) == 5
    monkeypatch.setenv("OK_TEST_INT", "notanint")
    assert m.intenv("OK_TEST_INT", 7) == 7                  # bad -> default
    monkeypatch.setenv("OK_TEST_INT", "0")
    assert m.intenv("OK_TEST_INT", 4, lo=1) == 1            # clamped to lo


def test_rate_limiter_disabled_always_allows():
    m = _load()
    rl = m.RateLimiter(0)
    assert all(rl.allow("ip", now=float(i)) for i in range(1000))


def test_rate_limiter_blocks_over_window():
    m = _load()
    rl = m.RateLimiter(3)
    # 3 allowed in the same 60s window, the 4th denied
    assert [rl.allow("1.2.3.4", now=10.0) for _ in range(4)] == [True, True, True, False]
    # a different key has its own budget
    assert rl.allow("5.6.7.8", now=10.0) is True
    # once the window rolls past 60s, the key is allowed again
    assert rl.allow("1.2.3.4", now=80.0) is True
