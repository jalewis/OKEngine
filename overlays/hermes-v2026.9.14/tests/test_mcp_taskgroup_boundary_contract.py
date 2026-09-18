"""A pre-ready MCP TaskGroup failure must not hot-loop as a live reconnect."""

import asyncio

import pytest

from tools.mcp_tool import MCPServerTask


class _FailingTransport:
    """Disposable external transport that fails before a session can exist."""

    async def __aenter__(self):
        raise BaseExceptionGroup("pre-ready transport failure", [ConnectionError("handshake")])

    async def __aexit__(self, *_exc_info):
        raise AssertionError("the unentered transport must not be torn down")


def test_pre_ready_transport_group_reraises_for_backoff_not_immediate_reconnect():
    task = MCPServerTask("disposable-pre-ready")
    with pytest.raises(BaseExceptionGroup, match="pre-ready transport failure"):
        asyncio.run(task._serve_transport(_FailingTransport(), "HTTP", 1.0))
    assert not task._ready.is_set(), "no MCP session was ever published"
