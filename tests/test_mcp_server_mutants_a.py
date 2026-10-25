"""okengine#965 (group A): behavioral contracts for the read MCP's qmd telemetry and its background
index maintainer, the two subsystems whose surviving mutants held `okengine-mcp/server.py` under
the 90% critical mutation floor.

Covered here:

  _record_qmd / _percentiles / _qmd_stats_snapshot / _publish_qmd_stats
      the search-telemetry counters, the nearest-rank latency percentiles and the JSON snapshot
      fleet health reads (okengine#568)
  _refresh_index / _refresh_index_locked
      collection bootstrap, readiness publishing and the rc contract (okengine#80, invariant audit)
  _index_maintainer_step / _index_maintainer
      the periodic full refresh, the debounced incremental update and the poll loop

Each test drives the real functions with a deterministic clock (a stand-in for the `time` module
inside server.py), a scripted qmd, and a real temporary directory where files are involved.
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

# server.py imports `mcp` at module level; skip where that runtime dep is absent (same pattern
# as the other read-MCP tests). Runs in CI where deps are installed.
pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp" / "server.py"
_NAME = "okengine_server_mutants_a"


@pytest.fixture(scope="module")
def srv():
    """A private copy of server.py, loaded once for this file and removed afterwards."""
    spec = importlib.util.spec_from_file_location(_NAME, SRV)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(_NAME, None)


@pytest.fixture
def s(srv, monkeypatch):
    """The module with fresh telemetry/index state and pinned maintenance constants, so every test
    starts from the same documented defaults regardless of the host environment."""
    monkeypatch.setattr(srv, "_QMD_STATS", {
        "search": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
        "maintenance": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
        "saturated": {"search": 0, "maintenance": 0},
    })
    monkeypatch.setattr(srv, "_QMD_LATENCY_SAMPLES", 200)
    monkeypatch.setattr(srv, "_INDEX_STATUS", {"active": True, "ready": False, "error": ""})
    monkeypatch.setattr(srv, "_INDEX_REFRESH_HOURS", 6.0)
    monkeypatch.setattr(srv, "_INDEX_POLL_SECONDS", 30.0)
    monkeypatch.setattr(srv, "_INDEX_MIN_UPDATE_SECONDS", 60.0)
    return srv


class _StopLoop(Exception):
    """Raised by the fake clock's sleep to end the otherwise infinite maintainer loop."""


class _Clock:
    """Deterministic stand-in for the `time` module inside server.py."""

    def __init__(self, now: float = 1000.0, max_sleeps: int | None = None):
        self.now = now
        self.sleeps: list = []
        self.max_sleeps = max_sleeps

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.max_sleeps is not None and len(self.sleeps) >= self.max_sleeps:
            raise _StopLoop
        self.now += seconds


class _Stream:
    """A stderr stand-in that records writes and flushes in order."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def write(self, text):
        self.events.append(("write", text))
        return len(text)

    def flush(self):
        self.events.append(("flush", ""))

    def text(self) -> str:
        return "".join(t for kind, t in self.events if kind == "write")

    def flushed_lines(self) -> list[str]:
        """Lines whose terminating newline was IMMEDIATELY followed by a flush (print(flush=True))."""
        out, line = [], ""
        for i, (kind, text) in enumerate(self.events):
            if kind != "write":
                continue
            line += text
            if line.endswith("\n"):
                nxt = self.events[i + 1][0] if i + 1 < len(self.events) else None
                if nxt == "flush":
                    out.append(line.rstrip("\n"))
                line = ""
        return out


class _SysWithStderr:
    """server.py's view of `sys` with stderr redirected. Patching the real sys.stderr in a fixture
    does not hold: pytest's capture reinstalls its own stream between setup and the test call."""

    def __init__(self, stream):
        self.stderr = stream

    def __getattr__(self, name):
        return getattr(sys, name)


@pytest.fixture
def stderr(s, monkeypatch):
    stream = _Stream()
    monkeypatch.setattr(s, "sys", _SysWithStderr(stream))
    return stream


def _not_interned(text: str) -> str:
    """An equal string that is a distinct object, so identity comparison cannot stand in for value."""
    return "".join(list(text))


# ── _record_qmd ──────────────────────────────────────────────────────────────


def test_saturation_is_matched_by_value_not_identity(s):
    s._record_qmd("search", _not_interned("saturated"))
    assert s._QMD_STATS["saturated"]["search"] == 1, \
        "an outcome EQUAL to 'saturated' must count as saturation even when it is a distinct object"
    assert "saturated" not in s._QMD_STATS["search"], "saturation must not be filed as a search outcome"


def test_saturation_of_a_new_kind_counts_from_zero(s):
    s._record_qmd("probe", "saturated")
    assert s._QMD_STATS["saturated"]["probe"] == 1, "first saturation of an unseen kind must read 1"
    s._record_qmd("probe", "saturated")
    assert s._QMD_STATS["saturated"]["probe"] == 2, "a second saturation must add one, not combine bits"


def test_new_kind_bucket_starts_with_zeroed_counters(s):
    s._record_qmd("probe", "ok")
    assert s._QMD_STATS["probe"] == {"ok": 1, "timeouts": 0, "errors": 0, "latency_ms": []}, \
        "an unseen kind's bucket must start every counter at zero"


def test_novel_outcome_counts_from_zero(s):
    s._record_qmd("search", "cancelled")
    assert s._QMD_STATS["search"]["cancelled"] == 1, "an unseen outcome must start counting at zero"


def test_latency_window_keeps_only_the_newest_sample_at_capacity_one(s, monkeypatch):
    monkeypatch.setattr(s, "_QMD_LATENCY_SAMPLES", 1)
    s._record_qmd("search", "ok", 10.4)
    s._record_qmd("search", "ok", 20.6)
    assert s._QMD_STATS["search"]["latency_ms"] == [21], \
        "the window must trim to exactly the newest _QMD_LATENCY_SAMPLES samples"


def test_latency_window_trims_the_oldest_at_default_capacity(s):
    for ms in range(201):
        s._record_qmd("search", "ok", float(ms))
    samples = s._QMD_STATS["search"]["latency_ms"]
    assert samples == list(range(1, 201)), "201 samples must keep the newest 200, dropping only the first"


# ── _percentiles ─────────────────────────────────────────────────────────────


def _shuffled(values):
    values = list(values)
    random.Random(965).shuffle(values)
    return values


def test_percentiles_of_no_samples_is_empty(s):
    assert s._percentiles([]) == {}, "no samples must publish no percentile keys"


def test_percentiles_of_one_sample_are_that_sample(s):
    assert s._percentiles([7]) == {"p50_ms": 7, "p95_ms": 7, "max_ms": 7, "samples": 1}


def test_percentiles_nearest_rank_for_ten_samples(s):
    got = s._percentiles(_shuffled(range(10, 101, 10)))
    assert got == {"p50_ms": 50, "p95_ms": 90, "max_ms": 100, "samples": 10}, \
        f"nearest-rank over 10..100 must be p50=50 (rank 5), p95=90 (rank 9), max=100; got {got}"


def test_percentiles_nearest_rank_for_one_hundred_samples(s):
    got = s._percentiles(_shuffled(range(1, 101)))
    assert got == {"p50_ms": 50, "p95_ms": 95, "max_ms": 100, "samples": 100}, \
        f"nearest-rank over 1..100 must be p50=50, p95=95; got {got}"


def test_percentiles_nearest_rank_for_two_hundred_samples(s):
    got = s._percentiles(_shuffled(range(1, 201)))
    assert got == {"p50_ms": 100, "p95_ms": 190, "max_ms": 200, "samples": 200}, \
        f"nearest-rank over 1..200 must be p50=100, p95=190; got {got}"


# ── _qmd_stats_snapshot / _publish_qmd_stats ─────────────────────────────────


def test_snapshot_excludes_latency_key_by_value(s, monkeypatch):
    monkeypatch.setattr(s, "_QMD_STATS", {
        "search": {"ok": 2, _not_interned("latency_ms"): [10, 20]},
        "maintenance": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
        "saturated": {"search": 0, "maintenance": 0},
    })
    snap = s._qmd_stats_snapshot()
    assert snap["search"]["calls"] == 2, "calls must count outcomes only, never the latency list"
    assert "latency_ms" not in snap["search"], "the raw latency list must not be published"
    assert snap["search"]["max_ms"] == 20


def test_publish_writes_into_an_existing_directory(s, tmp_path, monkeypatch):
    target = tmp_path / "qmd" / "search-telemetry.json"
    target.parent.mkdir()
    monkeypatch.setattr(s, "_QMD_STATS_PATH", target)
    s._publish_qmd_stats()
    assert target.is_file(), "publishing must succeed when the telemetry directory already exists"


def test_publish_creates_missing_parent_directories(s, tmp_path, monkeypatch):
    target = tmp_path / "data" / "qmd" / "search-telemetry.json"
    monkeypatch.setattr(s, "_QMD_STATS_PATH", target)
    s._publish_qmd_stats()
    assert target.is_file(), "publishing must create every missing directory above the telemetry file"


def test_publish_uses_two_space_indented_json(s, tmp_path, monkeypatch):
    target = tmp_path / "search-telemetry.json"
    monkeypatch.setattr(s, "_QMD_STATS_PATH", target)
    s._publish_qmd_stats()
    text = target.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2) + "\n", \
        "the telemetry file must be 2-space indented JSON with a trailing newline"


# ── _refresh_index / _refresh_index_locked ───────────────────────────────────


def _scripted_qmd(monkeypatch, s, replies):
    """Replace _qmd with a script: `replies` maps the first argument to (rc, output)."""
    calls = []

    def fake_qmd(args, timeout=1800):
        calls.append((list(args), timeout))
        return replies[args[0]]

    monkeypatch.setattr(s, "_qmd", fake_qmd)
    return calls


def test_coalesced_refresh_reports_success_and_flushes_its_notice(s, stderr):
    assert s._INDEX_REFRESH_LOCK.acquire(blocking=False)
    try:
        assert s._refresh_index() is True, "a refresh already in flight must coalesce as success"
    finally:
        s._INDEX_REFRESH_LOCK.release()
    assert "okengine-mcp: qmd index refresh coalesced" in stderr.flushed_lines()


def test_collection_listing_uses_a_sixty_second_timeout(s, monkeypatch):
    calls = _scripted_qmd(monkeypatch, s, {"collection": (0, "qmd://wiki"), "update": (0, "")})
    s._refresh_index_locked()
    assert calls[0] == (["collection", "list"], 60), f"listing must be bounded at 60s; got {calls[0]}"


def test_listing_failure_other_than_missing_binary_still_refreshes(s, monkeypatch):
    calls = _scripted_qmd(monkeypatch, s, {"collection": (255, "qmd://wiki"), "update": (0, "")})
    assert s._refresh_index_locked() is True
    assert (["update"], 1800) in calls, "only rc 127 means qmd is absent; rc 255 must still update"
    assert s._index_unavailable_message() is None, "a successful update must mark search ready"


def test_missing_qmd_notice_is_flushed(s, monkeypatch, stderr):
    _scripted_qmd(monkeypatch, s, {"collection": (127, "")})
    assert s._refresh_index_locked() is True
    assert "okengine-mcp: qmd not found — skipping index maintenance" in stderr.flushed_lines()


def test_registration_and_refresh_notices_are_flushed(s, monkeypatch, stderr):
    _scripted_qmd(monkeypatch, s, {"collection": (0, ""), "update": (0, "")})
    s._refresh_index_locked()
    lines = stderr.flushed_lines()
    assert "okengine-mcp: registered qmd 'wiki' collection (rc=0)" in lines, lines
    assert "okengine-mcp: qmd index refresh rc=0" in lines, lines


def test_signal_killed_update_is_a_failed_refresh(s, monkeypatch):
    _scripted_qmd(monkeypatch, s, {"collection": (0, "qmd://wiki"), "update": (-9, "")})
    assert s._refresh_index_locked() is False, "a negative (signal) rc must not read as success"
    assert "initial qmd refresh failed (rc=-9)" in (s._index_unavailable_message() or ""), \
        "a signal-killed update must leave search marked unavailable"


# ── _index_maintainer_step ───────────────────────────────────────────────────


def _rig(monkeypatch, s, now=1000.0, qmd_duration=0.0, qmd_rc=0):
    """A fake clock, a recording _qmd/_refresh_index that take `qmd_duration` fake seconds, and a
    controllable vault mtime."""
    clock = _Clock(now)
    monkeypatch.setattr(s, "time", clock)
    updates, refreshes = [], []

    def fake_qmd(args, timeout=1800):
        updates.append(list(args))
        clock.now += qmd_duration
        return qmd_rc, ""

    def fake_refresh():
        refreshes.append(clock.now)
        clock.now += qmd_duration
        return True

    monkeypatch.setattr(s, "_qmd", fake_qmd)
    monkeypatch.setattr(s, "_refresh_index", fake_refresh)
    mtime = {"v": 0.0}
    monkeypatch.setattr(s, "_vault_max_mtime", lambda: mtime["v"])
    return clock, updates, refreshes, mtime


def test_full_refresh_cooldown_runs_from_refresh_end(s, monkeypatch):
    clock, _, refreshes, _ = _rig(monkeypatch, s, now=20.0, qmd_duration=30.0)
    state = {"last_full": 0.0, "last_seen": -1.0, "cooldown_until": 0.0}
    s._index_maintainer_step(state)
    assert refreshes == [20.0]
    assert state["last_full"] == 20.0, "the periodic clock must restart at the refresh start"
    # the refresh ran 20 -> 50 (30 s), so cooldown = max(60, 3 x 30) = 90 after it ENDS
    assert state["cooldown_until"] == 140.0, f"expected 50 + 90 = 140, got {state['cooldown_until']}"


def _periodic_state(clock, elapsed):
    return {"last_full": clock.now - elapsed, "last_seen": 0.0, "cooldown_until": 0.0}


@pytest.mark.parametrize("elapsed", [21600.0, 21601.0])
def test_periodic_full_refresh_fires_at_and_after_the_interval(s, monkeypatch, elapsed):
    clock, _, refreshes, _ = _rig(monkeypatch, s, now=100000.0)
    s._index_maintainer_step(_periodic_state(clock, elapsed))
    assert refreshes == [100000.0], f"6 h interval, {elapsed}s elapsed: a full refresh is due"


def test_periodic_full_refresh_waits_until_the_interval(s, monkeypatch):
    clock, _, refreshes, _ = _rig(monkeypatch, s, now=100000.0)
    s._index_maintainer_step(_periodic_state(clock, 21595.0))
    assert refreshes == [], "5 s before the 6 h interval, no full refresh may run"


def test_fractional_refresh_hours_are_honored(s, monkeypatch):
    monkeypatch.setattr(s, "_INDEX_REFRESH_HOURS", 0.5)
    clock, _, refreshes, _ = _rig(monkeypatch, s, now=100000.0)
    s._index_maintainer_step(_periodic_state(clock, 1800.0))
    assert refreshes == [100000.0], "a 0.5 h interval must refresh after 1800 s"


@pytest.mark.parametrize("hours", [0.0, -1.0])
def test_non_positive_refresh_hours_disable_periodic_refresh(s, monkeypatch, hours):
    monkeypatch.setattr(s, "_INDEX_REFRESH_HOURS", hours)
    clock, _, refreshes, _ = _rig(monkeypatch, s, now=100000.0)
    s._index_maintainer_step(_periodic_state(clock, 90000.0))
    assert refreshes == [], f"REFRESH_HOURS={hours} must disable the periodic full refresh"


def test_negative_clock_readings_are_not_treated_as_never_refreshed(s, monkeypatch):
    # time.monotonic() has an undefined reference point; only differences are meaningful
    clock, _, refreshes, _ = _rig(monkeypatch, s, now=-500.0)
    s._index_maintainer_step({"last_full": -1000.0, "last_seen": 0.0, "cooldown_until": 0.0})
    assert refreshes == [], "a refresh 500 s ago on a negative clock is recent, not 'never'"


def test_vault_mtime_moving_backwards_does_not_trigger_an_update(s, monkeypatch):
    clock, updates, _, mtime = _rig(monkeypatch, s)
    mtime["v"] = 400.0
    s._index_maintainer_step({"last_full": clock.now, "last_seen": 500.0, "cooldown_until": 0.0})
    assert updates == [], "only a NEWER vault mtime is a change worth reindexing"


def test_update_runs_exactly_when_the_cooldown_expires(s, monkeypatch):
    clock, updates, _, mtime = _rig(monkeypatch, s)
    mtime["v"] = 10.0
    s._index_maintainer_step({"last_full": clock.now, "last_seen": 0.0, "cooldown_until": clock.now})
    assert updates == [["update"]], "the cooldown is inclusive: at its end the update may run"


def test_incremental_update_logs_duration_and_sets_cooldown_from_its_end(s, monkeypatch, stderr):
    clock, updates, _, mtime = _rig(monkeypatch, s, now=20.0, qmd_duration=30.0)
    mtime["v"] = 10.0
    state = {"last_full": 20.0, "last_seen": 0.0, "cooldown_until": 0.0}
    s._index_maintainer_step(state)
    assert updates == [["update"]]
    assert "okengine-mcp: qmd index update on vault change rc=0 (30.0s)" in stderr.flushed_lines(), \
        stderr.text()
    assert state["cooldown_until"] == 140.0, f"expected 50 + max(60, 90) = 140, got {state['cooldown_until']}"
    assert state["last_seen"] == 10.0


def test_signal_killed_incremental_update_leaves_change_pending(s, monkeypatch):
    clock, updates, _, mtime = _rig(monkeypatch, s, qmd_rc=-9)
    mtime["v"] = 10.0
    state = {"last_full": clock.now, "last_seen": 0.0, "cooldown_until": 0.0}
    s._index_maintainer_step(state)
    assert updates == [["update"]]
    assert state["last_seen"] == 0.0, "a signal-killed update must leave the change pending"
    assert s._INDEX_STATUS["ready"] is False, "a failed update must not mark search ready"


# ── _index_maintainer (the poll loop) ────────────────────────────────────────


def _loop_rig(monkeypatch, s, max_sleeps, refresh_ok=True):
    clock = _Clock(1000.0, max_sleeps=max_sleeps)
    monkeypatch.setattr(s, "time", clock)
    refreshes, updates = [], []

    def fake_refresh():
        refreshes.append(clock.now)
        return refresh_ok

    def fake_qmd(args, timeout=1800):
        updates.append(list(args))
        return 0, ""

    monkeypatch.setattr(s, "_refresh_index", fake_refresh)
    monkeypatch.setattr(s, "_qmd", fake_qmd)
    return clock, refreshes, updates


def test_maintainer_starts_with_a_full_refresh_and_polls(s, monkeypatch):
    clock, refreshes, _ = _loop_rig(monkeypatch, s, max_sleeps=1)
    with pytest.raises(_StopLoop):
        s._index_maintainer()
    assert refreshes == [1000.0], "the first iteration must run the startup full refresh"
    assert clock.sleeps == [30.0], "with POLL_SECONDS=30 the loop must sleep 30 s"


def test_maintainer_retries_pending_changes_after_a_failed_startup_refresh(s, monkeypatch, tmp_path):
    monkeypatch.setattr(s, "_INDEX_MIN_UPDATE_SECONDS", 0.0)
    monkeypatch.setattr(s, "WIKI", tmp_path / "missing-wiki")     # empty vault: max mtime 0.0
    _, refreshes, updates = _loop_rig(monkeypatch, s, max_sleeps=2, refresh_ok=False)
    with pytest.raises(_StopLoop):
        s._index_maintainer()
    assert refreshes == [1000.0]
    assert updates == [["update"]], \
        "after a failed startup refresh nothing is marked indexed, so the next poll must update"


@pytest.mark.parametrize("poll", [0.0, -5.0])
def test_non_positive_poll_falls_back_to_the_refresh_interval(s, monkeypatch, poll):
    monkeypatch.setattr(s, "_INDEX_POLL_SECONDS", poll)
    clock, _, _ = _loop_rig(monkeypatch, s, max_sleeps=1)
    with pytest.raises(_StopLoop):
        s._index_maintainer()
    assert clock.sleeps == [21600.0], f"POLL_SECONDS={poll} must sleep REFRESH_HOURS x 3600"


def test_sub_second_poll_is_honored(s, monkeypatch):
    monkeypatch.setattr(s, "_INDEX_POLL_SECONDS", 0.5)
    clock, _, _ = _loop_rig(monkeypatch, s, max_sleeps=1)
    with pytest.raises(_StopLoop):
        s._index_maintainer()
    assert clock.sleeps == [0.5], "a positive sub-second poll interval must be used as-is"


def test_maintainer_logs_step_errors_and_keeps_polling(s, monkeypatch, stderr):
    clock = _Clock(1000.0, max_sleeps=2)
    monkeypatch.setattr(s, "time", clock)

    def broken_refresh():
        raise RuntimeError("boom")

    monkeypatch.setattr(s, "_refresh_index", broken_refresh)
    with pytest.raises(_StopLoop):
        s._index_maintainer()
    assert clock.sleeps == [30.0, 30.0], "an error in one iteration must not stop the loop"
    assert "okengine-mcp: index maintainer error: boom" in stderr.flushed_lines(), stderr.text()
