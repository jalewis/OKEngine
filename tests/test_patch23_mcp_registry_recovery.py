"""Behavioral contracts for carried MCP reconnect recovery (okengine#608)."""
from __future__ import annotations

import logging
import re
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


PATCH = Path(__file__).resolve().parents[1] / "patches/23-mcp-registry-recovery.patch"


def _added_function(name: str) -> str:
    lines = PATCH.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines)
                 if line.startswith(f"+def {name}(") )
    body = []
    for line in lines[start:]:
        if not line.startswith("+") or line.startswith("+++"):
            break
        body.append(line[1:])
    return "\n".join(body)


def test_registry_loss_is_durable_and_specific_to_mcp():
    source = _added_function("_record_mcp_registry_loss")
    namespace = {
        "Any": object,
        "_multimodal_text_summary": str,
        "logger": logging.getLogger(__name__),
    }
    exec(compile(source, "<patch23-record>", "exec"), namespace)  # noqa: S102
    record = namespace["_record_mcp_registry_loss"]
    interrupts = []
    agent = SimpleNamespace(interrupt=interrupts.append)

    record(agent, "read_file", '{"error":"Unknown tool: read_file"}')
    assert not hasattr(agent, "_okengine_mcp_registry_losses")
    record(agent, "mcp__okengine__get_page", '{"error":"transport down"}')
    assert not hasattr(agent, "_okengine_mcp_registry_losses")
    missing = '{"error":"Unknown tool: mcp__okengine__get_page"}'
    record(agent, "mcp__okengine__get_page", missing)
    record(agent, "mcp__okengine__get_page", missing)
    assert agent._okengine_mcp_registry_losses == ["mcp__okengine__get_page"]
    assert interrupts == ["MCP registry integrity lost: mcp__okengine__get_page"]


def test_recovered_transport_republishes_the_missing_tool(monkeypatch):
    source = _added_function("recover_missing_mcp_tool")

    class FakeRegistry:
        entry = None

        def get_entry(self, _name):
            return self.entry

    registry = FakeRegistry()
    registry_module = ModuleType("tools.registry")
    registry_module.registry = registry
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)

    class Server:
        session = object()
        _tools = [object()]

        def _register_discovered_tools_if_needed(self):
            registry.entry = object()

    server = Server()
    namespace = {
        "MCP_TOOL_NAME_PREFIX": "mcp__",
        "_MCP_NAME_DELIM": "__",
        "_lock": threading.Lock(),
        "_servers": {"okengine": server},
        "sanitize_mcp_name_component": lambda value: re.sub(r"[^A-Za-z0-9_]", "_", value),
        "_signal_reconnect": lambda _server: False,
        "logger": logging.getLogger(__name__),
        "time": time,
    }
    exec(compile(source, "<patch23-recover>", "exec"), namespace)  # noqa: S102
    assert namespace["recover_missing_mcp_tool"]("mcp__okengine__get_page", 0) is True
    assert registry.entry is not None


def test_unrecovered_registry_loss_cannot_report_a_clean_cron_completion():
    source = _added_function("_enforce_mcp_registry_integrity")
    namespace = {}
    exec(compile(source, "<patch23-enforce>", "exec"), namespace)  # noqa: S102
    enforce = namespace["_enforce_mcp_registry_integrity"]
    job = {}

    enforce(SimpleNamespace(), job)
    assert job["_okengine_mcp_registry_losses"] == []

    agent = SimpleNamespace(_okengine_mcp_registry_losses=[
        "mcp__okengine__search", "mcp__okengine__search",
    ])
    with pytest.raises(RuntimeError, match="MCP tool registry loss after bounded recovery"):
        enforce(agent, job)
    assert job["_okengine_mcp_registry_losses"] == ["mcp__okengine__search"]


def test_patch_recovers_before_unknown_and_enforces_integrity_before_receipts():
    body = PATCH.read_text(encoding="utf-8")
    recovery_at = body.index("recover_missing_mcp_tool(name)")
    unknown_at = body.index('Unknown tool: {name}', recovery_at)
    assert recovery_at < unknown_at
    integrity_at = body.index("_enforce_mcp_registry_integrity(agent, job)")
    receipt_at = body.index("_write_result = re.compile", integrity_at)
    assert integrity_at < receipt_at
    assert body.count("_record_mcp_registry_loss(agent, function_name, function_result)") == 2
    assert 'agent.interrupt(f"MCP registry integrity lost: {function_name}")' in body
