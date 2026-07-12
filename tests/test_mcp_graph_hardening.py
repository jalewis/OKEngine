"""Regressions for the MCP graph-tool hardening (okengine#198 + #199).

#198 — _run's timeout must bound WALL-CLOCK, grandchildren included: subprocess.run(timeout=)
kills only the direct child, and a spawned grandchild (kb_graph's `iwe`) survives holding the
stdout pipe, so the post-kill communicate() blocked until IT exited — the "internal timeout is
cosmetic, client hangs to its 300s ceiling" bug. The fix runs the child in its own process group
and killpg's the tree.

#199 — graph_stats serves from the wiki/.backlinks.json artifact (meta + hub ranking) instead of
a live whole-graph IWE rebuild per call; live IWE remains only as the no-artifact fallback.
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp" / "server.py"


def _load(monkeypatch, vault: Path):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    spec = importlib.util.spec_from_file_location("okengine_server", SRV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["okengine_server"] = m
    spec.loader.exec_module(m)
    return m


def test_run_timeout_kills_the_whole_process_group(monkeypatch, tmp_path):
    """The defect (#198): on timeout only the DIRECT child died — the iwe grandchild survived,
    kept burning CPU, and piled up across calls. After _run returns, the grandchild must be DEAD
    (killpg on the group), and the call must return at the budget, not the grandchild's runtime."""
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(monkeypatch, tmp_path)
    pidfile = tmp_path / "grandchild.pid"
    hung = tmp_path / "hung.py"
    # parent spawns a grandchild that records its pid and sleeps 30s; parent then sleeps too
    hung.write_text(
        "import subprocess, sys, time\n"
        "code = 'import os,sys,time; open(sys.argv[1], \"w\").write(str(os.getpid())); time.sleep(30)'\n"
        f"subprocess.Popen([sys.executable, '-c', code, {str(pidfile)!r}])\n"
        "time.sleep(30)\n")
    t0 = time.monotonic()
    out = m._run([str(hung)], timeout=2)
    elapsed = time.monotonic() - t0
    assert out == "(query timed out)"
    assert elapsed < 10, f"call took {elapsed:.1f}s — grandchild extended the timeout"
    # the grandchild must be gone (poll briefly for signal delivery)
    gpid = int(pidfile.read_text())
    for _ in range(20):
        try:
            import os as _os
            _os.kill(gpid, 0)                      # raises when the process is gone
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        import os as _os
        _os.kill(gpid, 9)                          # clean up before failing
        raise AssertionError(f"grandchild {gpid} still alive after _run returned — orphaned iwe")


def _artifact(vault: Path, pages=100, targets=2, edges=7) -> None:
    wiki = vault / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / ".backlinks.json").write_text(json.dumps({
        "version": 1, "built_at": 1783754429, "pages": pages, "targets": targets, "edges": edges,
        "excluded_namespaces": ["sources"],
        "backlinks": {
            "entities/hub": [{"key": f"s{i}", "title": f"S{i}"} for i in range(5)],
            "concepts/minor": [{"key": "s0", "title": "S0"}],
        }}))


def test_graph_stats_serves_from_artifact_without_subprocess(monkeypatch, tmp_path):
    _artifact(tmp_path)
    m = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("live IWE called")))
    out = m.graph_stats()
    assert "pages: 100" in out and "edges: 7" in out
    assert "no inbound link: 98" in out                       # pages - targets
    assert out.index("entities/hub") < out.index("concepts/minor")   # hub ranking desc
    assert "backlinks artifact" in out


def test_graph_stats_falls_back_to_live_iwe_without_artifact(monkeypatch, tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(monkeypatch, tmp_path)
    called = {}
    monkeypatch.setattr(m, "_run", lambda args, **k: (called.setdefault("args", args), "IWE STATS")[1])
    assert m.graph_stats() == "IWE STATS"
    assert any("kb_graph.py" in str(a) for a in called["args"])


def test_graph_stats_falls_back_on_malformed_meta(monkeypatch, tmp_path):
    """An artifact without integer meta (older builder) must not crash or mis-report — fall back."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / ".backlinks.json").write_text(json.dumps({"backlinks": {"a": []}}))
    m = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "_run", lambda *a, **k: "IWE STATS")
    assert m.graph_stats() == "IWE STATS"
