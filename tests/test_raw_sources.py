"""_converge_raw_source: the ONE identity both raw writers use (okengine#862).

raw-backfill's tool and the raw-compile lane must derive byte-identical ids and seats for the same
URL, or each re-creates the other's page (the okengine#54 loop). These tests pin the exact values,
not their prefixes. `_converge` (the storage step, tested in its own suites) is replaced with a
recorder so the identity it is handed can be read back precisely.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "okengine-mcp" / "write_server.py"
URL = "https://example.test/paper"
H = hashlib.sha256(URL.encode("utf-8")).hexdigest()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    name = "write_server_raw_sources"
    spec = importlib.util.spec_from_file_location(name, WRITE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    calls: list[tuple] = []

    def record(*args):
        calls.append(args)
        return "created"

    monkeypatch.setattr(module, "_converge", record)
    yield module, calls, tmp_path
    sys.modules.pop(name, None)


def test_identity_and_seat_are_exact(ws):
    m, calls, _ = ws
    fm = {"type": "source", "url": URL, "raw": "raw/somewhere/else.md"}
    assert m._converge_raw_source("raw/ai/x.md", fm, "sources/2026/09/20/paper.md",
                                  "Body text.", "pack-a", "sha256:prev") == "created"
    (path, fm_yaml, body, pack, remove, expected), = calls
    assert path == f"sources/2026/09/20/paper-{H[:8]}.md"
    written = yaml.safe_load(fm_yaml)
    assert written["id"] == f"sources:url-{H[:20]}"
    assert written["raw"] == "raw/ai/x.md", "the selected capture, never a caller-supplied one"
    assert written["url"] == URL
    assert (body, pack, remove, expected) == ("Body text.", "pack-a", "", "sha256:prev")
    assert list(written)[:3] == ["type", "url", "raw"], "caller field order is preserved"


def test_a_suffixless_request_gets_md(ws):
    m, calls, _ = ws
    m._converge_raw_source("raw/ai/x.md", {"url": URL}, "sources/paper")
    assert calls[0][0] == f"sources/paper-{H[:8]}.md"


def test_the_same_url_yields_the_same_identity_from_any_capture(ws):
    m, calls, _ = ws
    m._converge_raw_source("raw/ai/a.md", {"url": URL}, "sources/2026/09/20/a.md")
    m._converge_raw_source("raw/connectors/n/records/b.json", {"url": URL},
                           "sources/2026/09/20/b.md")
    ids = {yaml.safe_load(call[1])["id"] for call in calls}
    assert ids == {f"sources:url-{H[:20]}"}


def test_a_missing_url_is_recovered_from_the_capture(ws):
    m, calls, root = ws
    raw = root / "raw" / "connectors" / "n" / "records" / "a.json"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"payload": {"source_url": "%s"}}' % URL)
    m._converge_raw_source("raw/connectors/n/records/a.json", {"type": "source"},
                           "sources/a.md")
    written = yaml.safe_load(calls[0][1])
    assert written["url"] == URL and written["id"] == f"sources:url-{H[:20]}"


def test_no_url_anywhere_is_rejected_before_any_write(ws):
    """NEGATIVE: without a URL the page has no identity — nothing reaches storage."""
    m, calls, root = ws
    (root / "raw").mkdir()
    (root / "raw" / "x.md").write_text("no url here\n")
    assert m._converge_raw_source("raw/x.md", {"url": "  "}, "sources/x.md") == \
        "rejected: source url is required"
    assert calls == []


def test_non_ascii_frontmatter_is_stored_readably(ws):
    """Titles and publishers are read by people in the page file: `Zürich` must be stored as
    `Zürich`, not a `\\xFC` escape that only round-trips through a YAML parser."""
    m, calls, _ = ws
    m._converge_raw_source("raw/ai/x.md", {"url": URL, "title": "Zürich lab — résumé"},
                           "sources/x.md")
    assert "title: Zürich lab — résumé" in calls[0][1]


class _Registry:
    def __init__(self, path=None, tombstoned=False):
        self.path, self.tombstoned = path, tombstoned

    def resolve(self, _pid):
        return self.path

    def is_tombstoned(self, _pid):
        return self.tombstoned


def _backlinks(m, monkeypatch, root, existing_raw, **reg):
    page = root / "wiki" / "sources" / "p.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    if existing_raw is not None:
        page.write_text("---\n" + yaml.safe_dump({"type": "source", "raw": existing_raw})
                        + "---\nbody\n")
    monkeypatch.setattr(m, "_registry", lambda: _Registry(**({"path": "sources/p.md"} | reg)))
    return m._raw_backlinks_with("sources:url-x", "raw/new.md")


def test_a_second_capture_is_appended_not_replaced(ws, monkeypatch):
    """REGRESSION (okengine#886): converging a second capture of a compiled URL replaced
    `raw:`, un-marking the first capture so the selector re-queued it forever."""
    m, _calls, root = ws
    assert _backlinks(m, monkeypatch, root, "raw/old.md") == ["raw/old.md", "raw/new.md"]


def test_an_already_listed_capture_is_not_duplicated(ws, monkeypatch):
    m, _calls, root = ws
    assert _backlinks(m, monkeypatch, root, ["raw/old.md", "raw/new.md"]) == \
        ["raw/old.md", "raw/new.md"]


def test_a_new_page_keeps_the_scalar_backlink(ws, monkeypatch):
    m, _calls, root = ws
    assert _backlinks(m, monkeypatch, root, None, path=None) == "raw/new.md"
    assert _backlinks(m, monkeypatch, root, [], path="sources/p.md") == "raw/new.md"


def test_a_tombstoned_or_unreadable_page_contributes_nothing(ws, monkeypatch):
    """NEGATIVE: a retired id is not a live seat (converge then refuses it anyway), and a page
    that cannot be read never lends backlinks."""
    m, _calls, root = ws
    assert _backlinks(m, monkeypatch, root, "raw/old.md", tombstoned=True) == "raw/new.md"
    monkeypatch.setattr(m, "_registry", lambda: _Registry(path="sources/p.md"))
    monkeypatch.setattr(m, "_read_page", lambda _p: (_ for _ in ()).throw(OSError("gone")))
    assert m._raw_backlinks_with("sources:url-x", "raw/new.md") == "raw/new.md"


def test_two_captures_of_one_url_keep_both_backlinks_end_to_end(tmp_path, monkeypatch):
    """No stubs on the write path: the second converge lands on the first page and both
    captures stay recorded."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "types: {source: {required: [type, published], extensible: true}}\n"
        "permissions: {default: {create: true, update: true, delete: false}}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-09-26")  # after the fixture's `published`
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    for rel in ("raw/a.md", "raw/b.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"url: {URL}\n")
    name = "write_server_raw_e2e"
    spec = importlib.util.spec_from_file_location(name, WRITE)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    try:
        spec.loader.exec_module(m)
        fm = {"type": "source", "url": URL, "title": "Paper", "published": "2026-09-01"}
        first = m._converge_raw_source("raw/a.md", dict(fm), "sources/paper.md", "x" * 90)
        second = m._converge_raw_source("raw/b.md", dict(fm), "sources/paper.md", "x" * 90)
        assert not first.startswith(("rejected", "refused")), first
        assert not second.startswith(("rejected", "refused")), second
        pages = list((tmp_path / "wiki" / "sources").rglob("*.md"))
        assert len(pages) == 1
        written = yaml.safe_load(pages[0].read_text().split("---")[1])
        assert written["raw"] == ["raw/a.md", "raw/b.md"]
    finally:
        sys.modules.pop(name, None)


def test_a_pack_lane_appends_a_capture_to_an_engine_owned_source_end_to_end(tmp_path, monkeypatch):
    """REGRESSION (okengine#886): the live `sources` type is owned by `engine` and lanes write as
    the deployment's pack. The merge treated the pack's longer `raw` as a conflict and kept the
    old value, so one live page was converged 39 times while its second capture stayed queued.
    The fixture above has no owners and never exercised this."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "types: {source: {required: [type, published], extensible: true}}\n"
        "owners: {types: {source: engine}}\n"
        "permissions: {default: {create: true, update: true, delete: false}}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_PACK", "okpack-test")
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-09-26")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    for rel in ("raw/a.json", "raw/b.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"url: {URL}\n")
    name = "write_server_raw_owned_e2e"
    spec = importlib.util.spec_from_file_location(name, WRITE)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    try:
        spec.loader.exec_module(m)
        fm = {"type": "source", "url": URL, "title": "Paper", "published": "2026-09-01"}
        for raw in ("raw/a.json", "raw/b.json", "raw/b.json"):
            result = m._converge_raw_source(raw, dict(fm), "sources/paper.md", "x" * 90)
            assert not result.startswith(("rejected", "refused")), result
        (page,) = list((tmp_path / "wiki" / "sources").rglob("*.md"))
        written = yaml.safe_load(page.read_text().split("---")[1])
        assert written["raw"] == ["raw/a.json", "raw/b.json"], "second capture recorded once"
    finally:
        sys.modules.pop(name, None)
