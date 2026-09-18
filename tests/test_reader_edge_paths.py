"""Failure-path and boundary coverage for the read-only vault service."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("nh3")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-reader" / "app.py"


def _load(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("OKENGINE_TRUST", "private")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("reader_edge_app", None)
    spec = importlib.util.spec_from_file_location("reader_edge_app", APP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _request(host="127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_basename_fallback_blocks_symlink_that_resolves_outside_vault(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    namespace = wiki / "entities"
    namespace.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (namespace / "escaped.md").symlink_to(outside)
    module = _load(tmp_path, monkeypatch)

    with pytest.raises(module.HTTPException) as error:
        module.browse.resolve_page(
            "escaped", wiki=wiki, skip=lambda _name: False, excluded_dirs=lambda: set(),
            namespaces=lambda _path: set(),
            within=lambda root, path: path.resolve().is_relative_to(root.resolve()),
            split_frontmatter=module.split_fm,
        )
    assert error.value.status_code == 403


def test_tombstoned_skipped_exact_page_reaches_final_block_guard(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    page = wiki / "entities" / "blocked.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nstatus: tombstoned\n---\n", encoding="utf-8")
    module = _load(tmp_path, monkeypatch)

    with pytest.raises(module.HTTPException) as error:
        module.browse.resolve_page(
            "entities/blocked", wiki=wiki, skip=lambda name: name == "blocked.md",
            excluded_dirs=lambda: set(), namespaces=lambda _path: set(),
            within=lambda root, path: path.resolve().is_relative_to(root.resolve()),
            split_frontmatter=module.split_fm,
        )
    assert error.value.status_code == 403


def test_basic_auth_rejects_bad_credentials_and_allows_health(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    called = []

    async def downstream(scope, receive, send):
        called.append(scope["path"])

    middleware = m._BasicAuth(downstream, "user", "secret")

    async def invoke(path, authorization=None):
        sent = []
        headers = [] if authorization is None else [(b"authorization", authorization)]

        async def send(message):
            sent.append(message)

        await middleware(
            {"type": "http", "path": path, "headers": headers},
            lambda: None,
            send,
        )
        return sent

    denied = asyncio.run(invoke("/"))
    assert denied[0]["status"] == 401
    assert denied[1]["body"] == b"unauthorized"
    assert asyncio.run(invoke("/healthz")) == []
    expected = middleware._expected.encode()
    assert asyncio.run(invoke("/", expected)) == []
    assert called == ["/healthz", "/"]
    assert m.exposure_refusal("public", "0.0.0.0", "") is None
    assert m.exposure_refusal("private", "localhost", "") is None
    assert m.exposure_refusal("private", "0.0.0.0", "secret") is None
    assert "REFUSED" in m.exposure_refusal("private", "0.0.0.0", "")


def test_guard_covers_rate_and_capacity_failures(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    assert m._client_ip(SimpleNamespace(client=None)) == "?"

    monkeypatch.setattr(m, "_RATE", SimpleNamespace(allow=lambda _ip: False))
    with pytest.raises(m.HTTPException) as error:
        m._guard(_request(), SimpleNamespace(acquire=lambda **_kw: True))
    assert error.value.status_code == 429

    monkeypatch.setattr(m, "_RATE", SimpleNamespace(allow=lambda _ip: True))
    with pytest.raises(m.HTTPException) as error:
        m._guard(_request(), SimpleNamespace(acquire=lambda **_kw: False))
    assert error.value.status_code == 503

    released = []
    sem = SimpleNamespace(
        acquire=lambda **_kw: True,
        release=lambda: released.append(True),
    )
    release = m._guard(_request("10.0.0.2"), sem)
    release()
    assert released == [True]


def test_frontmatter_and_original_link_error_paths(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki" / "sources"
    wiki.mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)

    assert m.split_fm("plain") == ({}, "plain")
    assert m.split_fm("---\n- list\n---\nbody") == ({}, "body")
    assert m.split_fm("---\ninvalid: [\n---\nbody") == ({}, "body")

    missing = '<a class="wl" data-page="sources/missing">Missing</a>'
    assert m._link_originals(missing) == missing
    local = wiki / "local.md"
    local.write_text("---\nurl: ftp://example.test/a\n---\nbody\n")
    assert m._link_originals(
        '<a class="wl" data-page="sources/local">Local</a>'
    ).startswith('<a class="wl"')
    external = wiki / "external.md"
    external.write_text("---\nurl: 'https://example.test/report'\n---\n")
    linked = m._link_originals(
        '<a class="wl" data-page="sources/external">Report</a>'
    )
    assert 'class="ext"' in linked
    assert "https://example.test/report" in linked
    sibling = tmp_path / "wiki-private"
    sibling.mkdir()
    (sibling / "secret.md").write_text("---\nurl: https://secret.test/\n---\n")
    traversal = '<a class="wl" data-page="sources/../../wiki-private/secret">Secret</a>'
    assert m._link_originals(traversal) == traversal


def test_pandoc_and_download_failure_paths(tmp_path, monkeypatch):
    page = tmp_path / "wiki" / "entities" / "a" / "alpha.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: Alpha\n---\nBody\n")
    m = _load(tmp_path, monkeypatch)

    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(m.HTTPException) as error:
        m._pandoc("body", "docx")
    assert error.value.status_code == 503

    failure = subprocess.CalledProcessError(2, ["pandoc"], stderr=b"bad input")
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(m.HTTPException) as error:
        m._pandoc("body", "pdf")
    assert error.value.status_code == 500
    assert "bad input" in error.value.detail

    with pytest.raises(m.HTTPException) as error:
        m.api_download(_request(), "zip", "entities/a/alpha")
    assert error.value.status_code == 400

    monkeypatch.setattr(m, "_EXPORTS_ENABLED", False)
    with pytest.raises(m.HTTPException) as error:
        m.api_download(_request(), "pdf", "entities/a/alpha")
    assert error.value.status_code == 403

    released = []
    monkeypatch.setattr(m, "_EXPORTS_ENABLED", True)
    monkeypatch.setattr(m, "_guard", lambda *_a: lambda: released.append(True))
    monkeypatch.setattr(m, "_pandoc", lambda *_a, **_kw: b"document")
    response = m.api_download(_request(), "docx", "entities/a/alpha")
    assert response.body == b"document"
    assert released == [True]
    assert "alpha.docx" in response.headers["content-disposition"]


def test_search_short_query_errors_filtering_sorting_and_limit(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    assert m.api_search(_request(), " x ") == {"q": "x", "results": []}

    released = []
    monkeypatch.setattr(m, "_guard", lambda *_a: lambda: released.append(True))
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(m.HTTPException) as error:
        m.api_search(_request(), "needle")
    assert error.value.status_code == 503
    assert released == [True]

    released.clear()
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["rg"], 12)
        ),
    )
    assert m.api_search(_request(), "needle")["truncated"] is True
    assert released == [True]

    inside1 = wiki / "notes" / "needle-title.md"
    inside2 = wiki / "entities" / "a" / "alpha.md"
    inside1.parent.mkdir()
    inside2.parent.mkdir(parents=True)
    stdout = "\n".join(
        [
            "malformed",
            f"/outside/page.md:1:needle",
            f"{inside2}:2:some needle text",
            f"{inside2}:3:duplicate needle",
            f"{inside1}:4:title needle",
        ]
    )
    monkeypatch.setattr(m.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(stdout=stdout))
    result = m.api_search(_request(), "needle", limit=0)
    assert result["total"] == 2
    assert len(result["results"]) == 1
    assert result["results"][0]["path"] == "notes/needle-title"


def test_backlinks_limit_and_health(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(
        m,
        "_load_backlinks",
        lambda blocking=False: {"entities/a/alpha": [{"path": str(i)} for i in range(3)]},
    )
    result = m.api_backlinks("entities/a/alpha.md", limit=0)
    assert result["path"] == "entities/a/alpha"
    assert result["count"] == 3
    assert len(result["backlinks"]) == 1
    assert m.healthz()["vault_present"] is True


def test_chat_validation_sanitization_and_relay(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)

    class JsonRequest:
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, value=None, error=None):
            self.value, self.error = value, error

        async def json(self):
            if self.error:
                raise self.error
            return self.value

    with pytest.raises(m.HTTPException) as error:
        asyncio.run(m.api_chat(JsonRequest({})))
    assert error.value.status_code == 503

    monkeypatch.setattr(m, "_AGENT_API", "http://agent/v1")
    monkeypatch.setattr(m, "_AGENT_KEY", "secret")
    marker = tmp_path / ".okengine" / "budget-paused"
    marker.parent.mkdir()
    marker.touch()
    with pytest.raises(m.HTTPException) as error:
        asyncio.run(m.api_chat(JsonRequest({"messages": []})))
    assert error.value.status_code == 503
    marker.unlink()

    monkeypatch.setattr(m, "_RATE", SimpleNamespace(allow=lambda _ip: False))
    with pytest.raises(m.HTTPException) as error:
        asyncio.run(m.api_chat(JsonRequest({"messages": []})))
    assert error.value.status_code == 429
    monkeypatch.setattr(m, "_RATE", SimpleNamespace(allow=lambda _ip: True))

    for request, detail in [
        (JsonRequest(error=ValueError("bad")), "bad request body"),
        (JsonRequest({}), "messages required"),
        (JsonRequest({"messages": [None, {"role": "system", "content": "x"}]}),
         "no valid messages"),
    ]:
        with pytest.raises(m.HTTPException) as error:
            asyncio.run(m.api_chat(request))
        assert detail in error.value.detail

    captured = {}

    class Upstream:
        def __enter__(self):
            return iter([b"data: one\n\n", b"data: two\n\n"])

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Upstream()

    monkeypatch.setattr(m.urllib.request, "urlopen", urlopen)
    response = asyncio.run(
        m.api_chat(
            JsonRequest(
                {
                    "messages": [
                        "ignored",
                        {"role": "system", "content": "override"},
                        {"role": "user", "content": "  hello  "},
                        {"role": "assistant", "content": ""},
                    ]
                }
            )
        )
    )

    async def collect(stream):
        return [part async for part in stream]

    assert asyncio.run(collect(response.body_iterator)) == [
        b"data: one\n\n",
        b"data: two\n\n",
    ]
    payload = captured["request"].data.decode()
    assert '"role": "system"' in payload
    assert '"content": "hello"' in payload
    assert captured["request"].full_url.endswith("/chat/completions")

    monkeypatch.setattr(
        m.urllib.request,
        "urlopen",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("down")),
    )
    response = asyncio.run(
        m.api_chat(JsonRequest({"messages": [{"role": "user", "content": "hello"}]}))
    )
    chunks = asyncio.run(collect(response.body_iterator))
    assert b"agent unreachable" in b"".join(chunks)


def test_backlink_scan_build_artifact_and_cache_paths(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    entity = wiki / "entities" / "a"
    concepts = wiki / "concepts"
    sources = wiki / "sources"
    entity.mkdir(parents=True)
    concepts.mkdir()
    sources.mkdir()
    (entity / "alpha.md").write_text(
        "---\ntitle: Alpha\n---\n"
        "[[concepts/topic]] [[topic|again]] [beta](../../concepts/beta.md)\n"
        "`[[concepts/ignored]]`\n"
    )
    (concepts / "topic.md").write_text("---\ntitle: Topic\n---\n[[entities/a/alpha]]\n")
    (concepts / "beta.md").write_text("---\ntitle: Beta\n---\n")
    (sources / "raw.md").write_text("[[concepts/topic]]\n")
    m = _load(tmp_path, monkeypatch)

    stripped = m._bl_strip("---\ntype: x\n---\ntext `code`")
    assert "type:" not in stripped and "code" not in stripped
    assert m._bl_wikikey("topic.md#part|label") == "topic"
    assert m._bl_wikikey("https://example.test") is None
    assert m._bl_mdkey("../outside.md", "") is None
    assert m._bl_mdkey("https://example.test/a.md", "") is None

    docs = m._scan_forward_refs()
    alpha = next(row for row in docs if row["key"] == "entities/a/alpha")
    assert {row["key"] for row in alpha["references"]} >= {
        "concepts/topic",
        "concepts/beta",
    }
    assert all(row["key"] != "sources/raw" for row in docs)
    built = m._build_backlinks()
    assert built["concepts/topic"][0]["title"] == "Alpha"

    artifact = wiki / ".backlinks.json"
    artifact.write_text('{"backlinks":{"concepts/topic":[{"key":"entities/a/alpha"}]}}')
    now = time.time()
    monkeypatch.setattr(m.time, "time", lambda: now)
    assert m._artifact_backlinks()["concepts/topic"]
    assert m._artifact_backlinks()["concepts/topic"]
    artifact.write_text("{bad")
    # Force an observably newer artifact even on coarse-resolution CI filesystems;
    # a same-tick rewrite legitimately retains the mtime-keyed cache entry.
    os.utime(artifact, (now + 2, now + 2))
    assert m._artifact_backlinks() is None
    artifact.write_text('{"wrong":[]}')
    # A rewrite can land on the original cached mtime (especially on fast/coarse CI filesystems),
    # in which case returning the cached map is correct. Make this malformed shape observably newer.
    os.utime(artifact, (now + 4, now + 4))
    assert m._artifact_backlinks() is None

    artifact.unlink()
    monkeypatch.setattr(m, "_build_backlinks", lambda: {"x": [{"key": "y"}]})
    m._BACKLINKS.update(map=None, ts=0.0)
    assert m._load_backlinks(blocking=True)["x"]
    assert m._load_backlinks(blocking=True)["x"]

    m._BACKLINKS.update(map=None, ts=0.0)
    refreshed = []
    monkeypatch.setattr(m, "_refresh_backlinks_async", lambda: refreshed.append(True))
    assert m._load_backlinks(blocking=False) == {}
    assert refreshed == [True]


def test_reader_metadata_cache_and_derived_namespace_edges(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    dashboards = wiki / "briefings"
    mixed = wiki / "mixed"
    dashboards.mkdir(parents=True)
    mixed.mkdir()
    dashboard = dashboards / "daily.md"
    dashboard.write_text("---\ntype: dashboard\ntitle: Daily\n---\nbody")
    entity = mixed / "entity.md"
    entity.write_text("---\ntype: entity\n---\n# Entity title\n")
    typeless = mixed / "typeless.md"
    typeless.write_text("body")
    m = _load(tmp_path, monkeypatch)

    assert m._dir_is_derived([dashboard]) is True
    assert m._dir_is_derived([dashboard, entity]) is False
    assert m._dir_is_derived([typeless]) is False
    assert m._read_head(tmp_path / "missing.md") == ""
    meta = m._page_meta(entity.resolve())
    assert meta["title"] == "Entity title"
    assert meta["revision"]
    assert m._disp_ts("") == ""
    assert m._disp_ts("2026-07-29T01:02:03Z") == "2026-07-29 01:02:03"
    assert m._disp_ts("2026-07-29") == "2026-07-29"
    assert m._disp_ts("custom") == "custom"

    m._DIR_CACHE.clear()
    first = m._scan_dir("mixed")
    assert len(first) == 2
    assert m._scan_dir("mixed") is first
    assert m._scan_dir("missing") == []
    assert m._pages_of_types(frozenset({"entity"}))[0]["title"] == "Entity title"

    monkeypatch.setattr(m, "_top_dirs", lambda: (_ for _ in ()).throw(OSError("scan")))
    m._warm_cache()  # transient filesystem errors are deliberately swallowed


def test_link_title_wikilink_and_embed_remaining_branches(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    assert m._link_title("") is None
    assert m._link_title("https://example.test") is None
    assert m._resolve_basename("missing.md") is None

    page = wiki / "entities/a/page.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nname: Friendly\n---\nbody\n")
    assert m._link_title("entities/a/page") == "Friendly"
    assert m._link_title("entities/a/page") == "Friendly"  # cache hit

    alias = m._WIKILINK.search("[[entities/a/page|Alias]]")
    anchor = m._WIKILINK.search("[[#heading]]")
    assert m._wl_display(alias) == "Alias"
    assert m._wl_display(anchor) == "heading"
    assert m._linkify("[[#heading]]") == "heading"

    assert m._resolve_embeds("![[anything]]", depth=4) == "![[anything]]"
    assert "embedded asset" in m._resolve_embeds("![[image.png]]")
    assert "missing embed" in m._resolve_embeds("![[absent]]")
    monkeypatch.setattr(m, "_within", lambda *_a: False)
    assert "blocked embed" in m._resolve_embeds("![[entities/a/page]]")
    monkeypatch.setattr(m, "_within", lambda *_a: True)
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda path, *a, **k: (_ for _ in ()).throw(OSError())
                        if path == page else original(path, *a, **k))
    assert "unreadable embed" in m._resolve_embeds("![[entities/a/page]]")


def test_reader_schema_display_and_backlink_drop_config_branches(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    schema = tmp_path / "schema.yaml"
    schema.write_text(
        "exclude: [wiki/operational/, dashboards/, '', wiki/archive/deep]\n"
        "backlink_drop: [wiki/sources/, concepts/deep, '']\n"
        "display_groups:\n"
        "  Actors: [actor, '']\n"
        "  '': [ignored]\n"
        "  Empty: []\n"
        "rail_top_section: {label: Highlights, namespaces: [briefings, '']}\n")
    m = _load(tmp_path, monkeypatch)
    m._EXCLUDE_CACHE = (float("-inf"), frozenset())
    m._BL_DROP_CACHE = (0.0, None)
    m._GROUPS_CACHE = (float("-inf"), [])
    assert m._excluded_dirs() == frozenset({"operational"})
    assert m._excluded_dirs() == frozenset({"operational"})
    assert m._backlink_drop_dirs() == frozenset({"sources", "concepts"})
    assert m._backlink_drop_dirs() == frozenset({"sources", "concepts"})
    assert m._display_groups() == [("Actors", frozenset({"actor"}))]
    assert m._display_groups() == [("Actors", frozenset({"actor"}))]
    assert m._rail_top_section() == ("Highlights", ("briefings",))

    schema.write_text("[broken\n")
    m._EXCLUDE_CACHE = (float("-inf"), frozenset())
    m._BL_DROP_CACHE = (0.0, None)
    m._GROUPS_CACHE = (float("-inf"), [])
    assert m._excluded_dirs() == frozenset()
    assert m._backlink_drop_dirs() == frozenset({"sources"})
    assert m._display_groups() == []


def test_recent_reporting_input_and_story_identity_branches(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki/sources"
    wiki.mkdir(parents=True)
    (wiki / "cluster.md").write_text(
        "---\ntitle: Cluster\nstory_id: shared\npublisher: One\n---\n")
    (wiki / "url.md").write_text(
        "---\ntitle: URL\nurl: https://www.example.test/story/?query=1\n---\n")
    (wiki / "path.md").write_text("---\ntitle: Path\n---\n")
    m = _load(tmp_path, monkeypatch)
    assert m._recent_reporting({"recent_news_refs": 3}) == []
    assert m._recent_reporting({"recent_news_refs": "not-a-source"}) == []
    rows = m._recent_reporting({"recent_news_refs": [
        "[[sources/cluster.md]]", "sources/url", "sources/path", "sources/missing",
    ]})
    assert len(rows) == 3
    assert all(row["count"] == 1 for row in rows)


def test_backlink_filter_build_and_refresh_lock_branches(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_excluded_dirs", lambda: frozenset({"operational"}))
    monkeypatch.setattr(m, "_backlink_drop_dirs", lambda: frozenset({"sources"}))
    assert m._skip_backlink_src("HOT")
    assert m._skip_backlink_src("nested/_archive/page")
    assert m._skip_backlink_src("sub/sources/page")
    assert not m._skip_backlink_src("entities/a/page")

    monkeypatch.setattr(m, "_scan_forward_refs", lambda: [
        {"key": "", "references": []},
        {"key": "entities/a/src", "references": [
            {"key": ""}, {"key": "entities/a/src"},
            {"key": "sources/raw"}, {"key": "concepts/target"},
            {"key": "concepts/target"},
        ]},
    ])
    monkeypatch.setattr(m, "_backlink_title", lambda _src: "Source")
    built = m._build_backlinks()
    assert built["concepts/target"] == [
        {"key": "entities/a/src", "title": "Source"}]

    class Locked:
        def acquire(self, blocking=False):
            return False
    monkeypatch.setattr(m, "_BL_LOCK", Locked())
    assert m._refresh_backlinks_async() is None
