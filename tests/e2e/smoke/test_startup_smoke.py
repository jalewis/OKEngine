"""Minimal release smoke: startup, auth, and one critical read path per service."""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


pytestmark = pytest.mark.smoke

READER = os.environ.get("SMOKE_READER_URL", "http://127.0.0.1:9880")
COCKPIT = os.environ.get("SMOKE_COCKPIT_URL", "http://127.0.0.1:9881")
MCP = os.environ.get("SMOKE_MCP_URL", "http://127.0.0.1:8880")
TOKEN = os.environ.get("SMOKE_MCP_TOKEN", "okengine-local")


def _request(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture(autouse=True)
def _release_stack_required():
    try:
        status, _ = _request(f"{READER}/healthz")
        if status != 200:
            raise RuntimeError(f"reader returned HTTP {status}")
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
        if os.environ.get("SMOKE_RELEASE") == "1":
            pytest.fail(f"release smoke stack unavailable: {exc}")
        pytest.skip(f"release smoke stack is not running: {exc}")


def test_release_stack_starts_and_serves_critical_read_paths():
    assert _request(f"{READER}/healthz")[0] == 200
    status, body = _request(f"{READER}/api/page?path=entities/a/apt-smoke")
    assert status == 200 and "SMOKE_BODY_SENTINEL" in json.loads(body)["html"]
    status, body = _request(f"{COCKPIT}/api/dashboards")
    assert status == 200 and json.loads(body)["groups"]
    assert _request(f"{MCP}/mcp")[0] == 401


def test_mcp_authentication_discovery_and_nonroot_qmd_are_operational():
    async def discover() -> tuple[str, set[str], bool]:
        async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15) as client:
            async with streamable_http_client(
                    f"{MCP}/mcp", http_client=client) as (read, write, _session_id):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("search", {"query": "APT Smoke", "limit": 1})
                    return (initialized.serverInfo.name, {tool.name for tool in tools.tools},
                            bool(result.isError))

    name, tools, search_error = asyncio.run(discover())
    assert name == "okengine"
    assert {"search", "get_page", "list_pages"} <= tools
    assert not search_error, "non-root MCP must be able to initialize/use its writable qmd mount"
