"""okengine#661: the model can approve its own review flags.

`resolve_review` / `assign_review` were registered on the default `okengine-write` MCP server --
the one 12 of 14 agent crons and api_server chat carry -- with a free-text `reviewer`, and the
stdio caller is `admin`, so `_capability_reject` returned None. A lane prompted to "clear the
backlog" could empty `_review-queue.md` with human-looking approvals. Human decisions reach
`_resolve_review` through the review-only HTTP sidecar (`OKENGINE_WRITE_REVIEW_ONLY=1`) and the
`framework review` CLI; neither is an MCP tool. So the MCP surface must not carry them at all."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[1]
WRITE = REPO / "okengine-mcp" / "write_server.py"
HUMAN_DECISION_TOOLS = {"resolve_review", "assign_review"}


def _load(name: str, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    (tmp_path / "wiki").mkdir(exist_ok=True)
    spec = importlib.util.spec_from_file_location(name, WRITE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tools(module) -> set[str]:
    return {tool.name for tool in asyncio.run(module.mcp.list_tools())}


def test_default_server_does_not_expose_human_review_decisions(tmp_path, monkeypatch):
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    tools = _tools(_load("ws_review_surface_default", tmp_path, monkeypatch))
    assert not (tools & HUMAN_DECISION_TOOLS), sorted(tools & HUMAN_DECISION_TOOLS)
    # the machine-evidence hook (cannot clear human-required state) and ordinary writes stay
    assert {"record_machine_review", "create_entity", "flag_for_review"} <= tools


@pytest.mark.parametrize("actor", [
    "cron:page-quality-enrich",            # pruned actor list
    "cron:okengine.predictions:grade",     # an actor with NO pruning entry -> full surface
])
def test_job_actor_servers_do_not_expose_human_review_decisions(tmp_path, monkeypatch, actor):
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    tools = _tools(_load("ws_review_surface_" + actor.replace(":", "_").replace(".", "_"),
                         tmp_path, monkeypatch))
    assert not (tools & HUMAN_DECISION_TOOLS), sorted(tools & HUMAN_DECISION_TOOLS)


def test_review_sidecar_http_app_still_serves_the_human_decisions(tmp_path, monkeypatch):
    """The human path is unchanged: the review-only sidecar keeps /review/resolve + /review/assign."""
    pytest.importorskip("fastapi")
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    m = _load("ws_review_surface_http", tmp_path, monkeypatch)
    routes = {getattr(r, "path", "") for r in m._review_http_app().routes}
    assert {"/review/resolve", "/review/assign", "/review/machine"} <= routes
