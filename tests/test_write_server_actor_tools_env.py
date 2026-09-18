"""okengine#664: the per-actor MCP tool allowlist is pack config, not an engine literal.

`write_server._actor_tools` carried a PRIVATE deployment's cron name ("cron:okpack-…-vendor-
frontmatter-backfill") in the baked write path -- the publish guard aborts on it, and an engine
table is the wrong home for a pack lane's surface anyway. A pack lane now declares
`write_tools: [...]` on its cron def; ensure-runtime turns that into `OKENGINE_WRITE_TOOLS` on the
lane's server-bound writer, and the server prunes to exactly that set. The engine table keeps only
engine lanes."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[1]
WRITE = REPO / "okengine-mcp" / "write_server.py"


def _load(name: str, tmp_path: Path, monkeypatch, actor: str, tools: str | None):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    (tmp_path / "wiki").mkdir(exist_ok=True)
    monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    if tools is None:
        monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    else:
        monkeypatch.setenv("OKENGINE_WRITE_TOOLS", tools)
    spec = importlib.util.spec_from_file_location(name, WRITE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tools(m) -> set[str]:
    return {t.name for t in asyncio.run(m.mcp.list_tools())}


def test_pack_lane_surface_comes_from_the_env_allowlist(tmp_path, monkeypatch):
    m = _load("ws_tools_env_a", tmp_path, monkeypatch,
              "cron:some-pack-frontmatter-backfill", "update_entity, append_to_section")
    assert _tools(m) == {"update_entity", "append_to_section"}


def test_unknown_names_in_the_allowlist_are_ignored_not_minted(tmp_path, monkeypatch):
    m = _load("ws_tools_env_b", tmp_path, monkeypatch,
              "cron:some-pack-lane", "update_entity,no_such_tool")
    assert _tools(m) == {"update_entity"}


def test_pack_lane_without_an_allowlist_keeps_the_full_policy_guarded_surface(tmp_path, monkeypatch):
    m = _load("ws_tools_env_c", tmp_path, monkeypatch, "cron:some-pack-lane", None)
    assert {"create_entity", "update_entity", "converge_entity"} <= _tools(m)


def test_env_allowlist_overrides_the_engine_table_for_engine_lanes(tmp_path, monkeypatch):
    """A pack that narrows an engine lane further (or widens it deliberately) is the deployment's
    call; the env is the single runtime source once set."""
    m = _load("ws_tools_env_d", tmp_path, monkeypatch, "cron:page-quality-enrich", "update_entity")
    assert _tools(m) == {"update_entity"}


def test_engine_table_carries_no_pack_or_deployment_names(tmp_path, monkeypatch):
    """The detector for okengine#664: an engine literal may only name engine lanes."""
    m = _load("ws_tools_env_e", tmp_path, monkeypatch, "cron:entity-backfill", None)
    for actor in m._ACTOR_TOOLS:
        assert actor.startswith("cron:"), actor
        assert "okpack-" not in actor, actor
