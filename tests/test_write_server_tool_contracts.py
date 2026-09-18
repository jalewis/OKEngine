"""Direct tests for the MCP tool wrappers and actor-specific backfill contracts."""
from __future__ import annotations

import importlib.util
import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "okengine-mcp" / "write_server.py"


def _load(tmp_path, monkeypatch, actor=""):
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {sources: {}, concepts: {}}}\n"
        "permissions: {default: {create: true, update: true, delete: false}}\n"
    )
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    if actor:
        monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    else:
        monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    name = "write_tool_" + (actor.replace(":", "_").replace("-", "_") or "default")
    spec = importlib.util.spec_from_file_location(name, WRITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tool(module, name):
    return module.mcp._tool_manager._tools[name].fn


def test_mcp2_server_fallback_registers_governed_tools(tmp_path, monkeypatch):
    """Hermes' MCP 2.x SDK must not disable every governed writer at import."""
    class MCP2Server:
        def __init__(self, _name):
            self._tool_manager = SimpleNamespace(_tools={})

        def tool(self):
            def register(function):
                self._tool_manager._tools[function.__name__] = SimpleNamespace(fn=function)
                return function
            return register

        def remove_tool(self, name):
            self._tool_manager._tools.pop(name, None)

    mcp2 = ModuleType("mcp.server.mcpserver")
    mcp2.MCPServer = MCP2Server
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", mcp2)

    module = _load(tmp_path, monkeypatch, "cron:okengine.lacuna")

    assert isinstance(module.mcp, MCP2Server)
    assert "converge_entity" in module.mcp._tool_manager._tools


def test_transactional_tools_preserve_writer_identity_precedence(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    writers = []

    @contextmanager
    def transaction(_deployment, *, writer, operation, tracking):
        # okengine#666: the per-tool fence must run in touched mode, never a full-vault snapshot
        assert tracking == "touched"
        writers.append((writer, operation))
        yield

    monkeypatch.setattr(m, "_corpus_mutation", transaction)
    monkeypatch.setattr(m, "_create", lambda *_args: "created")
    create = _tool(m, "create_entity")
    for caller, expected in (
        ({"actor": "cron:lane"}, "cron:lane"),
        ({"ext_id": "okengine.test"}, "okengine.test"),
        ({}, "mcp-write"),
    ):
        token = m._caller_var.set(caller)
        try:
            assert create("entities/e/example", "{}") == "created"
        finally:
            m._caller_var.reset(token)
        assert writers[-1] == (expected, "create_entity")


def test_generic_tool_wrappers_validate_and_delegate(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_update", lambda *args: f"updated:{args!r}")
    score = _tool(m, "score_source")
    assert "integer" in score("sources/a", "A", "bad")
    assert "reliability" in score("sources/a", "Z", 2)
    assert "integer" in score("sources/a", "A", 7)
    assert score("sources/a", " b ", 2).startswith("updated:")

    delegates = {
        "_create": ("create_entity", ("p", "type: x", "body")),
        "_tombstone": ("tombstone_entity", ("p", "reason", None)),
        "_flag": ("flag_for_review", ("p", "note")),
        "_patch": ("patch_entity", ("p", "old", "new")),
        "_record_machine_review": ("record_machine_review", ("p", "eval", "ok", "note")),
    }
    for internal, (tool_name, expected) in delegates.items():
        monkeypatch.setattr(m, internal, lambda *args, _name=internal: (_name, args))
        args = {
            "create_entity": ("p", "type: x", "body"),
            "tombstone_entity": ("p", "reason", ""),
            "flag_for_review": ("p", "note"),
            "patch_entity": ("p", "old", "new"),
            "record_machine_review": ("p", "eval", "ok", "note"),
        }[tool_name]
        result = _tool(m, tool_name)(*args)
        assert result[0] == internal
        assert result[1] == expected

    monkeypatch.setattr(m, "_append_section", lambda *_a: "written")
    assert _tool(m, "append_to_section")("p", "H", "text") == "written"
    monkeypatch.setattr(m, "_converge", lambda *_a: "converged")
    assert _tool(m, "converge_entity")("p", "type: x") == "converged"
    # Human review DECISIONS are not MCP tools (okengine#661): the sidecar HTTP app and the
    # `framework review` CLI are the only routes into _resolve_review/_assign_review.
    assert "resolve_review" not in m.mcp._tool_manager._tools
    assert "assign_review" not in m.mcp._tool_manager._tools


def test_create_entity_enforces_actor_admission_for_every_caller(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n"
        "identity_admission:\n"
        "  actor:\n"
        "    excluded_exact_titles: [North Korea]\n"
    )
    monkeypatch.setattr(m, "_create", lambda *_args: "created")
    create = _tool(m, "create_entity")
    assert "generic class labels" in create(
        "entities/unsafe", "type: actor\ntitle: Unsafe\nactor_type: unknown", ""
    )
    assert "software/tooling" in create(
        "entities/sleepwalker",
        "type: actor\ntitle: SLEEPWALKER\nactor_type: unknown",
        "SLEEPWALKER is a Windows backdoor.",
    )
    assert "geopolitical entities" in create(
        "entities/north-korea",
        "type: actor\ntitle: North Korea\nactor_type: nation-state",
        "Country attribution.",
    )
    assert create(
        "entities/comment-crew",
        "type: actor\ntitle: Comment Crew\nactor_type: nation-state",
        "Named intrusion set.",
    ) == "created"


def test_backfill_tool_success_and_terminal_guidance(tmp_path, monkeypatch):
    source_quality = _load(tmp_path / "quality", monkeypatch, "cron:source-quality-backfill")
    monkeypatch.setattr(source_quality, "_update", lambda *_a: "wrote")
    assert "SUCCESS:" in _tool(source_quality, "score_source")("sources/a", "a", 1)
    monkeypatch.setattr(source_quality, "_update", lambda *_a: "rejected: denied")
    assert _tool(source_quality, "score_source")("sources/a", "a", 1) == "rejected: denied"

    page_quality = _load(tmp_path / "page", monkeypatch, "cron:page-quality-enrich")
    monkeypatch.setattr(page_quality, "_append_section", lambda *_a: "wrote")
    assert "exact fenced okengine-receipt" in _tool(
        page_quality, "append_to_section"
    )("p", "H", "text")
    monkeypatch.setattr(page_quality, "_append_section", lambda *_a: "error: denied")
    assert _tool(page_quality, "append_to_section")("p", "H", "text") == "error: denied"

    entity = _load(tmp_path / "entity", monkeypatch, "cron:entity-backfill")
    monkeypatch.setattr(entity, "_entity_backfill_frontmatter", lambda *_a: (None, "rejected: yaml"))
    assert "rejected: yaml" in _tool(entity, "converge_entity")("entities/a", "bad")
    monkeypatch.setattr(entity, "_entity_backfill_frontmatter", lambda *_a: ("type: actor", None))
    monkeypatch.setattr(entity, "_converge", lambda *_a: "refused: conflict")
    assert "TERMINAL:" in _tool(entity, "converge_entity")("entities/a", "type: actor")
    monkeypatch.setattr(entity, "_converge", lambda *_a: "wrote")
    assert "SUCCESS:" in _tool(entity, "converge_entity")("entities/a", "type: actor")


def test_raw_backfill_tool_normalizes_identity_and_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / "raw-selection.json"
    manifest.write_text(json.dumps({"selected": ["raw/item.md"]}))
    raw = tmp_path / "raw" / "raw" / "item.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("---\nurl: https://example.test/report\n---\n")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest))
    m = _load(tmp_path / "raw", monkeypatch, "cron:raw-backfill")
    converge = _tool(m, "converge_source")
    assert "only sources" in converge("entities/a", {})
    monkeypatch.setattr(m, "_raw_backfill_frontmatter", lambda *_a: ({}, "rejected: bad"))
    assert converge("sources/a", {}) == "rejected: bad"

    monkeypatch.setattr(m, "_raw_backfill_frontmatter", lambda value: (dict(value), None))
    monkeypatch.setattr(m, "_selected_raw_url", lambda _selected: "")
    assert "source url is required" in converge("sources/a", {})

    captured = {}

    def write(path, frontmatter, *_args):
        captured["path"] = path
        captured["frontmatter"] = frontmatter
        return "wrote"

    monkeypatch.setattr(m, "_converge", write)
    result = converge("wiki/sources/report", {"url": "https://example.test/report"})
    assert "SUCCESS:" in result
    assert captured["path"].startswith("sources/report-")
    assert "sources:url-" in captured["frontmatter"]
    assert "raw/item.md" in captured["frontmatter"]
    result = converge(
        "wiki/sources/report",
        {"url": "https://example.test/report", "raw": "raw/unrelated.md"},
    )
    assert "SUCCESS:" in result
    assert "raw/item.md" in captured["frontmatter"]
    assert "raw/unrelated.md" not in captured["frontmatter"]
    monkeypatch.setattr(m, "_converge", lambda *_a: "rejected: duplicate")
    assert "TERMINAL:" in converge("sources/report", {"url": "https://example.test/report"})


def test_concept_backfill_tool_sanitizes_cross_namespace_ids(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:concept-backfill")
    converge = _tool(m, "converge_concept")
    assert "only concepts" in converge("entities/a")
    assert "invalid frontmatter" in converge("concepts/a", "invalid: [")
    assert "decode to a mapping" in converge("concepts/a", "- item")
    captured = {}

    def write(path, frontmatter, *_args):
        captured.update(path=path, frontmatter=frontmatter)
        return "wrote"

    monkeypatch.setattr(m, "_converge", write)
    result = converge("wiki/concepts/my-topic", "id: sources:wrong")
    assert "SUCCESS:" in result
    assert "type: concept" in captured["frontmatter"]
    assert "title: My Topic" in captured["frontmatter"]
    assert "sources:wrong" not in captured["frontmatter"]
    monkeypatch.setattr(m, "_converge", lambda *_a: "error: write")
    assert "TERMINAL:" in converge("concepts/a", "id: concepts:other")


def test_scoped_write_auth_admin_extension_and_rejection(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    called = []

    async def app(scope, receive, send):
        called.append((scope["path"], m._caller_var.get()))

    middleware = m._ScopedWriteAuth(app, "admin")

    async def invoke(path="/write", authorization=None, scope_type="http"):
        messages = []
        headers = [] if authorization is None else [(b"authorization", authorization)]

        async def send(message):
            messages.append(message)

        await middleware(
            {"type": scope_type, "path": path, "headers": headers},
            lambda: None,
            send,
        )
        return messages

    denied = asyncio.run(invoke())
    assert denied[0]["status"] == 401
    asyncio.run(invoke("/healthz"))
    assert called[-1][0] == "/healthz"
    asyncio.run(invoke(authorization=b"Bearer admin"))
    assert called[-1][1]["kind"] == "admin"

    monkeypatch.setattr(
        m._scope,
        "resolve",
        lambda token: {
            "ext_id": "ext.one",
            "actor": "",
            "write_scopes": ["wiki/entities"],
            "write_capability": {"operations": ["update"]},
        } if token == "extension" else None,
    )
    asyncio.run(invoke(authorization=b"Bearer extension"))
    caller = called[-1][1]
    assert caller["kind"] == "extension"
    assert caller["actor"] == "extension:ext.one"
    assert caller["write_scopes"] == ["wiki/entities"]
    assert caller["write_capability"] == {"operations": ["update"]}

    monkeypatch.setattr(m._scope, "resolve", lambda _token: {"ext_id": "empty"})
    asyncio.run(invoke(authorization=b"Bearer minimal"))
    assert called[-1][1] == {
        "kind": "extension",
        "ext_id": "empty",
        "actor": "extension:empty",
        "write_scopes": [],
        "write_capability": {},
    }

    # Non-HTTP scopes and the health endpoint bypass bearer resolution. Use
    # dynamically-built strings and values on both lexical sides of "http" so
    # equality cannot be weakened to identity or an ordering comparison.
    monkeypatch.setattr(m._scope, "resolve", lambda _token: None)
    before = len(called)
    for scope_type in ("asgi", "".join(["ht", "tp"]), "websocket"):
        path = "/healthz" if scope_type == "http" else "/write"
        asyncio.run(invoke(path=path, authorization=None, scope_type=scope_type))
    assert len(called) == before + 3
    dynamic_health = "".join(["/health", "z"])
    asyncio.run(invoke(path=dynamic_health))
    assert len(called) == before + 4
    denied_low_path = asyncio.run(invoke(path="/aaa"))
    assert denied_low_path[0]["status"] == 401
    assert len(called) == before + 4


def test_review_http_routes_bad_json_and_delegation(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    app = m._review_http_app()
    endpoints = {
        route.path: route.endpoint
        for route in app.routes
        if getattr(route, "path", "").startswith(("/healthz", "/review/"))
    }
    assert endpoints["/healthz"]() == {"ok": True}

    class Request:
        def __init__(self, value=None, fail=False):
            self.value, self.fail = value, fail

        async def json(self):
            if self.fail:
                raise ValueError("bad")
            return self.value

    for path in ("/review/resolve", "/review/assign", "/review/machine"):
        response = asyncio.run(endpoints[path](Request(fail=True)))
        assert response.status_code == 400
        assert json.loads(response.body) == {"ok": False, "error": "invalid JSON"}

    calls = []
    monkeypatch.setattr(
        m,
        "_resolve_review",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or {"ok": True, "status": 201},
    )
    response = asyncio.run(
        endpoints["/review/resolve"](
            Request(
                {
                    "path": "entities/a",
                    "decision": "approve",
                    "reviewer": "me",
                    "note": "because",
                    "expected_version": 1,
                    "expected_hash": "hash",
                    "review_id": "review-1",
                    "service": "analyst-ui",
                }
            )
        )
    )
    assert response.status_code == 201
    assert calls.pop() == ((
        "entities/a", "approve", "me", "because", 1, "hash", "review-1"
    ), {"service": "analyst-ui"})

    # Missing optional values must map to the documented defaults, not truthy
    # substitutes introduced by a weakened `or` chain.
    response = asyncio.run(endpoints["/review/resolve"](Request({})))
    assert response.status_code == 201
    assert calls.pop() == (("", "", "", "", None, "", None), {"service": "cockpit"})

    calls = []
    monkeypatch.setattr(
        m, "_assign_review", lambda *args, **kwargs: calls.append((args, kwargs))
        or {"ok": True, "status": 200}
    )
    response = asyncio.run(
        endpoints["/review/assign"](
            Request(
                {
                    "path": "entities/a",
                    "reviewer": "me",
                    "expected_version": 1,
                    "expected_hash": "hash",
                    "review_id": "review-2",
                    "service": "analyst-ui",
                }
            )
        )
    )
    assert response.status_code == 200
    assert calls.pop() == (("entities/a", "me", 1, "hash", "review-2"),
                           {"service": "analyst-ui"})
    response = asyncio.run(endpoints["/review/assign"](Request({})))
    assert response.status_code == 200
    assert calls.pop() == (("", "", None, "", None), {"service": "cockpit"})

    calls = []
    monkeypatch.setattr(
        m,
        "_record_machine_review",
        lambda *args: calls.append(args) or {"ok": False, "status": 409},
    )
    response = asyncio.run(
        endpoints["/review/machine"](
            Request({"path": "entities/a", "evaluator": "grader",
                     "outcome": "fail", "note": "unsupported"})
        )
    )
    assert response.status_code == 409
    assert calls.pop() == ("entities/a", "grader", "fail", "unsupported")
    response = asyncio.run(endpoints["/review/machine"](Request({})))
    assert response.status_code == 409
    assert calls.pop() == ("", "machine", "", "")

    # A service result without an explicit HTTP status is an internal error on
    # every route; never accidentally return a success-class replacement.
    monkeypatch.setattr(m, "_resolve_review", lambda *_a, **_k: {"ok": False})
    monkeypatch.setattr(m, "_assign_review", lambda *_a, **_k: {"ok": False})
    monkeypatch.setattr(m, "_record_machine_review", lambda *_a: {"ok": False})
    for path in ("/review/resolve", "/review/assign", "/review/machine"):
        assert asyncio.run(endpoints[path](Request({}))).status_code == 500
