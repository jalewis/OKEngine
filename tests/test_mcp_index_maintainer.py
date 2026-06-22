"""okengine#80: the mcp index maintainer reindexes promptly on a vault change, so an agent's
just-written page is searchable within seconds (the write -> recall loop), instead of waiting
for the periodic full refresh. Tests the cheap change-detector the poll loop gates on."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

# server.py imports `mcp` at module level; skip where that runtime dep is absent (same pattern
# as the auth/write-server tests). Runs in CI where deps are installed.
pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp" / "server.py"


def _load(monkeypatch, vault: Path):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    sys.modules.pop("okengine_server", None)
    spec = importlib.util.spec_from_file_location("okengine_server", SRV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["okengine_server"] = m
    spec.loader.exec_module(m)
    return m


def test_vault_max_mtime_tracks_newest_write(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "e").mkdir(parents=True)
    s = _load(monkeypatch, tmp_path)
    assert s._vault_max_mtime() == 0.0                      # empty vault -> nothing to index

    p = tmp_path / "wiki" / "e" / "a.md"
    p.write_text("---\ntype: incident\n---\n", encoding="utf-8")
    os.utime(p, (1000, 1000))
    assert s._vault_max_mtime() == 1000.0                   # a written page is detected

    q = tmp_path / "wiki" / "e" / "b.md"
    q.write_text("x", encoding="utf-8")
    os.utime(q, (2000, 2000))
    assert s._vault_max_mtime() == 2000.0                   # newest write wins -> triggers reindex

    nonmd = tmp_path / "wiki" / "e" / "c.txt"
    nonmd.write_text("x", encoding="utf-8")
    os.utime(nonmd, (3000, 3000))
    assert s._vault_max_mtime() == 2000.0                   # non-.md ignored
