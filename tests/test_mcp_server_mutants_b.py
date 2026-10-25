"""Read-MCP admission, helper-process and startup contracts (okengine#965, group B).

`okengine-mcp/server.py` bounds every qmd call with a shared capacity gate, runs helpers in their
own process group, records search and maintenance telemetry, and parses its tuning knobs from the
environment at import time. These tests pin the observable side of each of those duties:

  * environment knobs: blank, zero, negative and explicit values clamp to the documented results
  * _acquire_qmd_capacity: waits exactly until its deadline, polls at least every 50 ms, never
    busy-spins, and picks up a slot freed mid-wait
  * _search / _qmd: argument construction, output truncation, latency and outcome telemetry,
    immediately flushed operator log lines
  * _run / _run_async / _qmd: helpers start in their own session and default to their timeouts
  * the ``__main__`` block: HTTP startup arms the index maintainer only for a positive refresh
    interval, and the HTTP app accepts the bridge service-name Host header

Every module load uses explicit environment values and its own module name, so no state leaks
into other test files.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("mcp")

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp/server.py"
MODULE_NAME = "okengine_server_mutants_b"

_KNOBS = (
    "OKENGINE_MCP_QMD_CONCURRENCY", "OKENGINE_MCP_SEARCH_QUEUE_SECONDS",
    "OKENGINE_MCP_SEARCH_TIMEOUT_SECONDS", "OKENGINE_MCP_LIMIT_MAX", "OKENGINE_BACKLINKS_MAX_AGE",
    "OKENGINE_MCP_INDEX_REFRESH_HOURS", "OKENGINE_MCP_INDEX_POLL_SECONDS",
    "OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS", "OKENGINE_QMD_BIN", "OKENGINE_MCP_TRANSPORT",
    "OKENGINE_MCP_HOST", "OKENGINE_MCP_TOKEN", "OKENGINE_MCP_ALLOW_UNAUTHENTICATED",
    "OKENGINE_MCP_ALLOW_DEFAULT_TOKEN", "PORT",
)


def _prepare_env(monkeypatch, vault: Path, env: dict | None = None) -> None:
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    for name in _KNOBS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    monkeypatch.setenv("OKENGINE_MCP_QMD_STATS", str(vault / "qmd" / "search-telemetry.json"))
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)


def _load(monkeypatch, vault: Path, env: dict | None = None, name: str = MODULE_NAME):
    """A fresh server module under explicit env values; removed from sys.modules afterwards."""
    _prepare_env(monkeypatch, vault, env)
    spec = importlib.util.spec_from_file_location(name, SRV)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _telemetry(module) -> dict:
    return json.loads(module._QMD_STATS_PATH.read_text(encoding="utf-8"))


class FakeClock:
    """A monotonic clock that only moves when the code under test sleeps (or is told to)."""

    def __init__(self, now: float, max_sleeps: int = 200):
        self.now = now
        self.sleeps: list[float] = []
        self.max_sleeps = max_sleeps
        self.on_sleep = None

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if len(self.sleeps) > self.max_sleeps:
            raise AssertionError(
                f"capacity wait never terminated: {len(self.sleeps)} polls, last delays "
                f"{self.sleeps[-5:]} (busy-spin or a clock running backward)")
        self.now += delay
        if self.on_sleep is not None:
            self.on_sleep(len(self.sleeps))


class ScriptedClock:
    """A monotonic clock that returns a scripted sequence of readings, then repeats the last."""

    def __init__(self, *readings: float):
        self.readings = list(readings)

    def monotonic(self) -> float:
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


class FlushRecorder:
    """A stderr stand-in that records whether each written line was flushed through."""

    def __init__(self):
        self.text = ""
        self.flushed_upto = 0

    def write(self, data: str) -> int:
        self.text += data
        return len(data)

    def flush(self) -> None:
        self.flushed_upto = len(self.text)


def _install_fake_clock(monkeypatch, module, clock: FakeClock) -> None:
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(module, "asyncio", types.SimpleNamespace(sleep=clock.sleep))


def _occupy_all_capacity(module) -> None:
    for _ in range(module._QMD_CONCURRENCY):
        assert module._QMD_CAPACITY.acquire(blocking=False), "fixture could not take a qmd slot"


# ── environment knobs ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("0", 1), ("-3", 1), ("", 2), ("5", 5),
])
def test_qmd_concurrency_knob_is_clamped_to_at_least_one(tmp_path, monkeypatch, raw, expected):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_QMD_CONCURRENCY": raw})
    assert module._QMD_CONCURRENCY == expected, (
        f"OKENGINE_MCP_QMD_CONCURRENCY={raw!r} must yield {expected} slots "
        f"(blank = default 2, values below 1 clamp to 1), got {module._QMD_CONCURRENCY}")


@pytest.mark.parametrize("raw, expected", [
    ("-5", 0.0), ("", 10.0), ("3", 3.0),
])
def test_search_queue_knob_is_clamped_to_non_negative(tmp_path, monkeypatch, raw, expected):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_SEARCH_QUEUE_SECONDS": raw})
    assert module._SEARCH_QUEUE_SECONDS == expected, (
        f"OKENGINE_MCP_SEARCH_QUEUE_SECONDS={raw!r} must yield {expected}s "
        f"(blank = default 10, negative clamps to 0), got {module._SEARCH_QUEUE_SECONDS}")


@pytest.mark.parametrize("raw, expected", [
    ("0", 1.0), ("", 120.0), ("30", 30.0),
])
def test_search_timeout_knob_is_clamped_to_at_least_one_second(tmp_path, monkeypatch, raw, expected):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_SEARCH_TIMEOUT_SECONDS": raw})
    assert module._SEARCH_TIMEOUT_SECONDS == expected, (
        f"OKENGINE_MCP_SEARCH_TIMEOUT_SECONDS={raw!r} must yield {expected}s "
        f"(blank = default 120, below 1 clamps to 1), got {module._SEARCH_TIMEOUT_SECONDS}")


@pytest.mark.parametrize("raw, expected", [("", 100), ("7", 7)])
def test_limit_max_knob_caps_caller_limits(tmp_path, monkeypatch, raw, expected):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_LIMIT_MAX": raw})
    assert module._clamp_limit(10**6, 8) == expected, (
        f"OKENGINE_MCP_LIMIT_MAX={raw!r} must cap an absurd caller limit at {expected}")


def test_backlinks_max_age_knob_has_a_one_hour_floor(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_BACKLINKS_MAX_AGE": "60"})
    assert module._BL_ARTIFACT_MAX_AGE == 3600, (
        "a backlinks max age below one hour must clamp to 3600s, "
        f"got {module._BL_ARTIFACT_MAX_AGE}")


@pytest.mark.parametrize("knob, attr", [
    ("OKENGINE_MCP_INDEX_REFRESH_HOURS", "_INDEX_REFRESH_HOURS"),
    ("OKENGINE_MCP_INDEX_POLL_SECONDS", "_INDEX_POLL_SECONDS"),
    ("OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS", "_INDEX_MIN_UPDATE_SECONDS"),
])
def test_blank_index_knob_disables_the_interval(tmp_path, monkeypatch, knob, attr):
    module = _load(monkeypatch, tmp_path, {knob: ""})
    assert getattr(module, attr) == 0.0, (
        f"a blank {knob} must read as 0 (disabled), got {getattr(module, attr)!r}")


@pytest.mark.parametrize("knob, attr", [
    ("OKENGINE_MCP_INDEX_POLL_SECONDS", "_INDEX_POLL_SECONDS"),
    ("OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS", "_INDEX_MIN_UPDATE_SECONDS"),
])
def test_explicit_index_knob_value_is_honored(tmp_path, monkeypatch, knob, attr):
    module = _load(monkeypatch, tmp_path, {knob: "5"})
    assert getattr(module, attr) == 5.0, (
        f"{knob}=5 must yield 5.0 seconds, got {getattr(module, attr)!r}")


def test_latency_telemetry_keeps_only_the_newest_200_samples(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    for ms in range(205):
        module._record_qmd("search", "ok", float(ms))
    snap = module._qmd_stats_snapshot()["search"]
    assert snap["samples"] == 200, f"latency window must hold 200 samples, got {snap['samples']}"
    assert snap["max_ms"] == 204, "the window must keep the NEWEST samples"


def test_fresh_module_publishes_zeroed_outcome_counters(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    module._publish_qmd_stats()
    snap = _telemetry(module)
    for kind in ("search", "maintenance"):
        counts = {k: snap[kind][k] for k in ("ok", "timeouts", "errors", "calls")}
        assert counts == {"ok": 0, "timeouts": 0, "errors": 0, "calls": 0}, (
            f"a fresh server must report no {kind} calls yet, got {counts}")


def test_activating_maintenance_reports_building_until_first_refresh(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    module._set_index_status(active=True)
    assert module._index_unavailable_message() == (
        "(search unavailable: knowledge index is building; retry later)"), (
        "an active maintainer that has not completed a refresh must not claim the index is ready")


def test_http_app_accepts_the_bridge_service_name_host(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    module = _load(monkeypatch, tmp_path)
    app = module.mcp.streamable_http_app()
    with TestClient(app, base_url="http://okengine-mcp:8730") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                             "clientInfo": {"name": "probe", "version": "1"}}},
            headers={"Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, (
        "a bridge peer reaching the MCP by service name must not be rejected by DNS-rebinding "
        f"host checks (okengine#138); got {response.status_code} {response.text[:120]!r}")


# ── _acquire_qmd_capacity ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", [100.0, 0.0625])
def test_capacity_wait_gives_up_exactly_at_its_deadline(tmp_path, monkeypatch, origin):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_QMD_CONCURRENCY": "1"})
    clock = FakeClock(origin)
    _install_fake_clock(monkeypatch, module, clock)
    _occupy_all_capacity(module)

    got = asyncio.run(module._acquire_qmd_capacity(0.125))

    assert got is False, "a saturated gate must refuse once the wait expires"
    assert clock.now == pytest.approx(origin + 0.125), (
        f"the wait must end at its deadline, not before or after: waited "
        f"{clock.now - origin:.4f}s for 0.125s (polls {clock.sleeps})")
    assert clock.sleeps and all(0 < d <= 0.05 for d in clock.sleeps), (
        f"each poll must sleep a positive interval of at most 50 ms, got {clock.sleeps}")


def test_zero_capacity_wait_refuses_without_polling(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_QMD_CONCURRENCY": "1"})
    clock = FakeClock(100.0, max_sleeps=20)
    _install_fake_clock(monkeypatch, module, clock)
    _occupy_all_capacity(module)

    got = asyncio.run(module._acquire_qmd_capacity(0.0))

    assert got is False, "a zero-length wait on a saturated gate must refuse"
    assert clock.sleeps == [], f"a zero-length wait must not poll, polled {clock.sleeps}"


def test_capacity_freed_mid_wait_is_acquired_on_the_next_poll(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path, {"OKENGINE_MCP_QMD_CONCURRENCY": "1"})
    clock = FakeClock(100.0)
    _install_fake_clock(monkeypatch, module, clock)
    _occupy_all_capacity(module)
    clock.on_sleep = lambda n: module._QMD_CAPACITY.release() if n == 2 else None

    got = asyncio.run(module._acquire_qmd_capacity(1.0))

    assert got is True, "a slot released during the wait must be taken"
    assert clock.now == pytest.approx(100.1), (
        f"the freed slot must be taken on the very next 50 ms poll, took {clock.now - 100.0:.3f}s")
    module._QMD_CAPACITY.release()


# ── _search ───────────────────────────────────────────────────────────────────────────────────


def _search_module(monkeypatch, vault, *, output="hit", advance=0.0, raises=None, env=None):
    module = _load(monkeypatch, vault, env)
    clock = FakeClock(0.5)
    monkeypatch.setattr(module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    calls: list[list[str]] = []

    async def helper(args, extra_env=None, timeout=90):
        calls.append(list(args))
        clock.now += advance
        if raises is not None:
            raise raises
        return output

    monkeypatch.setattr(module, "_run_async", mock.create_autospec(module._run_async,
                                                                    side_effect=helper))
    return module, calls


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def test_search_defaults_to_lexical_mode_and_eight_results(tmp_path, monkeypatch):
    module, calls = _search_module(monkeypatch, tmp_path)
    asyncio.run(module._search("ransomware"))
    assert (_flag(calls[0], "--mode"), _flag(calls[0], "--limit")) == ("search", "8"), (
        f"a bare search must run qmd lexical search for 8 results, ran {calls[0]}")


def test_search_mode_decoded_from_a_request_stays_lexical(tmp_path, monkeypatch):
    module, calls = _search_module(monkeypatch, tmp_path)
    mode = json.loads('"search"')       # a wire-decoded string, not a compile-time constant
    asyncio.run(module._search("ransomware", mode=mode))
    assert _flag(calls[0], "--mode") == "search", (
        f"mode='search' from a JSON request must select lexical search, ran {calls[0]}")


@pytest.mark.parametrize("mode", ["hybrid", "vector"])
def test_search_any_other_mode_runs_the_hybrid_query(tmp_path, monkeypatch, mode):
    module, calls = _search_module(monkeypatch, tmp_path)
    asyncio.run(module._search("ransomware", mode=mode))
    assert _flag(calls[0], "--mode") == "query", (
        f"mode={mode!r} must map to qmd 'query', ran {calls[0]}")


def test_search_with_an_unparseable_limit_falls_back_to_eight(tmp_path, monkeypatch):
    module, calls = _search_module(monkeypatch, tmp_path)
    asyncio.run(module._search("ransomware", limit="many"))
    assert _flag(calls[0], "--limit") == "8", (
        f"an unparseable limit must fall back to 8 results, ran {calls[0]}")


def test_search_output_is_truncated_to_8000_characters(tmp_path, monkeypatch):
    module, _ = _search_module(monkeypatch, tmp_path, output="x" * 9000)
    result = asyncio.run(module._search("ransomware"))
    assert len(result) == 8000, f"search output must be cut to 8000 chars, got {len(result)}"


def test_search_records_its_latency_in_milliseconds(tmp_path, monkeypatch):
    module, _ = _search_module(monkeypatch, tmp_path, advance=2.0)
    asyncio.run(module._search("ransomware"))
    snap = _telemetry(module)["search"]
    assert (snap["ok"], snap["max_ms"]) == (1, 2000), (
        f"a 2.0s search must publish one ok call of 2000 ms, published {snap}")


def test_search_timeout_records_its_latency_in_milliseconds(tmp_path, monkeypatch):
    module, _ = _search_module(monkeypatch, tmp_path, advance=2.0, raises=TimeoutError())
    result = asyncio.run(module._search("ransomware"))
    snap = _telemetry(module)["search"]
    assert result == "(search timed out: narrow the query or retry later)"
    assert (snap["timeouts"], snap["max_ms"]) == (1, 2000), (
        f"a search timing out after 2.0s must publish one timeout of 2000 ms, published {snap}")


def test_search_timeout_log_line_is_flushed_immediately(tmp_path, monkeypatch):
    module, _ = _search_module(monkeypatch, tmp_path, raises=TimeoutError())
    err = FlushRecorder()
    monkeypatch.setattr(sys, "stderr", err)
    asyncio.run(module._search("ransomware"))
    assert "SEARCH_TIMEOUT" in err.text, f"a search timeout must be logged, stderr was {err.text!r}"
    assert err.flushed_upto == len(err.text), "the SEARCH_TIMEOUT line must be flushed at once"


def test_search_saturation_log_line_is_flushed_immediately(tmp_path, monkeypatch):
    module, _ = _search_module(
        monkeypatch, tmp_path,
        env={"OKENGINE_MCP_QMD_CONCURRENCY": "1", "OKENGINE_MCP_SEARCH_QUEUE_SECONDS": "0"})
    _occupy_all_capacity(module)
    err = FlushRecorder()
    monkeypatch.setattr(sys, "stderr", err)
    result = asyncio.run(module._search("ransomware"))
    assert result.startswith("(search saturated"), f"a saturated gate must refuse, got {result!r}"
    assert "SEARCH_SATURATED" in err.text, f"saturation must be logged, stderr was {err.text!r}"
    assert err.flushed_upto == len(err.text), "the SEARCH_SATURATED line must be flushed at once"


# ── helper processes: _run / _run_async / _qmd ────────────────────────────────────────────────


_SID_SCRIPT = "import os, sys\nsys.stdout.write(str(os.getsid(0)))\n"


def _sid_script(tmp_path: Path) -> Path:
    script = tmp_path / "print_sid.py"
    script.write_text(_SID_SCRIPT, encoding="utf-8")
    return script


def _fake_qmd(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-qmd"
    script.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _popen_stub(monkeypatch, module, stdout="out"):
    popen = mock.create_autospec(subprocess.Popen)
    proc = popen.return_value
    proc.communicate.return_value = (stdout, "")
    proc.returncode = 0
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    return proc


def test_run_helper_starts_in_its_own_session(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    sid = module._run([str(_sid_script(tmp_path))], timeout=20)
    assert int(sid) != os.getsid(0), (
        "a helper must run in its own session so a timeout can kill its whole process group")


def test_run_helper_returns_decoded_text(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    out = module._run([str(_sid_script(tmp_path))], timeout=20)
    assert isinstance(out, str) and out.isdigit(), (
        f"a helper's stdout must come back as decoded text, got {out!r}")


def test_run_helper_defaults_to_a_90_second_timeout(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    proc = _popen_stub(monkeypatch, module)
    assert module._run(["helper.py"]) == "out"
    proc.communicate.assert_called_once_with(timeout=90)


def test_async_helper_starts_in_its_own_session(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    sid = asyncio.run(module._run_async([str(_sid_script(tmp_path))], timeout=20))
    assert int(sid) != os.getsid(0), (
        "an async helper must run in its own session so cancellation kills its process group")


def test_async_helper_defaults_to_a_90_second_timeout(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    real_wait_for = asyncio.wait_for
    wait_for = mock.create_autospec(real_wait_for, side_effect=real_wait_for)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    out = asyncio.run(module._run_async([str(_sid_script(tmp_path))]))
    assert out.isdigit(), f"the helper output must come back, got {out!r}"
    assert wait_for.call_args.kwargs["timeout"] == 90, (
        f"the async helper must default to a 90s bound, used {wait_for.call_args}")


def test_qmd_defaults_to_a_1800_second_timeout(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    proc = _popen_stub(monkeypatch, module)
    assert module._qmd(["update"]) == (0, "out")
    proc.communicate.assert_called_once_with(timeout=1800)


def test_qmd_returns_text_output(tmp_path, monkeypatch):
    qmd = _fake_qmd(tmp_path, "sys.stdout.write('indexed 3 files\\n')")
    module = _load(monkeypatch, tmp_path, {"OKENGINE_QMD_BIN": str(qmd)})
    assert module._qmd(["update"], timeout=20) == (0, "indexed 3 files\n"), (
        "qmd output must be returned as decoded text")


def test_qmd_runs_in_its_own_session(tmp_path, monkeypatch):
    qmd = _fake_qmd(tmp_path, "sys.stdout.write(str(os.getsid(0)))")
    module = _load(monkeypatch, tmp_path, {"OKENGINE_QMD_BIN": str(qmd)})
    rc, out = module._qmd(["update"], timeout=20)
    assert rc == 0 and int(out) != os.getsid(0), (
        "qmd must run in its own session so a timeout can kill the whole group")


@pytest.mark.parametrize("body, outcome", [
    ("sys.exit(0)", "ok"),
    ("sys.exit(1)", "errors"),
    ("sys.exit(3)", "errors"),
    ("os.kill(os.getpid(), 9)", "errors"),
])
def test_qmd_counts_only_a_zero_exit_as_success(tmp_path, monkeypatch, body, outcome):
    qmd = _fake_qmd(tmp_path, body)
    module = _load(monkeypatch, tmp_path, {"OKENGINE_QMD_BIN": str(qmd)})
    module._qmd(["update"], timeout=20)
    snap = _telemetry(module)["maintenance"]
    counts = {"ok": snap["ok"], "errors": snap["errors"]}
    expected = {"ok": 1 if outcome == "ok" else 0, "errors": 1 if outcome == "errors" else 0}
    assert counts == expected, f"qmd `{body}` must count as {outcome}, published {counts}"


def test_qmd_records_completion_latency_in_milliseconds(tmp_path, monkeypatch):
    qmd = _fake_qmd(tmp_path, "sys.exit(0)")
    module = _load(monkeypatch, tmp_path, {"OKENGINE_QMD_BIN": str(qmd)})
    monkeypatch.setattr(module, "time", ScriptedClock(0.5, 2.5))
    module._qmd(["update"], timeout=20)
    snap = _telemetry(module)["maintenance"]
    assert (snap["ok"], snap["max_ms"]) == (1, 2000), (
        f"a 2.0s qmd run must publish one ok call of 2000 ms, published {snap}")


def test_qmd_timeout_records_latency_in_milliseconds(tmp_path, monkeypatch):
    qmd = _fake_qmd(tmp_path, "time.sleep(30)")
    module = _load(monkeypatch, tmp_path, {"OKENGINE_QMD_BIN": str(qmd)})
    monkeypatch.setattr(module, "time", ScriptedClock(0.5, 2.5))
    assert module._qmd(["update"], timeout=0.3) == (124, "qmd timed out")
    snap = _telemetry(module)["maintenance"]
    assert (snap["timeouts"], snap["max_ms"]) == (1, 2000), (
        f"a qmd run timing out after 2.0s must publish one timeout of 2000 ms, published {snap}")


# ── __main__ startup ──────────────────────────────────────────────────────────────────────────


def _run_main(monkeypatch, vault, env):
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    _prepare_env(monkeypatch, vault, env)
    serve = mock.create_autospec(uvicorn.run)
    thread = mock.create_autospec(threading.Thread)
    stdio = mock.create_autospec(FastMCP.run)
    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setattr(threading, "Thread", thread)
    monkeypatch.setattr(FastMCP, "run", stdio)
    namespace = runpy.run_path(str(SRV), run_name="__main__")
    return namespace, serve, thread, stdio


def test_http_startup_arms_the_index_maintainer(tmp_path, monkeypatch):
    namespace, serve, thread, _ = _run_main(monkeypatch, tmp_path, {
        "OKENGINE_MCP_TRANSPORT": "http", "OKENGINE_MCP_INDEX_REFRESH_HOURS": "0.5"})
    thread.assert_called_once_with(target=namespace["_index_maintainer"],
                                   name="qmd-index-maintainer", daemon=True)
    thread.return_value.start.assert_called_once_with()
    assert namespace["_index_unavailable_message"]() == (
        "(search unavailable: knowledge index is building; retry later)"), (
        "search must report the index as building until the maintainer's first refresh")
    assert serve.call_args.kwargs["host"] == "127.0.0.1"


@pytest.mark.parametrize("hours", ["0", "-2"])
def test_http_startup_without_a_refresh_interval_skips_the_maintainer(tmp_path, monkeypatch, hours):
    namespace, serve, thread, _ = _run_main(monkeypatch, tmp_path, {
        "OKENGINE_MCP_TRANSPORT": "http", "OKENGINE_MCP_INDEX_REFRESH_HOURS": hours})
    thread.assert_not_called()
    assert namespace["_index_unavailable_message"]() is None, (
        f"with refresh hours {hours} no maintainer runs, so search must not report building")
    serve.assert_called_once()


def test_http_startup_warning_is_flushed_immediately(tmp_path, monkeypatch):
    err = FlushRecorder()
    monkeypatch.setattr(sys, "stderr", err)
    _run_main(monkeypatch, tmp_path, {
        "OKENGINE_MCP_TRANSPORT": "http", "OKENGINE_MCP_HOST": "0.0.0.0",
        "OKENGINE_MCP_ALLOW_UNAUTHENTICATED": "1", "OKENGINE_MCP_INDEX_REFRESH_HOURS": "0"})
    assert "WARNING: okengine-mcp" in err.text, f"exposed startup must warn, stderr {err.text!r}"
    assert err.flushed_upto == len(err.text), "the startup WARNING must be flushed at once"


def test_importing_the_module_does_not_start_serving(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP

    stdio = mock.create_autospec(FastMCP.run)
    monkeypatch.setattr(FastMCP, "run", stdio)
    _load(monkeypatch, tmp_path, name="OKMCP_IMPORTED")
    stdio.assert_not_called()
