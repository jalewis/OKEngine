"""okengine-mcp must disable the MCP SDK's loopback-only host allowlist, or the bridge
service-name Host (okengine-mcp:8730) gets 421'd and the read MCP silently dies (okengine#138)."""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "okengine-mcp" / "server.py").read_text()


def test_dns_rebinding_protection_disabled_for_bridge():
    assert "from mcp.server.transport_security import TransportSecuritySettings" in SRC
    assert "transport_security=TransportSecuritySettings(" in SRC
    assert "enable_dns_rebinding_protection=False" in SRC
