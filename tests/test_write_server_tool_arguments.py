"""okengine#879: a governed write tool refuses an argument it does not declare, by name.

Both MCP SDKs build an argument model that silently DROPS unknown keys. A model that sent a brief
as `content` instead of `body` had the text discarded; the write was refused as an empty body four
times, the message never named `body`, and the run ended "Wrote briefings/..." having written
nothing. The published schema is now closed (the gateway validates calls against it) and the
argument model forbids extras, so the call fails naming the key instead."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[1]
WRITE = REPO / "okengine-mcp" / "write_server.py"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "briefings").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\ntypes: {briefing: {required: [type]}}\nstrict_types: false\n",
        encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    monkeypatch.delenv("OKENGINE_WRITE_TOOLS", raising=False)
    name = f"ws_tool_args_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(name, WRITE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def _schema(tool):
    return getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)


def test_every_published_write_tool_schema_is_closed(ws):
    m, _root = ws
    tools = asyncio.run(m.mcp.list_tools())
    assert tools, "the write server published no tools"
    open_tools = [t.name for t in tools if _schema(t).get("additionalProperties") is not False]
    assert not open_tools, f"write tools accept undeclared arguments: {open_tools}"


def test_an_undeclared_argument_is_refused_by_name_and_writes_nothing(ws):
    m, root = ws
    args = {"path": "briefings/2026-09-25", "frontmatter_yaml": "type: briefing\ntitle: Brief",
            "content": "# Daily brief\n\n" + "text " * 40}
    try:
        result = asyncio.run(m.mcp.call_tool("create_entity", args))
        text = str(result)
    except Exception as exc:  # the SDK may raise its ToolError instead of returning an error result
        text = f"{type(exc).__name__}: {exc}"
    assert "content" in text and ("extra" in text.lower() or "not permitted" in text.lower()
                                  or "unexpected" in text.lower()), text
    assert not (root / "wiki" / "briefings" / "2026-09-25.md").exists()


def test_a_well_formed_call_still_writes(ws):
    m, root = ws
    args = {"path": "briefings/2026-09-25", "frontmatter_yaml": "type: briefing\ntitle: Brief",
            "body": "# Daily brief\n\n" + "text " * 40}
    asyncio.run(m.mcp.call_tool("create_entity", args))
    assert (root / "wiki" / "briefings" / "2026-09-25.md").is_file()


def test_an_unrecognized_sdk_tool_layout_fails_startup_naming_the_tool():
    from okengine.write_services import transport

    class _Server:  # an SDK whose registry no longer lives at _tool_manager._tools
        def tool(self):
            return None

    with pytest.raises(RuntimeError, match="'create_entity'.*tool layout changed"):
        transport._close_tool_arguments(_Server().tool, "create_entity")


@pytest.mark.parametrize("parameters, has_arg_model", [({"type": "object"}, False), (None, True)],
                         ids=["schema-without-arg-model", "arg-model-without-schema"])
def test_a_half_recognized_sdk_tool_layout_also_fails_startup(parameters, has_arg_model):
    """Either piece missing means the tool cannot be fully closed; neither may be skipped."""
    from types import SimpleNamespace

    from okengine.write_services import transport

    arg_model = SimpleNamespace(model_config={}, model_rebuild=lambda force: None)
    tool = SimpleNamespace(parameters=parameters,
                           fn_metadata=SimpleNamespace(arg_model=arg_model) if has_arg_model else None)

    class _Server:
        def tool(self):
            return None

    server = _Server()
    server._tool_manager = SimpleNamespace(_tools={"create_entity": tool})
    with pytest.raises(RuntimeError, match="'create_entity'.*tool layout changed"):
        transport._close_tool_arguments(server.tool, "create_entity")
    assert arg_model.model_config == {}, "a half-recognized layout must not be partially closed"
