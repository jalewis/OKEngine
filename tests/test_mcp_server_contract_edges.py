"""Tool and maintenance contract coverage for the read-only MCP server."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import runpy
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "okengine-mcp/server.py"


def _load(tmp_path, monkeypatch, name="mcp_server_contract_edges"):
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    spec = importlib.util.spec_from_file_location(name, SERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class Capacity:
    def __init__(self): self.releases = 0
    def acquire(self, **_kwargs): return True
    def release(self): self.releases += 1


def test_search_modes_tier_timeout_and_public_wrapper(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_search_contract_edges")
    cap = Capacity(); monkeypatch.setattr(m, "_QMD_CAPACITY", cap)
    monkeypatch.setattr(m, "_acquire_qmd_capacity", lambda _wait: asyncio.sleep(0, result=True))
    calls = []
    async def run(args, **kwargs):
        calls.append((args, kwargs)); return "result"
    monkeypatch.setattr(m, "_run_async", run)
    assert asyncio.run(m._search("term", "hybrid", 4, " hot ")) == "result"
    assert "query" in calls[0][0] and calls[0][0][-2:] == ["--tier", "hot"]
    assert "term" in calls[0][0]
    assert asyncio.run(m.search("plain")) == "result"
    assert cap.releases == 2

    async def timeout(*_a, **_k): raise TimeoutError
    monkeypatch.setattr(m, "_run_async", timeout)
    assert "timed out" in asyncio.run(m._search("slow"))
    monkeypatch.setattr(m, "_acquire_qmd_capacity", lambda _wait: asyncio.sleep(0, result=False))
    assert "saturated" in asyncio.run(m._search("busy"))


def test_search_distinguishes_bootstrap_index_from_genuine_no_results(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_search_index_readiness")
    before = dict(m._INDEX_STATUS)
    m._set_index_status()
    assert m._INDEX_STATUS == before
    m._set_index_status(active=True, ready=False, error="")
    assert asyncio.run(m._search("known topic")) == (
        "(search unavailable: knowledge index is building; retry later)"
    )
    m._set_index_status(ready=False, error="qmd is not installed")
    assert "qmd is not installed" in asyncio.run(m._search("known topic"))
    m._set_index_status(ready=True, error="")
    monkeypatch.setattr(m, "_QMD_CAPACITY", Capacity())
    monkeypatch.setattr(m, "_acquire_qmd_capacity", lambda _wait: asyncio.sleep(0, result=True))
    monkeypatch.setattr(m, "_run_async", lambda *_a, **_k: asyncio.sleep(0, result="No results"))
    assert asyncio.run(m._search("genuine miss")) == "No results"


def test_get_page_all_results_and_default_caller(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_get_page_edges")
    page = tmp_path / "wiki/entities/a.md"; page.parent.mkdir(); page.write_text("body")
    assert m._caller()["kind"] == "admin"
    assert m.get_page("../../outside") .startswith("(refused")
    monkeypatch.setattr(m, "_authorize_read", lambda _p: False)
    assert "read scope" in m.get_page("entities/a")
    monkeypatch.setattr(m, "_authorize_read", lambda _p: True)
    assert "not found" in m.get_page("entities/missing")
    assert m.get_page("entities/a") == "body"


def test_backlink_artifact_absent_stale_corrupt_shape_cache(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_artifact_edges")
    artifact = tmp_path / "wiki/.backlinks.json"
    assert m._artifact_backlinks() is None
    artifact.write_text("{}")
    old = time.time() - m._BL_ARTIFACT_MAX_AGE - 10
    os.utime(artifact, (old, old))
    assert m._artifact_backlinks() is None
    artifact.write_text("{bad")
    assert m._artifact_backlinks() is None
    artifact.write_text("[]")
    assert m._artifact_backlinks() is None
    artifact.write_text(json.dumps({"backlinks": []}))
    assert m._artifact_backlinks() is None
    doc = {"backlinks": {"entities/a": []}, "pages": 1, "targets": 1, "edges": 0}
    artifact.write_text(json.dumps(doc))
    assert m._artifact_backlinks() == doc["backlinks"]
    assert m._artifact_backlinks() is m._BL_CACHE["map"]
    assert m._artifact_doc() == doc


def test_reference_context_and_graph_stats_artifact_and_degraded_results(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_graph_tool_edges")
    page = tmp_path / "wiki/entities/a.md"; page.parent.mkdir(); page.write_text("[[concepts/x]]")
    backlinks = {"entities/a": [{"key": "sources/s", "title": "S"}]}
    monkeypatch.setattr(m, "_artifact_backlinks", lambda: backlinks)
    assert "Referenced by" in m.find_references("entities/a")
    assert "Incoming backlinks" in m.retrieve_context("entities/a")
    page.unlink()
    assert "page body unavailable" in m.retrieve_context("entities/a")

    monkeypatch.setattr(m, "_artifact_backlinks", lambda: None)
    monkeypatch.setattr(m, "_run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("subprocess")))
    assert "graph unavailable" in m.find_references("missing")
    assert "graph unavailable" in m.retrieve_context("missing")
    monkeypatch.setattr(m, "_authorize_read", lambda _p: False)
    assert "read scope" in m.retrieve_context("missing")

    doc = {"backlinks": backlinks, "pages": 3, "targets": 1, "edges": 2,
           "built_at": time.time(), "excluded_namespaces": ["raw"]}
    monkeypatch.setattr(m, "_artifact_doc", lambda: doc)
    assert "pages: 3" in m.graph_stats()
    monkeypatch.setattr(m, "_artifact_doc", lambda: {"backlinks": {}})
    assert "graph unavailable" in m.graph_stats()


def test_list_pages_every_skip_and_render_branch(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_list_pages_edges")
    base = tmp_path / "wiki/items"; base.mkdir()
    sub = tmp_path / "wiki/domain/items"; sub.mkdir(parents=True)
    (base / "INDEX.md").write_text("ignored")
    (base / "_hidden.md").write_text("ignored")
    (base / "nofm.md").write_text("body")
    (base / "scalar.md").write_text("---\n- scalar\n---\n")
    (base / "wrong-type.md").write_text("---\ntype: other\nstatus: open\n---\n")
    (base / "wrong-status.md").write_text("---\ntype: item\nstatus: closed\n---\n")
    (base / "bad-yaml.md").write_text("---\ntype: [\n---\n")
    (base / "good.md").write_text(
        "---\ntype: item\nstatus: open\nname: Good\ncreated: 2026-01-01\n---\n")
    (sub / "second.md").write_text("---\ntype: item\nstatus: open\n---\n")
    assert "Good" in m.list_pages("items", "item", "open", 10)
    assert "no pages" in m.list_pages("items", "missing", "")
    assert "bad namespace" in m.list_pages("")

    original = Path.read_text
    unreadable = base / "good.md"
    monkeypatch.setattr(Path, "read_text", lambda path, *a, **k: (
        (_ for _ in ()).throw(OSError()) if path == unreadable else original(path, *a, **k)))
    monkeypatch.setattr(m, "_authorize_read", lambda rel: not rel.endswith("second"))
    assert "no pages" in m.list_pages("items", "item", "open")


def test_http_auth_resolution_all_postures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_auth_resolution_edges")
    assert m._resolve_http_auth({"OKENGINE_MCP_ALLOW_UNAUTHENTICATED": "1"}, "127.0.0.1") == (None, None)
    token, warning = m._resolve_http_auth({"OKENGINE_MCP_ALLOW_UNAUTHENTICATED": "1"}, "0.0.0.0")
    assert token is None and "NO authentication" in warning
    assert m._resolve_http_auth({}, "localhost") == (m.DEFAULT_LOCAL_TOKEN, None)
    with pytest.raises(SystemExit): m._resolve_http_auth({}, "0.0.0.0")
    token, warning = m._resolve_http_auth({"OKENGINE_MCP_ALLOW_DEFAULT_TOKEN": "1"}, "0.0.0.0")
    assert token == m.DEFAULT_LOCAL_TOKEN and "built-in DEFAULT" in warning
    assert m._resolve_http_auth({"OKENGINE_MCP_TOKEN": "secret"}, "0.0.0.0") == ("secret", None)


def test_qmd_success_refresh_lock_and_locked_existing_collection(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_qmd_success_edges")
    cap = Capacity(); monkeypatch.setattr(m, "_QMD_CAPACITY", cap)
    proc = SimpleNamespace(returncode=0, communicate=lambda timeout=None: ("out", "err"))
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_k: proc)
    assert m._qmd(["update"]) == (0, "outerr")
    assert cap.releases == 1
    monkeypatch.setattr(m, "_qmd", lambda args, **_k: (0, "qmd://wiki") if args[0] == "collection" else (0, ""))
    assert m._refresh_index_locked()
    monkeypatch.setattr(m, "_refresh_index_locked", lambda: False)
    assert m._refresh_index() is False


def test_vault_mtime_and_index_maintainer_state_transitions(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_index_state_edges")
    page = tmp_path / "wiki/a.md"; page.write_text("x")
    assert m._vault_max_mtime() > 0
    assert m._index_update_cooldown(100) == max(m._INDEX_MIN_UPDATE_SECONDS, 300)

    clock = iter([10.0, 12.0])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(m, "_vault_max_mtime", lambda: 5.0)
    monkeypatch.setattr(m, "_refresh_index", lambda: True)
    state = {"last_full": 0.0, "last_seen": -1.0, "cooldown_until": 0.0}
    m._index_maintainer_step(state)
    assert state["last_seen"] == 5.0

    clock = iter([20.0, 21.0])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(m, "_vault_max_mtime", lambda: 8.0)
    monkeypatch.setattr(m, "_qmd", lambda _args: (0, ""))
    state.update(last_full=19.0, cooldown_until=0.0)
    m._index_maintainer_step(state)
    assert state["last_seen"] == 8.0

    clock = iter([30.0, 31.0])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(m, "_vault_max_mtime", lambda: 9.0)
    monkeypatch.setattr(m, "_qmd", lambda _args: (1, ""))
    state.update(last_full=29.0, cooldown_until=0.0)
    m._index_maintainer_step(state)
    assert state["last_seen"] == 8.0


def test_index_maintainer_catches_errors_then_sleep_ends_loop(tmp_path, monkeypatch):
    class LoopStopped(Exception):
        pass

    m = _load(tmp_path, monkeypatch, "mcp_index_loop_edges")
    monkeypatch.setattr(m, "_index_maintainer_step", lambda _state: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(m.time, "sleep", lambda _seconds: (_ for _ in ()).throw(LoopStopped()))
    with pytest.raises(LoopStopped):
        m._index_maintainer()


def test_remaining_safe_graph_and_list_loop_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_remaining_tool_loops")
    assert m._safe("already.md").name == "already.md"
    refs = {"target": [{"key": "first/nope"}, {"key": "second/match"}]}
    assert m._resolve_key("match", refs) == "second/match"
    monkeypatch.setattr(m, "_artifact_doc", lambda: None)
    monkeypatch.setattr(m, "_run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("subprocess")))
    assert "graph unavailable" in m.graph_stats()

    # The direct namespace is absent; discovery continues to a subdomain namespace.
    sub = tmp_path / "wiki/domain/items"; sub.mkdir(parents=True)
    (sub / "one.md").write_text("---\ntype: item\ntitle: One\n---\n")
    assert "One" in m.list_pages("items")


def test_vault_mtime_error_paths_and_no_update_state(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_mtime_error_edges")
    root = str(tmp_path / "wiki")
    calls = {root: 0, os.path.join(root, "one.md"): 0,
             os.path.join(root, "two.md"): 0}
    monkeypatch.setattr(m.os, "walk", lambda _p: [(root, [], ["skip.txt", "one.md", "two.md"])])
    def stat(path):
        calls[path] = calls.get(path, 0) + 1
        if path in (root, os.path.join(root, "one.md")):
            raise OSError("stat")
        return SimpleNamespace(st_mtime=4.0)
    monkeypatch.setattr(m.os, "stat", stat)
    assert m._vault_max_mtime() == 4.0
    monkeypatch.setattr(m.os, "walk", lambda _p: (_ for _ in ()).throw(OSError("walk")))
    assert m._vault_max_mtime() == 0.0

    clock = iter([10.0, 11.0])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(m, "_vault_max_mtime", lambda: 5.0)
    monkeypatch.setattr(m, "_refresh_index", lambda: False)
    state = {"last_full": 0.0, "last_seen": 1.0, "cooldown_until": 0.0}
    m._index_maintainer_step(state)
    assert state["last_seen"] == 1.0

    monkeypatch.setattr(m.time, "monotonic", lambda: 20.0)
    monkeypatch.setattr(m, "_vault_max_mtime", lambda: 1.0)
    state.update(last_full=19.0, last_seen=2.0, cooldown_until=0.0)
    m._index_maintainer_step(state)
    assert state["last_seen"] == 2.0


def test_stdio_and_http_entrypoints(tmp_path, monkeypatch, capsys):
    pytest.importorskip("mcp")
    from mcp.server.fastmcp import FastMCP

    calls = []
    monkeypatch.setattr(FastMCP, "run", lambda self, transport=None: calls.append(("stdio", transport)))
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_TRANSPORT", "stdio")
    runpy.run_path(str(SERVER), run_name="__main__")
    assert calls == [("stdio", "stdio")]

    app = object()
    monkeypatch.setattr(FastMCP, "streamable_http_app", lambda self: app)
    uvicorn = SimpleNamespace(run=lambda application, **kwargs: calls.append((application, kwargs)))
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setenv("OKENGINE_MCP_TRANSPORT", "http")
    monkeypatch.setenv("OKENGINE_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("OKENGINE_MCP_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv("OKENGINE_MCP_INDEX_REFRESH_HOURS", "1")
    threads = []
    class Thread:
        def __init__(self, **kwargs): threads.append(kwargs)
        def start(self): threads.append("started")
    monkeypatch.setattr("threading.Thread", Thread)
    runpy.run_path(str(SERVER), run_name="__main__")
    assert calls[-1][0] is app and threads[-1] == "started"
    assert "NO authentication" in capsys.readouterr().err

    monkeypatch.delenv("OKENGINE_MCP_ALLOW_UNAUTHENTICATED")
    monkeypatch.setenv("OKENGINE_MCP_TOKEN", "secret")
    monkeypatch.setenv("OKENGINE_MCP_INDEX_REFRESH_HOURS", "0")
    runpy.run_path(str(SERVER), run_name="__main__")
    assert calls[-1][0].__class__.__name__ == "_ScopedAuth"


def test_vault_mtime_nonincreasing_loop_arcs(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "mcp_mtime_loop_arcs")
    first, second = "/wiki/first", "/wiki/second"
    monkeypatch.setattr(m.os, "walk", lambda _p: [
        (first, [], ["older.md", "newer.md"]), (second, [], [])])
    mtimes = {first: 10.0, os.path.join(first, "older.md"): 5.0,
              os.path.join(first, "newer.md"): 15.0, second: 12.0}
    monkeypatch.setattr(m.os, "stat", lambda path: SimpleNamespace(st_mtime=mtimes[path]))
    assert m._vault_max_mtime() == 15.0
