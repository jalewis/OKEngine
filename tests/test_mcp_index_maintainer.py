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
    wiki = tmp_path / "wiki"
    (wiki / "e").mkdir(parents=True)
    s = _load(monkeypatch, tmp_path)

    # _vault_max_mtime now ALSO includes directory mtimes (to catch a reshelve os.rename, which keeps
    # the file mtime stable — okengine#326 [30]); backdate the dirs so these assertions isolate the
    # .md FILE-mtime tracking. The dir-mtime path is covered by test_...detects_reshelve_rename below.
    def _backdate_dirs(t=1.0):
        for d in (wiki, wiki / "e"):
            os.utime(d, (t, t))

    _backdate_dirs()
    assert s._vault_max_mtime() == 1.0                      # empty vault -> only the (backdated) dirs

    p = wiki / "e" / "a.md"
    p.write_text("---\ntype: incident\n---\n", encoding="utf-8")
    os.utime(p, (1000, 1000)); _backdate_dirs()
    assert s._vault_max_mtime() == 1000.0                   # a written .md page is detected

    q = wiki / "e" / "b.md"
    q.write_text("x", encoding="utf-8")
    os.utime(q, (2000, 2000)); _backdate_dirs()
    assert s._vault_max_mtime() == 2000.0                   # newest .md write wins -> triggers reindex

    nonmd = wiki / "e" / "c.txt"
    nonmd.write_text("x", encoding="utf-8")
    os.utime(nonmd, (3000, 3000)); _backdate_dirs()
    assert s._vault_max_mtime() == 2000.0                   # a non-.md FILE's own mtime is still ignored


def test_vault_max_mtime_detects_reshelve_rename(tmp_path, monkeypatch):  # okengine#326 [30]
    """A reshelve/reshard moves a page with os.rename — the file mtime is PRESERVED but the source +
    destination directory mtimes bump. Including dir mtimes lets the change detector see the move; the
    old file-only scan missed it and search served the stale path until the 6h full refresh."""
    wiki = tmp_path / "wiki"
    (wiki / "cves" / "2026" / "07").mkdir(parents=True)
    page = wiki / "cves" / "cve-2026-0001.md"
    page.write_text("---\ntype: cve\n---\nx\n", encoding="utf-8")
    s = _load(monkeypatch, tmp_path)
    old = 1_000_000.0
    for p in (page, wiki, wiki / "cves", wiki / "cves" / "2026", wiki / "cves" / "2026" / "07"):
        os.utime(p, (old, old))
    before = s._vault_max_mtime()
    assert before == old
    dest = wiki / "cves" / "2026" / "07" / "cve-2026-0001.md"
    os.rename(page, dest)                                    # file mtime preserved, dir mtimes bump to now
    os.utime(dest, (old, old))                               # the moved FILE keeps its old mtime
    assert s._vault_max_mtime() > before, "a reshelve rename must be caught via the bumped dir mtime"
