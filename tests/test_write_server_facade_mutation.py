"""Facade contracts of okengine-mcp/write_server.py that its service tests cannot see (#612).

The bounded services own the write semantics; the facade owns which MCP tools exist, how a tool's
arguments reach its service, the actor-specific tool narrowing, and the process entry point that
decides which transport and which authentication wrapper serve the enforced write path.
"""
from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("mcp")
uvicorn = pytest.importorskip("uvicorn")

REPO = Path(__file__).resolve().parent.parent
WRITE = REPO / "okengine-mcp" / "write_server.py"
VAULT_DEFAULT = Path("/opt/vault")   # state.VAULT when WIKI_PATH is unset or blank

GENERIC_TOOLS = {
    "create_entity", "update_entity", "score_source", "tombstone_entity", "flag_for_review",
    "patch_entity", "append_to_section", "record_machine_review", "converge_entity",
}


def _vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {sources: {}, concepts: {}}}\n"
        "permissions: {default: {create: true, update: true, delete: false}}\n"
    )
    # Every deployment vault declares its pack; the entry point refuses to serve without one
    # (okengine#1027).
    (tmp_path / "pack.yaml").write_text("name: okpack-facade\n")
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    for name in ("OKENGINE_WRITE_ACTOR", "OKENGINE_WRITE_TOOLS", "OKENGINE_WRITE_TRANSPORT",
                 "OKENGINE_WRITE_HOST", "OKENGINE_WRITE_PORT", "OKENGINE_WRITE_REVIEW_ONLY",
                 "OKENGINE_WRITE_TOKEN", "OKENGINE_MCP_TOKEN",
                 "OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _load(tmp_path, monkeypatch, actor="", tools=""):
    _vault(tmp_path, monkeypatch)
    if actor:
        monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    if tools:
        monkeypatch.setenv("OKENGINE_WRITE_TOOLS", tools)
    name = "write_facade_" + (actor.replace(":", "_").replace("-", "_").replace(".", "_")
                              or "default") + ("_env" if tools else "")
    spec = importlib.util.spec_from_file_location(name, WRITE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tools(module) -> set[str]:
    return set(module.mcp._tool_manager._tools)


def _tool(module, name):
    return module.mcp._tool_manager._tools[name].fn


# --- tool registry -------------------------------------------------------------------------


def test_an_unbound_writer_registers_exactly_the_generic_governed_tools(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)

    assert _tools(m) == GENERIC_TOOLS


@pytest.mark.parametrize("actor, expected", [
    ("cron:source-quality-backfill", {"score_source"}),
    ("cron:entity-backfill", {"converge_entity"}),
    ("cron:concept-backfill", {"converge_concept"}),
    ("cron:raw-backfill", {"converge_source"}),
    ("cron:page-quality-enrich",
     {"update_entity", "patch_entity", "append_to_section", "converge_entity"}),
])
def test_a_bound_engine_lane_sees_only_its_operation_contract(tmp_path, monkeypatch, actor,
                                                              expected):
    m = _load(tmp_path, monkeypatch, actor)

    assert _tools(m) == expected


def test_an_unlisted_actor_keeps_every_generic_tool(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:some-pack-lane")

    assert _tools(m) == GENERIC_TOOLS


def test_declared_write_tools_override_the_engine_table(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:entity-backfill",
              tools=" update_entity , ,patch_entity,")

    assert _tools(m) == {"update_entity", "patch_entity"}


def test_a_blank_write_tools_value_falls_back_to_the_engine_table(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:source-quality-backfill", tools="  ")

    assert _tools(m) == {"score_source"}


# --- argument plumbing ---------------------------------------------------------------------


def test_update_entity_passes_supplied_frontmatter_and_maps_empty_to_keep(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(m, "_update", lambda *args: calls.append(args) or "updated")
    update = _tool(m, "update_entity")

    assert update("entities/a", "status: active", "new body", "abc") == "updated"
    assert update("entities/a") == "updated"
    assert update("entities/a", "", "") == "updated"

    assert calls == [
        ("entities/a", "status: active", "new body", "abc"),
        ("entities/a", None, None, ""),
        ("entities/a", None, "", ""),
    ]


def test_update_entity_really_rewrites_a_page_through_the_tool(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki" / "notes" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\nid: notes:a\ntype: note\ntitle: A\nstatus: draft\n---\nold body\n", encoding="utf-8")

    result = _tool(m, "update_entity")("notes/a", "status: active")

    assert not result.startswith(("rejected:", "refused:", "error:")), result
    text = page.read_text(encoding="utf-8")
    assert "status: active" in text and "old body" in text


@pytest.mark.parametrize("supplied", ["concepts:my-topic", "mitre:T1059", "wikidata:Q42"])
def test_concept_backfill_keeps_an_id_that_matches_the_slug_or_names_another_authority(
        tmp_path, monkeypatch, supplied):
    m = _load(tmp_path, monkeypatch, "cron:concept-backfill")
    captured = {}

    def converge(path, frontmatter, *_args):
        captured.update(path=path, frontmatter=frontmatter)
        return "wrote"

    monkeypatch.setattr(m, "_converge", converge)
    result = _tool(m, "converge_concept")("concepts/my-topic", f"id: {supplied}")

    assert "SUCCESS:" in result
    assert f"id: {supplied}" in captured["frontmatter"]


def test_concept_backfill_hands_converge_the_author_key_order_and_raw_unicode(tmp_path,
                                                                             monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:concept-backfill")
    captured = {}
    monkeypatch.setattr(m, "_converge",
                        lambda _path, frontmatter, *_a: captured.update(fm=frontmatter) or "ok")

    _tool(m, "converge_concept")("concepts/cafe", "zeta: 1\nsummary: Café crème\nalpha: 2")

    assert captured["fm"].splitlines() == [
        "zeta: 1", "summary: Café crème", "alpha: 2", "type: concept", "title: Cafe",
    ]


@pytest.mark.parametrize("supplied", ["concepts:aaa", "concepts:zzz", "sources:my-topic"])
def test_concept_backfill_drops_a_foreign_id_on_either_side_of_the_slug(tmp_path, monkeypatch,
                                                                        supplied):
    m = _load(tmp_path, monkeypatch, "cron:concept-backfill")
    captured = {}
    monkeypatch.setattr(m, "_converge",
                        lambda _path, frontmatter, *_a: captured.update(fm=frontmatter) or "ok")

    _tool(m, "converge_concept")("concepts/my-topic", f"id: {supplied}")

    assert "id:" not in captured["fm"]


def test_converge_entity_hands_an_unbound_callers_payload_through_untouched(tmp_path,
                                                                           monkeypatch):
    m = _load(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(m, "_converge", lambda *args: calls.append(args) or "converged")
    payload = "id: sources:report-1\ntype: actor\ntitle: Example"

    result = _tool(m, "converge_entity")("entities/e/example", payload, "body", "", "alias", "h")

    assert result == "converged"
    assert calls == [("entities/e/example", payload, "body", "", "alias", "h")]


def test_entity_backfill_converges_the_sanitized_payload(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:entity-backfill")
    calls = []
    monkeypatch.setattr(m, "_entity_backfill_frontmatter",
                        lambda *_a: ("type: actor\ntitle: Example", None))
    monkeypatch.setattr(m, "_converge", lambda *args: calls.append(args) or "wrote")

    result = _tool(m, "converge_entity")("entities/e/example", "id: sources:x\ntype: actor")

    assert "SUCCESS:" in result
    assert calls[0][1] == "type: actor\ntitle: Example"


@pytest.mark.parametrize("credibility, accepted", [
    (0, False), (1, True), (6, True), (7, False), (-1, False), ("3", True)])
def test_score_source_accepts_exactly_the_admiralty_credibility_range(tmp_path, monkeypatch,
                                                                      credibility, accepted):
    m = _load(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(m, "_update", lambda *args: calls.append(args) or "updated")

    result = _tool(m, "score_source")("sources/a", "c", credibility)

    if accepted:
        assert result == "updated"
        assert calls == [("sources/a", {"reliability": "C", "credibility": int(credibility)}, None)]
    else:
        assert result == "rejected: credibility must be an integer from 1 through 6"
        assert calls == []


def test_absent_converge_libs_disable_converge_but_keep_the_writer(tmp_path, monkeypatch):
    # The facade's own import of the converge libs is optional (a host without them keeps the
    # plain create/update tools). The services were imported at collection, so blocking a lib now
    # reaches only the facade's guarded import.
    from okengine.write_services import (  # noqa: F401 - preload, see above
        authorization, binding, convergence, deduplication, integrity, patching, policy,
        raw_sources, review, state, transactions, transport, validation,
    )
    monkeypatch.setitem(sys.modules, "converge", None)
    m = _load(tmp_path, monkeypatch)

    assert m._CONVERGE_OK is False
    assert _tools(m) == GENERIC_TOOLS
    assert _tool(m, "converge_entity")("entities/a", "type: x") == (
        "rejected: converge unavailable (id/schema libs not importable)")


# --- process entry point -------------------------------------------------------------------


def _run_main(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with mock.patch.object(uvicorn, "run", autospec=True) as run:
        namespace = runpy.run_path(str(WRITE), run_name="__main__")
    return run, namespace


def test_http_transport_serves_the_mcp_app_behind_scoped_write_auth(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    run, ns = _run_main(monkeypatch, OKENGINE_WRITE_TRANSPORT="http",
                        OKENGINE_WRITE_TOKEN="secret-token", OKENGINE_WRITE_PORT="9123")

    run.assert_called_once()
    app = run.call_args.args[0]
    assert isinstance(app, ns["_ScopedWriteAuth"])
    assert app.admin_token == "secret-token"
    assert type(app.app).__name__ == "Starlette"
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9123}


def test_streamable_http_is_the_same_networked_surface(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    run, ns = _run_main(monkeypatch, OKENGINE_WRITE_TRANSPORT="streamable-http",
                        OKENGINE_MCP_TOKEN="mcp-token", OKENGINE_WRITE_HOST="0.0.0.0")

    app = run.call_args.args[0]
    assert isinstance(app, ns["_ScopedWriteAuth"]) and app.admin_token == "mcp-token"
    assert run.call_args.kwargs == {"host": "0.0.0.0", "port": 8731}


def test_review_only_mode_serves_only_the_review_routes(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    run, _ns = _run_main(monkeypatch, OKENGINE_WRITE_TRANSPORT="http",
                         OKENGINE_WRITE_TOKEN="t", OKENGINE_WRITE_REVIEW_ONLY="1")

    inner = run.call_args.args[0].app
    paths = {getattr(route, "path", "") for route in inner.routes}
    assert {"/healthz", "/review/resolve", "/review/assign", "/review/machine"} <= paths
    assert "/mcp" not in paths


@pytest.mark.parametrize("flag", ["0", "true", "11", ""])
def test_only_the_exact_review_only_flag_selects_the_review_app(tmp_path, monkeypatch, flag):
    _vault(tmp_path, monkeypatch)
    run, _ns = _run_main(monkeypatch, OKENGINE_WRITE_TRANSPORT="http",
                         OKENGINE_WRITE_TOKEN="t", OKENGINE_WRITE_REVIEW_ONLY=flag)

    paths = {getattr(route, "path", "") for route in run.call_args.args[0].app.routes}
    assert "/review/resolve" not in paths


def test_networked_transport_refuses_to_start_without_a_token(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="requires OKENGINE_WRITE_TOKEN"):
        _run_main(monkeypatch, OKENGINE_WRITE_TRANSPORT="http")


def test_networked_transport_refuses_the_public_default_token_off_loopback(tmp_path,
                                                                          monkeypatch):
    _vault(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="built-in DEFAULT token"):
        _run_main(monkeypatch, OKENGINE_WRITE_TRANSPORT="http",
                  OKENGINE_WRITE_TOKEN="okengine-local", OKENGINE_WRITE_HOST="0.0.0.0")


def test_the_entry_point_refuses_to_serve_without_a_pack_principal(tmp_path, monkeypatch):
    """okengine#1027: checked before any transport, so neither stdio nor HTTP serves nobody."""
    _vault(tmp_path, monkeypatch)
    (tmp_path / "pack.yaml").unlink()
    from mcp.server.fastmcp import FastMCP

    with mock.patch.object(FastMCP, "run", autospec=True) as stdio, \
            mock.patch.object(uvicorn, "run", autospec=True) as http, \
            pytest.raises(SystemExit, match="refusing to start: no pack principal"):
        runpy.run_path(str(WRITE), run_name="__main__")
    stdio.assert_not_called()
    http.assert_not_called()


@pytest.mark.parametrize(("env_pack", "starts"), [("okpack-env", True), (None, False)])
def test_a_blank_wiki_path_judges_the_default_vault_not_the_working_directory(
        tmp_path, monkeypatch, env_pack, starts):
    """okengine#1027 entry rule: `WIKI_PATH or VAULT`. A set-but-blank WIKI_PATH falls back to the
    default vault, exactly as the write path itself does (state.VAULT), never to Path("") (the
    working directory). The working directory here names a DIFFERENT pack, so judging it would
    refuse a start that is allowed (env pin, default vault without pack.yaml), or allow one that
    is refused (no principal anywhere the server writes)."""
    _vault(tmp_path, monkeypatch)
    assert not (VAULT_DEFAULT / "pack.yaml").exists(), "this test needs the default vault absent"
    monkeypatch.setenv("WIKI_PATH", "")
    if env_pack:
        monkeypatch.setenv("OKENGINE_PACK", env_pack)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "pack.yaml").write_text("name: okpack-cwd\n")
    monkeypatch.chdir(cwd)
    from mcp.server.fastmcp import FastMCP

    with mock.patch.object(FastMCP, "run", autospec=True) as stdio:
        if starts:
            runpy.run_path(str(WRITE), run_name="__main__")
        else:
            with pytest.raises(SystemExit, match="refusing to start: no pack principal"):
                runpy.run_path(str(WRITE), run_name="__main__")
    assert stdio.called is starts


@pytest.mark.parametrize("transport", [None, "stdio", "sse", "HTTP"])
def test_any_other_transport_runs_the_stdio_server(tmp_path, monkeypatch, transport):
    _vault(tmp_path, monkeypatch)
    if transport is not None:
        monkeypatch.setenv("OKENGINE_WRITE_TRANSPORT", transport)
    from mcp.server.fastmcp import FastMCP

    with mock.patch.object(FastMCP, "run", autospec=True) as stdio, \
            mock.patch.object(uvicorn, "run", autospec=True) as http:
        runpy.run_path(str(WRITE), run_name="__main__")

    http.assert_not_called()
    stdio.assert_called_once()
    assert stdio.call_args.kwargs == {"transport": "stdio"}


@pytest.mark.parametrize("run_name", ["write_server", "__mp_main__", "__init__", "WriteServer"])
def test_importing_the_module_starts_no_server(tmp_path, monkeypatch, run_name):
    _vault(tmp_path, monkeypatch)
    monkeypatch.setenv("OKENGINE_WRITE_TRANSPORT", "http")
    monkeypatch.setenv("OKENGINE_WRITE_TOKEN", "t")
    from mcp.server.fastmcp import FastMCP

    with mock.patch.object(FastMCP, "run", autospec=True) as stdio, \
            mock.patch.object(uvicorn, "run", autospec=True) as http:
        runpy.run_path(str(WRITE), run_name=run_name)

    http.assert_not_called()
    stdio.assert_not_called()
