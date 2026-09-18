"""Focused contracts for reader lifecycle, cache, and defensive branches."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("nh3")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-reader" / "app.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    sys.path.insert(0, str(APP.parent))
    name = f"reader_contract_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(name, APP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_lifecycle_warmers_and_path_guards(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(m, "_start_warmer", lambda: calls.append("warm"))
    monkeypatch.setattr(m, "_prewarm_backlinks", lambda: calls.append("links"))

    async def enter():
        async with m._lifespan(None):
            calls.append("body")
    asyncio.run(enter())
    assert calls == ["warm", "links", "body"]
    assert not m._within(wiki, tmp_path.parent / "outside")

    m.WIKI = tmp_path / "absent"
    assert m._resolve_basename("x.md") is None
    assert m._top_dirs() == []


def test_file_races_and_malformed_metadata(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    page = wiki / "page.md"
    page.write_text("---\ntitle: Page\n---\n")
    source = wiki / "sources" / "one.md"
    source.parent.mkdir()
    source.write_text("---\nurl: https://example.test\n---\n")
    (tmp_path / "schema.yaml").write_text("[broken")
    m = _load(tmp_path, monkeypatch)

    original_head = m._read_head
    monkeypatch.setattr(m, "_read_head", lambda _p: (_ for _ in ()).throw(OSError()))
    assert m._link_title("page") is None
    monkeypatch.setattr(m, "_read_head", original_head)
    assert m._rail_top_section() == ("", ())

    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == source else original_read(p, *a, **k))
    html = '<a class="wl" data-page="sources/one">One</a>'
    assert m._link_originals(html) == html

    original_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == page else original_stat(p, *a, **k))
    assert m._page_meta(page)["revision"] == ""


def test_background_worker_idempotence_and_threads(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "a").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    scanned = []
    monkeypatch.setattr(m, "_top_dirs", lambda: ["a"])
    monkeypatch.setattr(m, "_scan_dir", lambda sub, force=False: scanned.append((sub, force)))
    m._warm_cache()
    assert scanned == [("a", True)]

    ticks = []
    monkeypatch.setattr(m, "_warm_cache", lambda: ticks.append(True))
    monkeypatch.setattr(m.time, "sleep", lambda _n: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        m._warm_loop()
    assert ticks == [True]

    started = []
    class Thread:
        def __init__(self, **kw): started.append(kw)
        def start(self): started.append("started")
    monkeypatch.setattr(m.threading, "Thread", Thread)
    if hasattr(m._start_warmer, "_started"):
        del m._start_warmer._started
    m._start_warmer()
    m._start_warmer()
    m._prewarm_backlinks()
    assert len([x for x in started if x == "started"]) == 2


def test_about_budget_chat_and_tree_edges(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError())
                        if p.name in {"pack.yaml", "CLAUDE.md"} else original_read(p, *a, **k))
    monkeypatch.setattr(Path, "iterdir", lambda _p: (_ for _ in ()).throw(OSError()))
    info = m._about_info()
    assert info["installed_domains"] == [] and info["sub_domains"] == []

    monkeypatch.setattr(Path, "exists", lambda _p: (_ for _ in ()).throw(OSError()))
    assert m._budget_tripped() is False

    class Request:
        client = SimpleNamespace(host="x")
        async def json(self):
            return {"messages": [{"role": "user", "content": "hi"}]}
    monkeypatch.setattr(m, "_AGENT_API", "http://agent")
    monkeypatch.setattr(m, "_AGENT_KEY", "key")
    err = urllib.error.HTTPError("http://agent", 502, "bad", {}, None)
    monkeypatch.setattr(m.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(err))
    response = asyncio.run(m.api_chat(Request()))
    async def collect(): return b"".join([x async for x in response.body_iterator])
    assert b"agent error 502" in asyncio.run(collect())

    m.WIKI = tmp_path / "missing"
    assert m.api_tree()["dirs"] == []


def test_routes_caches_and_resolution_errors(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    for bad in ("a/b", "..", ".hidden", "/root"):
        with pytest.raises(m.HTTPException) as exc:
            m.api_pages(dir=bad, group="")
        assert exc.value.status_code == 400
    with pytest.raises(m.HTTPException) as exc:
        m._resolve_page("../bad")
    assert exc.value.status_code == 400
    with pytest.raises(m.HTTPException) as exc:
        m._resolve_page("missing")
    assert exc.value.status_code == 404

    m._REVISION_CACHE = (time.monotonic(), [{"path": "cached"}])
    assert m.api_page_revisions()["pages"][0]["path"] == "cached"
    assert m._ns_about("") == ""
    assert m._ns_about("missing") == ""
    about = wiki / "docs" / "_about.md"
    about.parent.mkdir()
    about.write_text("About")
    monkeypatch.setattr(Path, "read_text", lambda _p, *a, **k: (_ for _ in ()).throw(OSError()))
    assert m._ns_about("docs") == ""


def test_metadata_assessment_and_reference_defenses(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    schema = tmp_path / "schema.yaml"
    schema.write_text("source_registry:\n  alpha: {reliability: A}\n  blank: {}\n")
    m = _load(tmp_path, monkeypatch)
    assert m._source_reliability() == {"alpha": "A"}
    assert m._source_reliability() == {"alpha": "A"}
    assert m._shape_conflicts({"conflicts": [1, {"values": [{"value": "x", "sources": 1}]}]})[0]["values"][0]["sources"] == []
    m._OBS_INDEX_CACHE = (time.monotonic(), {"x": []})
    assert m._observations_by_canonical() == {"x": []}
    assert m._url_label(SimpleNamespace())
    assert m._ref_target(2) is None
    m.WIKI = tmp_path / "absent"
    assert m._ref_target("entities/x") is None
    assert m._meta_panel_items([]) == {"primary": [], "secondary": []}
    assert m._panel_for({"panel": {"kind": "two-axis"}}, "<!-- panel-svg") is None
    assert m._entity_assessments({}, "entities/x") == []


def test_backlink_refresh_scan_and_shell_error_paths(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    bad = wiki / "bad.md"
    bad.write_text("[[x]]")
    m = _load(tmp_path, monkeypatch)
    assert m._backlink_title("missing") == "missing"

    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == bad else original_read(p, *a, **k))
    assert m._scan_forward_refs() == []

    events = []
    class Lock:
        def acquire(self, blocking=False): events.append("acquire"); return True
        def release(self): events.append("release")
    class Thread:
        def __init__(self, **kw): events.append(kw)
        def start(self): events.append("start")
    monkeypatch.setattr(m, "_BL_LOCK", Lock())
    monkeypatch.setattr(m.threading, "Thread", Thread)
    m._BACKLINKS.update(map=None, ts=0)
    m._refresh_backlinks_async()
    assert events[-1] == "start"
    events.clear()
    m._BACKLINKS.update(map={}, ts=time.monotonic())
    m._refresh_backlinks_async()
    assert "start" not in events

    assert m.favicon().media_type == "image/svg+xml"
    original_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda _p: (_ for _ in ()).throw(OSError()))
    assert isinstance(m.index(), str)
    monkeypatch.setattr(Path, "read_bytes", original_bytes)


def test_backlink_source_filter_unions_schema_and_reader_drop_sets(tmp_path, monkeypatch):
    """Both independently configured drop sets apply, including their overlap. This distinguishes
    union from subtraction, intersection, and symmetric difference in the reader boundary."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)

    def skip(key, *, excluded, surfaced, namespace):
        return m.backlinks.skip_source(
            key,
            skip=lambda _name: False,
            reserved_names=frozenset(),
            is_reserved_segment=lambda _segment: False,
            excluded_dirs=lambda: frozenset(excluded),
            surfaced_derived=frozenset(surfaced),
            backlink_drop_dirs=lambda: frozenset(),
            namespaces=lambda _source: frozenset({namespace}),
        )

    assert skip("observations/item", excluded={"observations"}, surfaced={"dashboards"},
                namespace="observations") is True
    assert skip("dashboards/item", excluded={"observations"}, surfaced={"dashboards"},
                namespace="dashboards") is True
    assert skip("dashboards/item", excluded={"dashboards"}, surfaced={"dashboards"},
                namespace="dashboards") is True
    assert skip("entities/item", excluded={"observations"}, surfaced={"dashboards"},
                namespace="entities") is False


def test_search_dependencies_remain_keyword_only(tmp_path, monkeypatch):
    """Reject positional dependency injection at the reader service boundary."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)

    with pytest.raises(TypeError):
        m.search.search(
            None, "query", 10, tmp_path / "wiki", lambda *_args: None, object(),
            lambda: frozenset(), lambda _path: frozenset(), SimpleNamespace(),
        )


def test_remaining_reader_value_and_error_contracts(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    assert m._ns_dirs(tmp_path / "outside.md") == frozenset()
    assert m._meta_compact_dict({"a": 1, "b": ""}) == "a=1"

    pack = tmp_path / "pack.yaml"
    claude = tmp_path / "CLAUDE.md"
    pack.write_text("name: x")
    claude.write_text("text")
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError())
                        if p in {pack, claude} else original_read(p, *a, **k))
    info = m._about_info()
    assert info["vault"] == "" and info["installed_domains"] == []

    dashboard = wiki / "dash.md"
    dashboard.write_text("---\ntype: dashboard\n---\n")
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == dashboard else original_read(p, *a, **k))
    assert m._dir_is_derived([dashboard]) is False
    assert m._backlink_title("dash") == "dash"


def test_remaining_routes_revisions_and_config_errors(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    docs = wiki / "docs"
    docs.mkdir(parents=True)
    page = docs / "one.md"
    page.write_text("# One")
    about = docs / "_about.md"
    about.write_text("# About")
    schema = tmp_path / "schema.yaml"
    schema.write_text("source_registry: [bad]")
    m = _load(tmp_path, monkeypatch)
    assert m.api_pages(dir="docs", group="")["pages"]
    assert "About" in m._ns_about("docs")
    assert m._source_reliability() == {}

    m._REVISION_CACHE = (float("-inf"), [])
    original_stat = Path.stat
    resolved_page = page.resolve()
    monkeypatch.setattr(Path, "stat", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == resolved_page else original_stat(p, *a, **k))
    assert all(row["path"] != "docs/one" for row in m.api_page_revisions()["pages"])

    m._SRC_REL_CACHE = (float("-inf"), {})
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == schema else Path.open(p).read())
    assert m._source_reliability() == {}


def test_reference_assessment_reporting_and_search_limits(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    target = wiki / "entities" / "a" / "alpha.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Alpha")
    assessment = wiki / "assessments" / "bad.md"
    assessment.parent.mkdir()
    assessment.write_text("bad")
    source = wiki / "sources" / "bad-url.md"
    source.parent.mkdir()
    source.write_text("---\ntitle: Bad URL\nurl: 'http://['\n---\n")
    m = _load(tmp_path, monkeypatch)
    assert m._ref_target("entities/alpha") == "entities/a/alpha"

    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == assessment else original_read(p, *a, **k))
    assert m._entity_assessments({}, "entities/x") == []
    assert m._recent_reporting({"recent_news_refs": ["sources/bad-url"]})[0]["count"] == 1

    monkeypatch.setattr(m, "_guard", lambda *_a: lambda: None)
    lines = "\n".join(f"{target}:{i}:needle" for i in range(1500))
    # Unique paths are needed to exercise the hard 1500-row safety cap.
    lines = "\n".join(f"{wiki}/docs/p{i}.md:1:needle" for i in range(1500))
    monkeypatch.setattr(m.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=lines))
    assert m.api_search(SimpleNamespace(client=None), "needle", 100)["total"] == 1500


def test_remaining_conditional_branches(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)

    plain = wiki / "sources" / "plain.md"
    plain.parent.mkdir()
    plain.write_text("body")
    html = '<a class="wl" data-page="sources/plain">Plain</a>'
    assert m._link_originals(html) == html

    schema = tmp_path / "schema.yaml"
    schema.write_text("display_groups: scalar\nrail_top_section: scalar\n")
    m._GROUPS_CACHE = (float("-inf"), [])
    m._RAILTOP_CACHE = (float("-inf"), ("", ()))
    assert m._display_groups() == []
    assert m._rail_top_section() == ("", ())
    schema.unlink()
    m._GROUPS_CACHE = (float("-inf"), [])
    assert m._display_groups() == []

    empty = wiki / "empty"
    empty.mkdir()
    (empty / "INDEX.md").write_text("ignored")
    full = wiki / "full"
    full.mkdir()
    (full / "page.md").write_text("page")
    assert "empty" not in [row["dir"] for row in m.api_tree()["dirs"]]

    assert m._shape_conflicts({"conflicts": [{"values": [{"value": 1, "sources": ["x"]}]}]})
    obs = wiki / "observations" / "x"
    obs.mkdir(parents=True)
    (obs / "blank.md").write_text("---\nsource: x\n---\n")
    (obs / "real.md").write_text("---\ncanonical: real\n---\n")
    m._OBS_INDEX_CACHE = (float("-inf"), {})
    assert "real" in m._observations_by_canonical()

    m._RPANELS_CACHE[:] = [time.time(), {"x": {}}]
    assert m._reader_panels() == {"x": {}}
    assert m._clean_markdown("body", title="") == "body\n"
    assert m._skip_backlink_src("HOT.md")

    assert m._bl_strip("plain") == "plain"
    (wiki / "links.md").write_text("[[https://example.test]] [[valid]]")
    assert m._scan_forward_refs()

    # A fresh stale-map check takes the no-rebuild path inside the single-flight lock.
    monkeypatch.setattr(m, "_artifact_backlinks", lambda: None)
    m._BACKLINKS.update(map={"old": []}, ts=time.monotonic())
    assert m._load_backlinks(blocking=True) == {"old": []}
    # Preserve a stale non-empty map when a transient rebuild yields no graph.
    # stale RELATIVE to monotonic(), not an absolute 0 -- see the cockpit twin. (The `ts=150.0`
    # case below is fine as an absolute: it mocks time.monotonic outright.)
    m._BACKLINKS.update(map={"old": []}, ts=time.monotonic() - m._BACKLINKS_TTL - 1)
    monkeypatch.setattr(m, "_build_backlinks", lambda: {})
    assert m._load_backlinks(blocking=True) == {"old": []}

    original_head = m._read_head
    monkeypatch.setattr(m, "_read_head", lambda _p: (_ for _ in ()).throw(OSError()))
    assert m._backlink_title("anything") == "anything"
    monkeypatch.setattr(m, "_read_head", original_head)

    original_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m._ref_target("entities/missing") is None
    monkeypatch.setattr(Path, "rglob", original_rglob)

    # The map can become fresh between the optimistic check and acquiring the lock.
    moments = iter([100000.0, 100.0])
    monkeypatch.setattr(m.time, "monotonic", lambda: next(moments))
    m._BACKLINKS.update(map={"raced": []}, ts=150.0)
    assert m._load_backlinks(blocking=True) == {"raced": []}

    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda p: False if p.name == "style.css" else original_is_file(p))
    assert isinstance(m.index(), str)
