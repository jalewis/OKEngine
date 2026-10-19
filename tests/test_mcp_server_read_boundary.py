"""Read-MCP security boundary: HTTP auth opt-outs, scope authorization, and path canonicalization.

These pin the read server's (okengine-mcp/server.py) refusal behavior with real temp vaults and the
real scope library: an opt-out flag is honored only at its exact value, an extension caller reads
only inside its declared scopes, and every accepted spelling of a page path resolves to the same
file the write surface would name (#612).
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "okengine-mcp" / "server.py"


def _load(tmp_path, monkeypatch, name="mcp_read_boundary"):
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    spec = importlib.util.spec_from_file_location(name, SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _page(root: Path, rel: str, text: str = "---\ntitle: T\n---\nbody\n") -> Path:
    path = root / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _Caller:
    def __init__(self, module, caller):
        self.module, self.caller = module, caller

    def __enter__(self):
        self.token = self.module._caller_var.set(self.caller)
        return self

    def __exit__(self, *_exc):
        self.module._caller_var.reset(self.token)


# --- HTTP auth resolution ------------------------------------------------------------------


@pytest.mark.parametrize("value", ["2", "true", "yes", "11", "1 ", "on"])
def test_only_the_exact_opt_out_value_serves_unauthenticated(tmp_path, monkeypatch, value):
    m = _load(tmp_path, monkeypatch)

    token, warning = m._resolve_http_auth(
        {"OKENGINE_MCP_ALLOW_UNAUTHENTICATED": value, "OKENGINE_MCP_TOKEN": "s3cret"}, "0.0.0.0")

    assert token == "s3cret"
    assert warning is None


@pytest.mark.parametrize("value", ["2", "true", "0", ""])
def test_only_the_exact_default_token_opt_in_serves_it_off_loopback(tmp_path, monkeypatch,
                                                                    value):
    m = _load(tmp_path, monkeypatch)

    with pytest.raises(SystemExit, match="built-in DEFAULT token"):
        m._resolve_http_auth({"OKENGINE_MCP_ALLOW_DEFAULT_TOKEN": value}, "10.0.0.5")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_every_loopback_spelling_serves_the_default_token_silently(tmp_path, monkeypatch, host):
    m = _load(tmp_path, monkeypatch)

    assert m._resolve_http_auth({}, host) == (m.DEFAULT_LOCAL_TOKEN, None)


# --- caller authorization ------------------------------------------------------------------


def test_admin_is_recognized_by_kind_value_not_identity(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    kind = "".join(["ad", "min"])

    with _Caller(m, {"kind": kind, "read_scopes": []}):
        assert m._authorize_read("concepts/anything") is True


def test_an_extension_reads_only_inside_its_declared_scopes(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "entities/a/acme.md")
    _page(tmp_path, "concepts/secret.md")

    with _Caller(m, {"kind": "extension", "ext_id": "ext.one",
                     "read_scopes": ["wiki/entities/**"]}):
        assert m._authorize_read("entities/a/acme") is True
        assert m._authorize_read("concepts/secret") is False
        assert "body" in m.get_page("entities/a/acme")
        assert m.get_page("concepts/secret") == "(refused: outside this caller's read scope)"
        assert m.get_page("entities/../concepts/secret") == (
            "(refused: outside this caller's read scope)")


def test_an_extension_without_scopes_reads_nothing(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "entities/a/acme.md")

    for scopes in (None, []):
        with _Caller(m, {"kind": "extension", "ext_id": "ext.none", "read_scopes": scopes}):
            assert m._authorize_read("entities/a/acme") is False
            assert m.get_page("entities/a/acme") == "(refused: outside this caller's read scope)"


def test_scoped_auth_refuses_unknown_tokens_and_binds_the_extension_scopes(tmp_path,
                                                                          monkeypatch):
    m = _load(tmp_path, monkeypatch)
    seen = []

    async def app(scope, _receive, _send):
        seen.append(m._caller())

    middleware = m._ScopedAuth(app, "admin-secret")

    def invoke(authorization):
        sent = []

        async def send(message):
            sent.append(message)

        headers = [(b"authorization", authorization)] if authorization is not None else []
        asyncio.run(middleware({"type": "http", "path": "/mcp", "headers": headers},
                               lambda: None, send))
        return sent

    monkeypatch.setattr(m._scope, "resolve", lambda token: None)
    for header in (None, b"", b"Bearer ", b"Bearer admin-secretx", b"bearer admin-secret",
                   b"Basic admin-secret", b"admin-secret", b"Bearer  admin-secret"):
        sent = invoke(header)
        assert sent and sent[0]["status"] == 401, header
        assert sent[1]["body"] == b"unauthorized"
    assert seen == []

    monkeypatch.setattr(m._scope, "resolve",
                        lambda token: {"ext_id": "ext.two"} if token == "ext-token" else None)
    assert invoke(b"Bearer ext-token") == []
    assert seen[-1] == {"kind": "extension", "ext_id": "ext.two", "read_scopes": []}


def test_an_empty_admin_token_never_matches_an_empty_bearer(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m._scope, "resolve", lambda token: None)
    reached = []

    async def app(_scope, _receive, _send):
        reached.append(True)

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(m._ScopedAuth(app, "")({"type": "http", "path": "/mcp",
                                        "headers": [(b"authorization", b"Bearer ")]},
                                       lambda: None, send))

    assert reached == [] and sent[0]["status"] == 401


# --- path canonicalization -----------------------------------------------------------------


def test_every_accepted_spelling_resolves_to_the_same_page(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = _page(tmp_path, "concepts/topic/example.md")
    wiki = (tmp_path / "wiki").resolve()

    for spelling in ("concepts/topic/example", "concepts/topic/example.md",
                     "/concepts/topic/example", "wiki/concepts/topic/example",
                     "wiki/wiki/concepts/topic/example", f"{wiki}/concepts/topic/example",
                     f"{wiki.parent}/wiki/concepts/topic/example",
                     f"{str(wiki).lstrip('/')}/concepts/topic/example",
                     f"{wiki}/{wiki}/concepts/topic/example",
                     "  concepts/topic/example  "):
        assert m._safe(spelling) == page.resolve(), spelling


def test_a_dotted_slug_keeps_its_dots(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = _page(tmp_path, "sources/report-v1.2.md")

    assert m._safe("sources/report-v1.2") == page.resolve()
    assert m._safe("sources/report-v1.2.md") == page.resolve()


def test_traversal_outside_the_wiki_is_refused(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    (tmp_path / "secret.md").write_text("x", encoding="utf-8")

    assert m._safe("../secret") is None
    assert m._safe("entities/../../secret") is None
    assert m.get_page("../secret") == "(refused: path outside the vault)"


def test_a_flat_entity_slug_finds_its_one_letter_shard(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    sharded = _page(tmp_path, "entities/a/acme.md")

    assert m._safe("entities/acme") == sharded.resolve()
    assert m._safe("entities/acme.md") == sharded.resolve()
    # A missing slug stays at its requested (flat) spelling, so "not found" names what was asked.
    assert m._safe("entities/Acme") == (tmp_path / "wiki/entities/Acme.md").resolve()


def test_a_flat_entity_slug_finds_its_two_letter_shard(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    deep = _page(tmp_path, "entities/a/c/acme.md")

    assert m._safe("entities/acme") == deep.resolve()
    assert m._safe("entities/a/acme") == deep.resolve()


def test_a_non_alphanumeric_second_character_uses_the_underscore_shard(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    deep = _page(tmp_path, "entities/a/_/a-team.md")

    assert m._safe("entities/a-team") == deep.resolve()


def test_a_one_character_entity_slug_uses_the_underscore_shard_when_two_level(tmp_path,
                                                                             monkeypatch):
    m = _load(tmp_path, monkeypatch)
    deep = _page(tmp_path, "entities/x/_/x.md")

    assert m._safe("entities/x") == deep.resolve()


def test_a_missing_slug_under_a_two_level_letter_directory_stays_at_its_request(
        tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "entities/a/c/other.md")

    assert m._safe("entities/abc") == (tmp_path / "wiki/entities/abc.md").resolve()
    assert m.get_page("entities/abc") == "(not found: entities/abc)"


def test_a_flat_legacy_entity_wins_when_no_shard_exists(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    flat = _page(tmp_path, "entities/legacy.md")

    assert m._safe("entities/legacy") == flat.resolve()


def test_an_existing_one_letter_seat_beats_a_two_level_letter_directory(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    one = _page(tmp_path, "entities/a/acme.md")
    _page(tmp_path, "entities/a/b/other.md")

    assert m._safe("entities/acme") == one.resolve()


@pytest.mark.parametrize("name", ["INDEX", "index.md", "Index"])
def test_an_entities_index_file_is_served_where_it_sits(tmp_path, monkeypatch, name):
    m = _load(tmp_path, monkeypatch)
    index = _page(tmp_path, "entities/a/INDEX.md" if name == "INDEX" else f"entities/a/{name}"
                  if name.endswith(".md") else f"entities/a/{name}.md")

    assert m._safe(f"entities/a/{name}") == index.resolve()


def test_a_non_entity_page_is_never_rerouted_to_an_entity_shard(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "entities/a/acme.md")

    assert m._safe("concepts/acme") == (tmp_path / "wiki/concepts/acme.md").resolve()


def test_a_multi_letter_middle_segment_is_not_an_entity_shard(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = _page(tmp_path, "entities/group/acme.md")
    _page(tmp_path, "entities/a/acme.md")

    assert m._safe("entities/group/acme") == page.resolve()


# --- list_pages ----------------------------------------------------------------------------


def test_list_pages_orders_by_the_first_present_date_field(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "items/u.md", "---\ntitle: U\nupdated: 2026-01-04\ncreated: 2020-01-01\n---\n")
    _page(tmp_path, "items/r.md", "---\ntitle: R\nresolves_by: 2026-01-03\ncreated: 2030-01-01\n---\n")
    _page(tmp_path, "items/m.md", "---\ntitle: M\nmade_on: 2026-01-02\ncreated: 2031-01-01\n---\n")
    _page(tmp_path, "items/c.md", "---\ntitle: C\ncreated: 2026-01-01\n---\n")

    lines = [line for line in m.list_pages("items").splitlines() if line.startswith("- ")]

    assert lines == [
        "- U [2026-01-04] — items/u",
        "- R [2026-01-03] — items/r",
        "- M [2026-01-02] — items/m",
        "- C [2026-01-01] — items/c",
    ]


def test_list_pages_names_only_the_filters_that_were_applied_when_empty(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "items/a.md", "---\ntitle: A\ntype: item\nstatus: open\n---\n")

    assert m.list_pages("items", "", "closed") == "(no pages in 'items' status='closed')"
    assert m.list_pages("items", "other", "") == "(no pages in 'items' type='other')"
    assert m.list_pages("items", "other", "closed") == (
        "(no pages in 'items' type='other' status='closed')")


# --- value-not-identity and ordering on the auth checks ------------------------------------


def test_a_runtime_built_default_token_is_still_refused_off_loopback(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    token = "".join(["okengine", "-local"])
    assert token == m.DEFAULT_LOCAL_TOKEN and token is not m.DEFAULT_LOCAL_TOKEN

    with pytest.raises(SystemExit, match="built-in DEFAULT token"):
        m._resolve_http_auth({"OKENGINE_MCP_TOKEN": token}, "0.0.0.0")


@pytest.mark.parametrize("token", ["abc", "OKENGINE", "a-very-long-secret", "zz-secret"])
def test_a_secret_sorting_either_side_of_the_default_serves_without_warning(tmp_path,
                                                                            monkeypatch, token):
    m = _load(tmp_path, monkeypatch)

    assert m._resolve_http_auth({"OKENGINE_MCP_TOKEN": token}, "0.0.0.0") == (token, None)


def test_scoped_auth_authenticates_a_runtime_built_http_scope(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m._scope, "resolve", lambda token: None)
    reached, sent = [], []

    async def app(_scope, _receive, _send):
        reached.append(True)

    async def send(message):
        sent.append(message)

    scope_type = "".join(["ht", "tp"])
    asyncio.run(m._ScopedAuth(app, "admin-secret")(
        {"type": scope_type, "path": "/mcp", "headers": []}, lambda: None, send))

    assert reached == [] and sent[0]["status"] == 401


def test_projection_queries_admit_admin_by_kind_value(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)

    with _Caller(m, {"kind": "".join(["ad", "min"]), "read_scopes": None}):
        m._authorize_projection_query()


@pytest.mark.parametrize("scopes", [["wiki/entities/**"], [], None])
def test_projection_queries_refuse_a_partial_scope_extension(tmp_path, monkeypatch, scopes):
    m = _load(tmp_path, monkeypatch)

    with _Caller(m, {"kind": "extension", "read_scopes": scopes}):
        with pytest.raises(PermissionError, match="full-vault read scope"):
            m._authorize_projection_query()


@pytest.mark.parametrize("scopes", [["wiki/**"], ["**"], ["*"]])
def test_projection_queries_admit_a_full_vault_extension(tmp_path, monkeypatch, scopes):
    m = _load(tmp_path, monkeypatch)

    with _Caller(m, {"kind": "extension", "read_scopes": scopes}):
        m._authorize_projection_query()


def test_get_page_returns_at_most_sixteen_thousand_characters(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    _page(tmp_path, "notes/long.md", "x" * 20000)
    _page(tmp_path, "notes/edge.md", "y" * 16000)

    assert m.get_page("notes/long") == "x" * 16000
    assert m.get_page("notes/edge") == "y" * 16000


def test_the_search_tool_defaults_to_eight_results(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    calls = []

    async def fake_search(*args):
        calls.append(args)
        return "ok"

    monkeypatch.setattr(m, "_search", fake_search)
    tool = m.mcp._tool_manager._tools["search"].fn

    assert asyncio.run(tool("acme")) == "ok"
    assert calls == [("acme", "search", 8, "")]
