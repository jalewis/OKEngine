"""The local Qwen serving contract is Responses-only for active inference."""
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
RETIRED = ("chat" + "/completions", "/api/" + "generate")


def test_qwen_runtime_patch_selects_responses_transport():
    patch = (REPO / "patches" / "17-qwen-coder-responses-api.patch").read_text()
    assert 'normalized_provider == "custom"' in patch
    assert 'startswith(("qwen3-coder", "qwen-coder"))' in patch
    assert "codex_responses" in patch
    assert '"type": "message"' in patch
    assert '"type": "output_text"' in patch


def test_qwen_runtime_patch_types_all_role_based_message_paths():
    patch = (REPO / "patches" / "17-qwen-coder-responses-api.patch").read_text()
    preflight = patch[patch.index("def _preflight_codex_input_items"):]
    assert preflight.count('"type": "message"') >= 5
    assert '+                    "role": role,' in preflight
    assert '{"user", "assistant", "system", "developer"}' in preflight
    assert '"output_text" if role == "assistant" else "input_text"' in preflight
    assert '"content": [{"type": "input_text", "text": content}]' in preflight
    assert 'if role == "assistant":' in preflight


def test_every_codex_stream_call_preflights_at_the_wire_boundary():
    """Summary/retry callers bypassed the main-loop preflight in #500."""
    patch = (REPO / "patches" / "17-qwen-coder-responses-api.patch").read_text()
    runtime = patch[patch.index("def run_codex_stream"):]
    preflight = runtime.index("preflight_kwargs(api_kwargs)")
    later_stream_hunk = runtime.index("@@ -861")
    assert preflight < later_stream_hunk
    assert "max-iteration summary path" in runtime


def test_responses_400_diagnostic_is_shape_only():
    patch = (REPO / "patches" / "17-qwen-coder-responses-api.patch").read_text()
    runtime = patch[patch.index("def run_codex_stream"):]
    assert "Codex Responses HTTP 400: input_items=%d" in runtime
    assert "missing_type_indexes=%s" in runtime
    start = runtime.index("Codex Responses HTTP 400:")
    diagnostic = runtime[start:runtime.index("agent._client_log_context()", start)]
    assert "content" not in diagnostic


def test_direct_llm_client_uses_responses_contract():
    source = (REPO / "scripts" / "cron" / "llm_lib.py").read_text()
    assert 'f"{url}/responses"' in source
    assert all(endpoint not in source for endpoint in RETIRED)


def test_direct_llm_client_copies_are_identical():
    canonical = (REPO / "scripts" / "cron" / "llm_lib.py").read_text()
    for relative in (
        "extensions/okengine.viz/llm_lib.py",
        "extensions/okengine.relevance-gate/llm_lib.py",
    ):
        assert (REPO / relative).read_text() == canonical


def test_no_retired_endpoint_literals_in_active_python_clients():
    offenders = []
    for root_name in ("scripts", "extensions", "tools", "okengine-mcp"):
        for path in (REPO / root_name).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for endpoint in RETIRED:
                if endpoint in text:
                    offenders.append(f"{path.relative_to(REPO)}: {endpoint}")
    assert not offenders, "retired endpoint callers remain:\n" + "\n".join(offenders)


def test_write_telemetry_is_captured_before_context_compression():
    patch = (REPO / "patches" / "14-cron-executed-writes.patch").read_text()
    execute = patch.index("agent._execute_tool_calls")
    capture_start = patch.index("_okengine_tool_result_start = len(messages)")
    capture_result = patch.index("agent._okengine_executed_writes = _okengine_writes")
    assert capture_start < execute < capture_result
    assert 'getattr(agent, "_okengine_executed_writes"' in patch


def test_write_telemetry_reads_responses_structured_tool_content():
    """Qwen Responses emits MCP results as text parts, not always a string."""
    patch = (REPO / "patches" / "14-cron-executed-writes.patch").read_text()
    assert patch.count("isinstance(_okengine_content, list)") == 1
    assert patch.count("isinstance(_content, list)") == 1
    assert patch.count('_part.get("text")') >= 2
    assert patch.count("created|updated|patched|tombstoned") == 2
