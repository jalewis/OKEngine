"""Bounded/cancel-safe MCP qmd search regressions (okengine#410)."""
import asyncio
import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp/server.py"


def _load(monkeypatch, vault, *, concurrency=2, queue="0.05"):
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    monkeypatch.setenv("OKENGINE_MCP_QMD_CONCURRENCY", str(concurrency))
    monkeypatch.setenv("OKENGINE_MCP_SEARCH_QUEUE_SECONDS", queue)
    sys.modules.pop("okengine_server", None)
    spec = importlib.util.spec_from_file_location("okengine_server", SRV)
    module = importlib.util.module_from_spec(spec)
    sys.modules["okengine_server"] = module
    spec.loader.exec_module(module)
    return module


def test_concurrent_searches_backpressure_and_cancellation_releases_capacity(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    active = 0
    maximum = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(*_args, **_kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            started.set()
        try:
            await release.wait()
            return "ok"
        finally:
            active -= 1

    monkeypatch.setattr(module, "_run_async", fake_run)

    async def scenario():
        first = asyncio.create_task(module._search("one"))
        second = asyncio.create_task(module._search("two"))
        await started.wait()
        assert await module._search("overflow") == (
            "(search saturated: qmd capacity is busy; retry with backoff)")
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == "ok"
        assert await module._search("after-cancel") == "ok"

    asyncio.run(scenario())
    assert maximum == 2
    assert active == 0


def test_cancel_kills_and_reaps_search_process(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    pidfile = tmp_path / "search.pid"
    helper = tmp_path / "slow.py"
    helper.write_text(
        "import os,sys,time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n")

    async def scenario():
        task = asyncio.create_task(module._run_async([str(helper), str(pidfile)], timeout=20))
        for _ in range(100):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_overlapping_index_refresh_is_coalesced(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_refresh():
        calls.append(True)
        entered.set()
        release.wait(timeout=5)
        return True

    monkeypatch.setattr(module, "_refresh_index_locked", slow_refresh)
    worker = threading.Thread(target=module._refresh_index)
    worker.start()
    assert entered.wait(timeout=2)
    assert module._refresh_index() is True
    release.set()
    worker.join(timeout=2)
    assert calls == [True]


def test_qmd_reports_capacity_timeout_missing_binary_and_process_timeout(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(module._QMD_CAPACITY, "acquire", lambda timeout: False)
    assert module._qmd(["update"]) == (75, "qmd capacity saturated")

    monkeypatch.setattr(module._QMD_CAPACITY, "acquire", lambda timeout: True)
    monkeypatch.setattr(module._QMD_CAPACITY, "release", lambda: None)
    monkeypatch.setattr(module.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))
    assert module._qmd(["update"]) == (127, "qmd not installed")

    class TimedOut:
        returncode = 1

        def communicate(self, timeout=None):
            if timeout is not None:
                raise module.subprocess.TimeoutExpired("qmd", timeout)
            return "", ""

    proc = TimedOut()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(module, "_kill_process_group", lambda p: None)
    assert module._qmd(["update"], timeout=1) == (124, "qmd timed out")


def test_list_pages_filters_invalid_yaml_scope_type_status_and_limit(tmp_path, monkeypatch):
    module = _load(monkeypatch, tmp_path)
    base = tmp_path / "wiki" / "entities"
    base.mkdir()
    (base / "good.md").write_text(
        "---\ntype: actor\nstatus: active\ntitle: Good\nupdated: 2026-07-24\n---\n")
    (base / "other.md").write_text("---\ntype: tool\nstatus: active\n---\n")
    (base / "bad.md").write_text("---\ntype: [\n---\n")
    (base / "INDEX.md").write_text("---\ntype: actor\n---\n")

    assert "refused" in module.list_pages("../entities")
    assert "Good" in module.list_pages("entities", type="actor", status="active", limit=1)
    assert "other" not in module.list_pages("entities", type="actor")
    monkeypatch.setattr(module, "_authorize_read", lambda _path: False)
    assert "(no pages" in module.list_pages("entities")


def test_refresh_index_registers_missing_collection_and_propagates_update_failure(
        tmp_path, monkeypatch, capsys):
    module = _load(monkeypatch, tmp_path)
    replies = iter([(0, "no collections"), (0, "added"), (1, "failed")])
    monkeypatch.setattr(module, "_qmd", lambda *a, **kw: next(replies))
    assert module._refresh_index_locked() is False
    assert "registered qmd" in capsys.readouterr().err

    monkeypatch.setattr(module, "_qmd", lambda *a, **kw: (127, "missing"))
    assert module._refresh_index_locked() is True


def test_latency_samples_are_bounded_so_a_long_lived_server_cannot_grow_them(tmp_path, monkeypatch):
    """The telemetry lives in the search server's own process for as long as it runs. An unbounded
    sample list is a slow leak in the one process that must not fall over — and the p95 would drift
    toward a lifetime average rather than describing recent behaviour."""
    module = _load(monkeypatch, tmp_path)
    cap = module._QMD_LATENCY_SAMPLES
    for n in range(cap + 50):
        module._record_qmd("search", "ok", elapsed_ms=n)
    samples = module._QMD_STATS["search"]["latency_ms"]
    assert len(samples) == cap
    assert samples[0] == 50 and samples[-1] == cap + 49, "the OLDEST samples are the ones dropped"


def test_publishing_telemetry_writes_atomically_and_never_takes_search_down(tmp_path, monkeypatch):
    """Consumed by fleet_health, which reads "no file" as UNMEASURED. A half-written file would be
    read as measured-and-malformed instead, so the swap is atomic. And a failure to publish must
    stay invisible to callers: an unwritable metrics path taking the read-MCP down would trade a
    monitoring gap for an outage."""
    import json

    module = _load(monkeypatch, tmp_path)
    out = tmp_path / "qmd" / "search-telemetry.json"
    monkeypatch.setattr(module, "_QMD_STATS_PATH", out)
    module._record_qmd("search", "ok", elapsed_ms=120)
    module._record_qmd("maintenance", "timeouts")
    module._record_qmd("search", "saturated")

    module._publish_qmd_stats()
    snap = json.loads(out.read_text(encoding="utf-8"))
    assert snap["search"]["ok"] == 1 and snap["search"]["p95_ms"] == 120
    assert snap["maintenance"]["timeouts"] == 1
    assert snap["saturated"] == 1, "saturation is shared — the two kinds compete for the same slots"
    assert snap["saturated_by_kind"] == {"search": 1, "maintenance": 0}
    assert not list(out.parent.glob("*.tmp")), "the temp file is renamed, never left behind"

    monkeypatch.setattr(module, "_QMD_STATS_PATH", tmp_path / "no-such-dir" / "x" / "s.json")
    monkeypatch.setattr(module.Path, "mkdir",
                        lambda self, *a, **kw: (_ for _ in ()).throw(OSError(30, "EROFS")))
    module._publish_qmd_stats()      # must not raise
