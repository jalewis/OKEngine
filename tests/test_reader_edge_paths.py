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
            tombstoned=module._tombstoned,
        )
    assert error.value.status_code == 403


def test_tombstoned_exact_page_is_not_found_rather_than_served(tmp_path, monkeypatch):
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
            tombstoned=module._tombstoned,
        )
    assert error.value.status_code == 404


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
    # okengine#1017: each rewrite lands in the same timestamp tick as the cached read (mtime
    # pinned), as it does on a fast disk; the cache must still notice it.
    pinned = artifact.stat().st_mtime_ns
    artifact.write_text("{bad")
    os.utime(artifact, ns=(pinned, pinned))
    assert m._artifact_backlinks() is None
    artifact.write_text('{"wrong":[]}')
    os.utime(artifact, ns=(pinned, pinned))
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


def test_backlinks_endpoint_never_waits_for_the_graph_build(tmp_path, monkeypatch):
    """The graph is prewarmed in the background at startup so a request never blocks on the
    build. While it is still building, the endpoint answers with what is ready (nothing),
    instead of waiting for the build to finish."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    built = {"entities/a/alpha": [{"path": "sources/one"}]}

    def load_backlinks(blocking=False):
        return built if blocking else {}   # a waiting caller would get the finished graph

    monkeypatch.setattr(m, "_load_backlinks", load_backlinks)
    result = m.api_backlinks("entities/a/alpha")
    assert result == {"path": "entities/a/alpha", "count": 0, "backlinks": []}


def _directory_index():
    sys.path.insert(0, str(APP.parent))
    import directory_index
    return directory_index


@pytest.mark.parametrize("text,limit,length", [
    # an envelope inside the head: just the head
    ("---\ntype: a\n---\nbody body body\n", 20, 20),
    # a closing delimiter past the head: read on, doubling the head, through it
    ("---\n" + "x: 1\n" * 10 + "---\nbody\n" + "tail\n" * 10, 8, 64),
    # the delimiter straddles a chunk boundary
    ("---\nab\n---\nbody\n" + "tail\n" * 10, 8, 16),
    # the first newline itself lies past the head
    ("---" + " " * 20 + "\na: 1\n---\nbody\n", 4, 32),
    # a delimiter right after the opener line is the earliest possible close
    ("---\n\n---\nbody\n", 4, 8),
    # ...but the opener's own newline cannot double as the closing one
    ("---\n---\nab\n---\nbody\n" + "tail\n" * 10, 4, 16),
    # never closed: the whole file, then stop
    ("---\na: 1\nb: 2\n", 4, 14),
    # no envelope: the head only
    ("plain text that is long\n", 5, 5),
])
def test_read_head_extends_through_the_frontmatter_envelope(tmp_path, text, limit, length):
    page = tmp_path / "page.md"
    page.write_text(text)
    assert _directory_index().read_head(page, limit) == text[:length]


def test_read_head_reads_no_further_than_the_closing_chunk(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("---\n" + "k: v\n" * 4 + "---\n" + "body\n" * 100)
    assert _directory_index().read_head(page, 8) == ("---\n" + "k: v\n" * 4 + "---\nbody\nbo")[:32]


def test_backlink_artifact_same_size_rewrite_within_one_tick_is_reloaded(tmp_path, monkeypatch):
    """okengine#1017: the artifact cache was keyed on mtime alone, so a rebuild landing in the
    same timestamp tick as the previous read kept serving the old backlink map."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    artifact = wiki / ".backlinks.json"
    artifact.write_text('{"backlinks":{"concepts/aaa":[]}}')
    assert set(m._artifact_backlinks()) == {"concepts/aaa"}
    before = artifact.stat()
    artifact.write_text('{"backlinks":{"concepts/bbb":[]}}')
    os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert set(m._artifact_backlinks()) == {"concepts/bbb"}


def test_root_namespaces_see_a_same_size_schema_rewrite_within_one_tick(tmp_path):
    """okengine#1017: the declared-namespace cache was keyed on the schema's mtime alone, so a
    schema edited within the same timestamp tick kept the previous namespace set."""
    module = _directory_index()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    schema = tmp_path / "schema.yaml"
    schema.write_text("partitioning: {namespaces: {aaa: {}}}\n", encoding="utf-8")
    assert module._root_namespaces(wiki) == {"aaa"}
    before = schema.stat()
    schema.write_text("partitioning: {namespaces: {bbb: {}}}\n", encoding="utf-8")
    os.utime(schema, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert module._root_namespaces(wiki) == {"bbb"}


def test_page_revision_changes_on_a_same_size_rewrite_within_one_tick(tmp_path):
    """okengine#1017: the revision was mtime_ns:size, so render-lint skipped a page rewritten with
    the same size in the same timestamp tick as if it were unchanged."""
    module = _directory_index()
    page = tmp_path / "page.md"
    page.write_text("body aaa\n", encoding="utf-8")
    first = module._revision(page, page.stat())
    before = page.stat()
    page.write_text("body bbb\n", encoding="utf-8")
    os.utime(page, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert module._revision(page, page.stat()) != first


_SETTLED = 10 * 1_000_000_000  # well past file_fingerprint's settle window


def _restat(st, **changes):
    """A stat result with some fields replaced: a ctime, inode or device change made exactly,
    rather than hoping the filesystem clock ticks between two calls."""
    fields = {name: getattr(st, name)
              for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")}
    fields.update(changes)
    return SimpleNamespace(**fields)


def _newest(st) -> int:
    return max(st.st_mtime_ns, st.st_ctime_ns)


def test_a_settled_page_revision_is_the_legacy_mtime_size_token(tmp_path):
    """okengine#1023: render-lint stores this token per page. v0.15.10 changed its format and so
    invalidated every stored check; a settled page must carry exactly the pre-v0.15.10 token."""
    module = _directory_index()
    page = tmp_path / "page.md"
    page.write_text("settled body\n", encoding="utf-8")
    st = page.stat()
    assert module._revision(page, st, _newest(st) + _SETTLED) == f"{st.st_mtime_ns}:{st.st_size}"


@pytest.mark.parametrize("change", [
    pytest.param(lambda st: {"st_ctime_ns": st.st_ctime_ns + 3_000_000_000}, id="chmod-ctime-only"),
    pytest.param(lambda st: {"st_ino": st.st_ino + 1, "st_dev": st.st_dev + 1}, id="restore-or-move"),
])
def test_a_metadata_only_change_keeps_a_settled_page_revision(tmp_path, change):
    """okengine#1023: a chmod -R, a backup restore or a vault move changes ctime, inode or device
    but not the content, and must not send every page back to render-lint."""
    module = _directory_index()
    page = tmp_path / "page.md"
    page.write_text("settled body\n", encoding="utf-8")
    st = page.stat()
    moved = _restat(st, **change(st))
    before = module._revision(page, st, _newest(st) + _SETTLED)
    assert module._revision(page, moved, _newest(moved) + _SETTLED) == before


def test_a_same_tick_same_size_rewrite_of_an_unsettled_page_changes_its_revision(tmp_path):
    """The okengine#1017 protection the legacy token alone lacks: inside the settle window the
    content digest tells two same-size, same-mtime versions apart."""
    module = _directory_index()
    page = tmp_path / "page.md"
    page.write_text("body aaa\n", encoding="utf-8")
    st = page.stat()
    first = module._revision(page, st, _newest(st))
    page.write_text("body bbb\n", encoding="utf-8")
    os.utime(page, ns=(st.st_atime_ns, st.st_mtime_ns))
    rewritten = _restat(page.stat(), st_ctime_ns=st.st_ctime_ns)
    assert rewritten.st_size == st.st_size and rewritten.st_mtime_ns == st.st_mtime_ns
    assert module._revision(page, rewritten, _newest(st)) != first


def test_a_page_changes_revision_at_most_once_as_it_settles(tmp_path):
    """okengine#1023: render-lint re-checks a page each time its token changes. A fresh page seen
    unsettled, then touched by a chmod inside the window, then settled, has one content and must
    present at most two tokens (one re-check), not three."""
    module = _directory_index()
    page = tmp_path / "page.md"
    page.write_text("fresh body\n", encoding="utf-8")
    st = page.stat()
    chmodded = _restat(st, st_ctime_ns=st.st_ctime_ns + 500_000_000)
    seen = [
        module._revision(page, st, _newest(st)),
        module._revision(page, chmodded, _newest(chmodded)),
        module._revision(page, chmodded, _newest(chmodded) + _SETTLED),
    ]
    assert seen[0] == seen[1], "a ctime-only change inside the window must not re-check the page"
    assert len(set(seen)) == 2, seen


def test_page_revision_is_empty_when_the_page_vanishes_mid_read(tmp_path):
    module = _directory_index()
    page = tmp_path / "page.md"
    page.write_text("body\n", encoding="utf-8")
    stat = page.stat()
    page.unlink()
    assert module._revision(page, stat) == ""


def test_an_unchanged_backlink_artifact_is_served_from_the_reader_cache(tmp_path, monkeypatch):
    """The fingerprint comparison must be equality: a fresh key tuple is never identical, so an
    identity check would re-parse the artifact on every request."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    m = _load(tmp_path, monkeypatch)
    (wiki / ".backlinks.json").write_text('{"backlinks":{"concepts/aaa":[]}}')
    first = m._artifact_backlinks()
    assert m._artifact_backlinks() is first


def test_revision_inventory_skips_only_a_page_that_vanished_mid_scan(tmp_path, monkeypatch):
    """A page that disappears between stat and hash is left out; the scan continues."""
    module = _directory_index()
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for name in ("a", "b", "c"):
        (wiki / f"{name}.md").write_text(name, encoding="utf-8")
    real = module._revision
    calls = []

    def first_vanishes(path, stat):
        calls.append(path)
        return "" if len(calls) == 1 else real(path, stat)

    monkeypatch.setattr(module, "_revision", first_vanishes)
    response, _cache = module.revision_inventory(
        wiki=wiki, cache=(float("-inf"), []), ttl=0, excluded_dirs=frozenset,
        skip=lambda _name: False, reserved=lambda _path: False, namespaces=lambda _path: frozenset())
    assert len(calls) == 3
    assert len(response["pages"]) == 2
