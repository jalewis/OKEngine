"""The write path refuses a placeholder source URL (okengine#886).

A source's URL is its identity. One live vault filed 20 different papers under
`https://arxiv.org/abs/[pending]`, so every URL-keyed writer and check treated them as one.
The guard refuses a write that INTRODUCES a placeholder; a page already stored with one stays
writable so its other fields and its repair are never blocked.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "okengine-mcp" / "write_server.py"
PH = "https://arxiv.test/abs/[pending]"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {source: {required: [type], extensible: true}, "
        "entity: {required: [type], extensible: true}}\n"
        "partitioning: {namespaces: {sources: {}, entities: {}}}\n"
        "permissions: {default: {create: true, update: true, delete: false}}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-09-26")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    name = "write_server_placeholder_guard"
    spec = importlib.util.spec_from_file_location(name, WRITE)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    yield m, tmp_path
    sys.modules.pop(name, None)


def _write(root: Path, rel: str, fm: dict) -> None:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + yaml.safe_dump(fm) + "---\nbody text\n")


def test_creating_a_source_with_a_placeholder_url_is_refused(ws):
    m, _root = ws
    result = m._create("sources/p.md", {"type": "source", "id": "sources:p", "url": PH}, "body")
    assert result.startswith("rejected") and "placeholder" in result


def test_updating_a_real_url_to_a_placeholder_is_refused(ws):
    m, root = ws
    _write(root, "sources/r.md", {"type": "source", "id": "sources:r", "url": "https://x.test/1"})
    assert "placeholder" in m._update("sources/r.md", {"url": PH})


def test_a_page_already_holding_a_placeholder_stays_writable_and_repairable(ws):
    """Grandfathered: refusing its other fields would freeze 20 live pages until repaired."""
    m, root = ws
    _write(root, "sources/g.md", {"type": "source", "id": "sources:g", "url": PH})
    assert not m._update("sources/g.md", {"title": "Now titled"}).startswith("rejected")
    assert not m._update("sources/g.md", {"url": "https://x.test/real"}).startswith("rejected")


def test_the_guard_ignores_non_sources_and_url_less_sources(ws):
    m, _root = ws
    assert m._placeholder_url_refuse({"type": "entity", "url": PH}, Path("/nowhere")) is None
    assert m._placeholder_url_refuse({"type": "source"}, Path("/nowhere")) is None
    assert m._placeholder_url_refuse({"type": "source", "url": "https://x.test/1"},
                                     Path("/nowhere")) is None


def test_an_unreadable_stored_page_is_not_grandfathered(ws, monkeypatch):
    m, root = ws
    _write(root, "sources/u.md", {"type": "source", "id": "sources:u", "url": PH})
    monkeypatch.setattr(m, "_read_page", lambda _p: (_ for _ in ()).throw(OSError("gone")))
    assert m._placeholder_url_refuse({"type": "source", "url": PH},
                                     root / "wiki" / "sources" / "u.md") is not None


@pytest.mark.parametrize("url, placeholder", [
    (PH, True), ("https://x.test/{id}", True), ("https://x.test/<url>", True),
    ("https://x.test/abs/tbd", True), ("https://x.test/TODO/", True),
    ("https://x.test/p/pending?x=1", True), ("https://example.com/a", True),
    ("http://www.example.org", True),
    ("https://arxiv.org/abs/2607.14935", False), ("https://x.test/pending-review-guide", False),
    ("https://notexample.com/a", False), ("https://x.test/todos", False),
])
def test_the_placeholder_pattern(ws, url, placeholder):
    m, _root = ws
    assert bool(m._PLACEHOLDER_URL.search(url)) is placeholder
