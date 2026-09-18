"""Versioned read-plane promises, validated without starting the full stack."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


pytestmark = pytest.mark.contract

REPO = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO / "config" / "testing" / "service-contracts-v1.json"


@contextmanager
def _module_path(path: Path):
    old_path = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        yield
    finally:
        sys.path[:] = old_path


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with _module_path(path):
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def contract():
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert value["schema_version"] == 1
    return value


@pytest.mark.parametrize("surface", ["reader", "cockpit"])
def test_http_openapi_contains_every_versioned_route(contract, surface, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("OKENGINE_TRUST", "public")
    monkeypatch.setenv("OKENGINE_BIND", "127.0.0.1")
    item = contract["http"][surface]
    module = _load(REPO / item["module"], f"contract_{surface}")
    paths = module.app.openapi()["paths"]
    for route, methods in item["routes"].items():
        assert route in paths, f"{surface} removed versioned route {route}"
        assert set(methods) <= set(paths[route]), (
            f"{surface} {route} methods changed: {sorted(paths[route])}")


def test_mcp_tool_names_and_required_arguments_match_versioned_contract(
        contract, tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_INDEX_REFRESH_HOURS", "0")
    item = contract["mcp"]
    module = _load(REPO / item["module"], "contract_mcp")
    published = {tool.name: tool for tool in asyncio.run(module.mcp.list_tools())}
    assert set(published) == set(item["tools"])
    for name, required in item["tools"].items():
        schema = published[name].inputSchema
        assert schema.get("type") == "object"
        assert sorted(schema.get("required", [])) == sorted(required)


def test_contract_contains_only_repository_modules(contract):
    modules = [item["module"] for item in contract["http"].values()]
    modules.append(contract["mcp"]["module"])
    assert all((REPO / module).is_file() for module in modules)
    assert not any(Path(module).is_absolute() for module in modules)
