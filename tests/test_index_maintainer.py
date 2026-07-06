"""Regression: change-triggered qmd reindexing is debounced.

Incident (2026-07-03, fleet health): during overlapping backfill lanes every
page write triggered an incremental `qmd update` that took 60-120s on a ~6k-page
vault, so the mcp container spent most of its 2-CPU budget reindexing and
read/write MCP tool calls starved past the client's 300s timeout. The fix gives
change-triggered updates a cooldown — max(_INDEX_MIN_UPDATE_SECONDS,
_INDEX_UPDATE_DUTY x the previous update's duration) — while writes landing
inside the cooldown coalesce into the next single update.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

# server.py imports `mcp` at module level; skip where that runtime dep is absent
# (same pattern as the write_server tests). Runs in CI where deps are installed.
pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp" / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("okengine_server_idx", SRV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["okengine_server_idx"] = m
    spec.loader.exec_module(m)
    return m


class _Clock:
    """Deterministic stand-in for the `time` module inside server.py."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _rig(monkeypatch, qmd_duration=0.0, min_update=60.0):
    """Load server.py with a fake clock, a recording _qmd that takes
    `qmd_duration` fake-seconds, and a controllable vault mtime."""
    s = _load()
    clock = _Clock()
    monkeypatch.setattr(s, "time", clock)
    monkeypatch.setattr(s, "_INDEX_MIN_UPDATE_SECONDS", min_update)
    updates = []

    def fake_qmd(args, timeout=1800):
        updates.append(clock.now)
        clock.now += qmd_duration
        return 0, ""

    monkeypatch.setattr(s, "_qmd", fake_qmd)
    mtime = {"v": 0.0}
    monkeypatch.setattr(s, "_vault_max_mtime", lambda: mtime["v"])
    # state as if the startup full refresh already ran, cooldown expired
    state = {"last_full": clock.now, "last_seen": 0.0, "cooldown_until": 0.0}
    return s, clock, state, updates, mtime


def test_cooldown_formula_floors_at_min_and_scales_with_duration():
    s = _load()
    assert s._index_update_cooldown(0.0) == s._INDEX_MIN_UPDATE_SECONDS
    slow = s._index_update_cooldown(100.0)
    assert slow == s._INDEX_UPDATE_DUTY * 100.0     # ~25% duty cycle at DUTY=3
    assert slow > s._INDEX_MIN_UPDATE_SECONDS


def test_write_burst_coalesces_into_one_update_per_cooldown(monkeypatch):
    """A write every poll must NOT mean an update every poll (the incident)."""
    s, clock, state, updates, mtime = _rig(monkeypatch, qmd_duration=90.0)
    for _ in range(40):                              # ~20 fake-minutes of burst
        mtime["v"] = clock.now                       # a page was just written
        s._index_maintainer_step(state)
        clock.now += 30.0                            # poll interval
    # 90s updates -> 270s cooldown -> one update per ~6 polls, not per poll
    assert 2 <= len(updates) <= 5
    gaps = [b - a for a, b in zip(updates, updates[1:])]
    assert all(g >= 270.0 for g in gaps)


def test_pending_writes_survive_the_cooldown(monkeypatch):
    """Writes landing during the cooldown are indexed by the NEXT update, not lost."""
    s, clock, state, updates, mtime = _rig(monkeypatch, qmd_duration=0.0)
    mtime["v"] = clock.now
    s._index_maintainer_step(state)                  # update #1, cooldown starts
    assert len(updates) == 1
    clock.now += 10.0
    mtime["v"] = clock.now                           # write during cooldown
    s._index_maintainer_step(state)
    assert len(updates) == 1                         # debounced...
    clock.now += 60.0                                # ...cooldown expires
    s._index_maintainer_step(state)
    assert len(updates) == 2                         # ...and the write is picked up
    assert state["last_seen"] == mtime["v"]


def test_idle_vault_never_updates(monkeypatch):
    s, clock, state, updates, mtime = _rig(monkeypatch)
    for _ in range(10):
        s._index_maintainer_step(state)
        clock.now += 30.0
    assert updates == []


def test_first_write_after_idle_indexes_on_next_poll(monkeypatch):
    """The okengine#80 write->recall loop: no added latency when the vault is quiet."""
    s, clock, state, updates, mtime = _rig(monkeypatch)
    clock.now += 3600.0                              # long idle, cooldown long expired
    mtime["v"] = clock.now
    s._index_maintainer_step(state)
    assert len(updates) == 1                         # indexed immediately on this poll
