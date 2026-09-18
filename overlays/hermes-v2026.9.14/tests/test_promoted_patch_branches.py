"""Behavioral edge contracts for OKEngine's promoted Hermes patch set."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest


# The target contract image installs these optional entrypoint dependencies.
# Keep this focused module importable in the engine's smaller developer venv too.
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *args, **kwargs: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _routing_agent(provider: str, model: str, base_url: str) -> SimpleNamespace:
    from run_agent import AIAgent
    from utils import base_url_hostname

    return SimpleNamespace(
        provider=provider,
        model=model,
        _base_url_hostname=base_url_hostname(base_url),
        _base_url_lower=base_url.lower(),
        _provider_model_requires_responses_api=AIAgent._provider_model_requires_responses_api,
    )


@pytest.mark.parametrize(
    ("provider", "model", "base_url", "provider_name", "requested", "expected_provider", "expected_mode"),
    [
        ("openai-codex", "other", "https://example.test/v1", "openai-codex", None,
         "openai-codex", "codex_responses"),
        ("custom", "other", "https://chatgpt.com/backend-api/codex", None, None,
         "openai-codex", "codex_responses"),
        ("custom", "other", "https://api.x.ai/v1", None, None,
         "xai", "codex_responses"),
        ("custom", "other", "https://api.anthropic.com/v1", None, None,
         "anthropic", "anthropic_messages"),
        ("custom", "other", "https://example.test/anthropic", "custom", None,
         "custom", "anthropic_messages"),
        ("bedrock", "other", "https://example.test/v1", "bedrock", None,
         "bedrock", "bedrock_converse"),
        ("nous", "anthropic/claude", "https://example.test/v1", "nous", None,
         "nous", "chat_completions"),
        ("custom", "other", "https://example.test/v1", "custom", "codex_app_server",
         "custom", "codex_app_server"),
    ],
)
def test_api_mode_routing_ladder(
    provider, model, base_url, provider_name, requested, expected_provider, expected_mode,
):
    from agent.agent_init import _resolve_api_mode

    agent = _routing_agent(provider, model, base_url)
    _resolve_api_mode(agent, requested, provider_name, base_url)
    assert (agent.provider, agent.api_mode) == (expected_provider, expected_mode)


def test_api_mode_host_mandate_failure_falls_back_to_chat(monkeypatch):
    from agent.agent_init import _resolve_api_mode
    import hermes_cli.providers as providers

    agent = _routing_agent("custom", "other", "https://example.test/v1")
    monkeypatch.setattr(providers, "host_mandated_api_mode", lambda _url: 1 / 0)
    _resolve_api_mode(agent, None, "custom", "https://example.test/v1")
    assert agent.api_mode == "chat_completions"


def test_actual_route_forces_chat_before_explicit_mode(monkeypatch):
    from agent.agent_init import _resolve_api_mode
    import hermes_cli.providers as providers

    agent = _routing_agent("actual", "gpt-5.4", "https://actual.test/v1")
    monkeypatch.setattr(providers, "is_actual_route", lambda *_args: True)
    _resolve_api_mode(agent, "codex_responses", "actual", "https://actual.test/v1")
    assert agent.api_mode == "chat_completions"


def _preflight_ctx():
    from agent.codex_responses_adapter import _PreflightCtx

    return _PreflightCtx(lambda text: f"clean:{text}", False, False, set())


@pytest.mark.parametrize("role", ["invalid", None])
def test_typed_message_rejects_unsupported_role(role):
    from agent.codex_responses_adapter import _preflight_message

    with pytest.raises(ValueError, match="unsupported role"):
        _preflight_message({"type": "message", "role": role, "content": "x"}, 2, _preflight_ctx())


@pytest.mark.parametrize("content", [7, {}, None])
def test_typed_message_rejects_non_list_content(content):
    from agent.codex_responses_adapter import _preflight_message

    with pytest.raises(ValueError, match="string or list"):
        _preflight_message({"type": "message", "role": "user", "content": content}, 1, _preflight_ctx())


def test_typed_message_normalizes_text_image_and_id():
    from agent.codex_responses_adapter import _preflight_message

    actual = _preflight_message({
        "type": "message", "role": "user", "id": "  msg-1 ",
        "content": ["plain", {"type": "output_text", "text": "typed"},
                    {"type": "input_image", "image_url": "https://image.test/a.png"}],
    }, 0, _preflight_ctx())
    assert actual == {
        "type": "message", "role": "user", "id": "msg-1",
        "content": [
            {"type": "input_text", "text": "clean:plain"},
            {"type": "input_text", "text": "clean:typed"},
            {"type": "input_image", "image_url": "https://image.test/a.png"},
        ],
    }


@pytest.mark.parametrize(
    ("content", "message"),
    [([object()], "must be an object or string"),
     ([{"type": "audio"}], "unsupported type"),
     ([], "at least one part")],
)
def test_typed_message_rejects_invalid_parts(content, message):
    from agent.codex_responses_adapter import _preflight_message

    with pytest.raises(ValueError, match=message):
        _preflight_message({"type": "message", "role": "user", "content": content}, 0, _preflight_ctx())


def test_typed_assistant_message_uses_assistant_shape():
    from agent.codex_responses_adapter import _preflight_message

    actual = _preflight_message(
        {"type": "message", "role": "assistant", "content": "answer"}, 0, _preflight_ctx())
    assert actual["role"] == "assistant"
    assert actual["content"] == [{"type": "output_text", "text": "clean:answer"}]


def test_legacy_role_message_rejects_unknown_role():
    from agent.codex_responses_adapter import _preflight_role_message

    with pytest.raises(ValueError, match="unsupported item shape"):
        _preflight_role_message({"role": "tool", "content": "x"}, 3, _preflight_ctx())


def test_transport_failure_sink_exception_is_contained(caplog):
    from tools.mcp_transport_failure_telemetry import (
        capture_mcp_transport_failures, record_mcp_transport_failure)

    def broken_sink(_name: str) -> None:
        raise RuntimeError("disposable sink failure")

    with capture_mcp_transport_failures(broken_sink):
        record_mcp_transport_failure("okengine-read")
    assert "MCP transport failure sink failed" in caplog.text


def test_custom_copilot_probe_failure_uses_generic_model_rule(monkeypatch):
    from run_agent import AIAgent
    import hermes_cli.models as models

    monkeypatch.setattr(models, "_should_use_copilot_responses_api", lambda _model: 1 / 0)
    assert AIAgent._provider_model_requires_responses_api("gpt-5.4", provider="copilot") is True


def test_nous_never_forces_responses():
    from run_agent import AIAgent

    assert AIAgent._provider_model_requires_responses_api("gpt-5.4", provider="nous") is False


def test_api_default_requires_provider(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter, _ProviderAuthResolutionError

    monkeypatch.setenv("API_SERVER_INFERENCE_MODEL", "interactive")
    monkeypatch.delenv("API_SERVER_INFERENCE_PROVIDER", raising=False)
    with pytest.raises(_ProviderAuthResolutionError, match="resolvable API provider"):
        APIServerAdapter._okengine_api_inference_default({}, "bulk")


def test_api_default_rejects_malformed_resolution(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter, _ProviderAuthResolutionError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("API_SERVER_INFERENCE_MODEL", "interactive")
    monkeypatch.setenv("API_SERVER_INFERENCE_PROVIDER", "custom")
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_runtime_agent_kwargs=lambda row: row))
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        create_autospec(resolve_runtime_provider, return_value={"provider": ""}),
    )
    with pytest.raises(_ProviderAuthResolutionError, match="no usable runtime"):
        APIServerAdapter._okengine_api_inference_default({}, "bulk")


def test_api_default_rejects_unexpected_requested_route(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter, _ProviderAuthResolutionError
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("API_SERVER_INFERENCE_MODEL", "interactive")
    monkeypatch.setenv("API_SERVER_INFERENCE_PROVIDER", "deepseek")
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(_runtime_agent_kwargs=lambda row: row))
    monkeypatch.setattr("hermes_cli.runtime_provider.is_routable_provider", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        create_autospec(resolve_runtime_provider, return_value={
            "provider": "deepseek", "requested_provider": "other", "capabilities": None,
        }),
    )
    with pytest.raises(_ProviderAuthResolutionError, match="unexpected route"):
        APIServerAdapter._okengine_api_inference_default({}, "bulk")


def test_api_default_backfills_requested_provider(monkeypatch):
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("API_SERVER_INFERENCE_MODEL", "interactive")
    monkeypatch.setenv("API_SERVER_INFERENCE_PROVIDER", "deepseek")
    monkeypatch.setitem(sys.modules, "gateway.run", SimpleNamespace(
        _runtime_agent_kwargs=lambda row: {"provider": row["provider"]}))
    monkeypatch.setattr("hermes_cli.runtime_provider.is_routable_provider", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        create_autospec(resolve_runtime_provider, return_value={
            "provider": "deepseek", "capabilities": {"vision": True, 1: True, "bad": "yes"},
        }),
    )
    runtime = {}
    assert APIServerAdapter._okengine_api_inference_default(runtime, "bulk") == "interactive"
    assert runtime == {
        "provider": "deepseek", "requested_provider": "deepseek",
        "capabilities": {"vision": True},
    }


def test_registry_loss_initializes_state_and_contains_interrupt_failure(monkeypatch):
    import agent.conversation_loop as loop

    callback = None

    class Capture:
        def __init__(self, sink):
            nonlocal callback
            callback = sink

        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    agent = SimpleNamespace(
        valid_tool_names={"mcp__okengine_read__get_page"},
        interrupt=lambda _message: (_ for _ in ()).throw(RuntimeError("interrupt failed")),
    )
    monkeypatch.setattr("tools.mcp_registry_loss_telemetry.capture_mcp_registry_misses", Capture)
    monkeypatch.setattr(loop, "_run_conversation_turn", lambda *_args, **_kwargs: callback(
        "mcp__okengine_read__get_page") or {"final_response": "done"})
    monkeypatch.setattr("agent.turn_context.export_current_turn_boundary",
                        lambda _agent, result, _message: result)
    assert loop.run_conversation(agent, "prompt")["final_response"] == "done"
    assert agent._okengine_mcp_registry_losses == ["mcp__okengine_read__get_page"]


def test_registry_loss_without_interrupt_still_records(monkeypatch):
    import agent.conversation_loop as loop

    callback = None

    class Capture:
        def __init__(self, sink):
            nonlocal callback
            callback = sink

        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    agent = SimpleNamespace(valid_tool_names={"mcp__okengine_read__get_page"}, interrupt=None)
    monkeypatch.setattr("tools.mcp_registry_loss_telemetry.capture_mcp_registry_misses", Capture)
    monkeypatch.setattr(loop, "_run_conversation_turn", lambda *_args, **_kwargs: callback(
        "mcp__okengine_read__get_page") or {"final_response": "done"})
    monkeypatch.setattr("agent.turn_context.export_current_turn_boundary",
                        lambda _agent, result, _message: result)
    loop.run_conversation(agent, "prompt")
    assert agent._okengine_mcp_registry_losses == ["mcp__okengine_read__get_page"]


def test_positive_disk_probe_result_populates_memory_cache(monkeypatch):
    from agent import model_metadata as metadata

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace())
    metadata._endpoint_probe_path_cache.clear()
    monkeypatch.setattr(metadata, "_endpoint_blackholed", lambda _url: False)
    monkeypatch.setattr(metadata, "_local_probe_disk_get", lambda _kind, _key: "ollama")
    assert metadata.detect_local_server_type("http://127.0.0.1:11434/v1", "key") == "ollama"
    assert next(iter(metadata._endpoint_probe_path_cache.values()))[0] == "ollama"


def test_llamacpp_bare_props_without_known_alias_returns_cleanly(monkeypatch):
    from agent import model_metadata as metadata

    response = SimpleNamespace(
        ok=True,
        json=lambda: {"model_alias": "unknown", "default_generation_settings": {"n_ctx": 4096}},
    )
    monkeypatch.setattr(metadata, "_ensure_requests", lambda: None)
    monkeypatch.setattr(metadata, "requests", SimpleNamespace(
        get=lambda *_args, **_kwargs: response))
    cache = {"known": {"context_length": 0}}
    metadata._apply_llamacpp_props(cache, "http://127.0.0.1:8080/v1", {}, True)
    assert cache["known"]["context_length"] == 0


def test_output_cap_returns_deferred_compression_verdict():
    from agent.turn_overflow import _Recovery, _clamp_output_cap
    from agent.turn_retry_state import TurnRetryState

    agent = SimpleNamespace(
        tools=[], compression_enabled=True, log_prefix="", _ephemeral_max_output_tokens=None,
        _buffer_vprint=lambda _message: None,
    )
    state = _Recovery(
        messages=[], active_system_prompt="", conversation_history=[], approx_tokens=0,
        compression_attempts=0, agent=agent, api_messages=[], system_message="",
        effective_task_id=None, api_call_count=0, max_compression_attempts=3,
    )
    state.request_tokens = lambda: 100
    deferred = state.done("return", {"partial": True})
    state.action, state.result = "fallthrough", None
    state.compress_scored_by_tokens = lambda _tokens: (deferred, False, 100)
    retry = TurnRetryState()
    assert _clamp_output_cap(state, retry, 1000, 2000) is deferred


def test_output_cap_compression_error_still_rebuilds_request():
    from agent.turn_overflow import _Recovery, _clamp_output_cap
    from agent.turn_retry_state import TurnRetryState

    agent = SimpleNamespace(
        tools=[], compression_enabled=True, log_prefix="", _ephemeral_max_output_tokens=None,
        _buffer_vprint=lambda _message: None,
    )
    state = _Recovery(
        messages=[], active_system_prompt="", conversation_history=[], approx_tokens=0,
        compression_attempts=0, agent=agent, api_messages=[], system_message="",
        effective_task_id=None, api_call_count=0, max_compression_attempts=3,
    )
    state.request_tokens = lambda: 100
    state.compress_scored_by_tokens = lambda _tokens: (_ for _ in ()).throw(RuntimeError("no summary"))
    retry = TurnRetryState()
    verdict = _clamp_output_cap(state, retry, 1000, 2000)
    assert verdict.action == "break" and retry.restart_with_compressed_messages is True


def test_context_attempt_exhaustion_uses_context_failure_contract():
    from agent.turn_overflow import _Recovery

    persisted = []
    agent = SimpleNamespace(
        log_prefix="", _flush_status_buffer=lambda: None, _vprint=lambda *_args, **_kwargs: None,
        _persist_session=lambda *args: persisted.append(args))
    state = _Recovery(
        messages=[], active_system_prompt="", conversation_history=[], approx_tokens=0,
        compression_attempts=1, agent=agent, api_messages=[], system_message="",
        effective_task_id=None, api_call_count=2, max_compression_attempts=1)
    verdict = state.count_attempt()
    assert verdict.action == "return" and verdict.result["compression_exhausted"] is True
    assert persisted


def test_output_cap_successful_noop_compression_rebuilds_request():
    from agent.turn_overflow import _Recovery, _clamp_output_cap
    from agent.turn_retry_state import TurnRetryState

    agent = SimpleNamespace(
        tools=[], compression_enabled=True, log_prefix="", _ephemeral_max_output_tokens=None,
        _buffer_vprint=lambda _message: None)
    state = _Recovery(
        messages=[], active_system_prompt="", conversation_history=[], approx_tokens=0,
        compression_attempts=0, agent=agent, api_messages=[], system_message="",
        effective_task_id=None, api_call_count=0, max_compression_attempts=3)
    state.request_tokens = lambda: 100
    state.compress_scored_by_tokens = lambda _tokens: (None, False, 100)
    retry = TurnRetryState()
    assert _clamp_output_cap(state, retry, 1000, 2000).action == "break"
    assert retry.restart_with_compressed_messages is True


def test_skill_prompt_without_user_prompt_skips_strict_scan(monkeypatch):
    import cron.scheduler_prompt as prompt

    strict_calls = []
    monkeypatch.setattr("tools.cronjob_tools._scan_cron_prompt",
                        lambda value: strict_calls.append(value))
    monkeypatch.setattr("tools.cronjob_prompt_scan._scan_cron_skill_assembled",
                        lambda value: (value, None))
    assert prompt._scan_assembled_cron_prompt("assembled", {"id": "job"}, has_skills=True) == "assembled"
    assert strict_calls == []


def test_prepare_job_prompt_blocks_empty_payload(monkeypatch):
    import cron.scheduler as scheduler

    monkeypatch.setattr("hermes_cli.config.require_parseable_user_config", lambda: None)
    monkeypatch.setattr("cron.jobs.job_payload_is_empty", lambda _job: True)
    monkeypatch.setattr(scheduler, "_block_and_pause_job",
                        lambda *_args: (False, "blocked", "", "empty"))
    early, prompt, script_failed = scheduler._prepare_job_prompt(
        {"id": "empty"}, "empty", "Empty", None, None)
    assert early == (False, "blocked", "", "empty")
    assert prompt is None and script_failed is False


def test_prepare_job_prompt_returns_monitor_gate_result(monkeypatch):
    import cron.scheduler as scheduler

    expected = (True, "monitor", "", None)
    monkeypatch.setattr("hermes_cli.config.require_parseable_user_config", lambda: None)
    monkeypatch.setattr("cron.jobs.job_payload_is_empty", lambda _job: False)
    monkeypatch.setattr(scheduler, "_apply_monitor_gate", lambda *_args: (expected, None))
    early, prompt, script_failed = scheduler._prepare_job_prompt(
        {"id": "monitor", "prompt": "x"}, "monitor", "Monitor", None, None)
    assert early is expected and prompt is None and script_failed is False


def test_prepare_job_prompt_treats_none_as_silent(monkeypatch):
    import cron.scheduler as scheduler

    monkeypatch.setattr("hermes_cli.config.require_parseable_user_config", lambda: None)
    monkeypatch.setattr("cron.jobs.job_payload_is_empty", lambda _job: False)
    monkeypatch.setattr(scheduler, "_apply_monitor_gate", lambda *_args: (None, None))
    monkeypatch.setattr(scheduler, "_build_job_prompt", lambda *_args, **_kwargs: None)
    early, prompt, script_failed = scheduler._prepare_job_prompt(
        {"id": "silent", "prompt": "x"}, "silent", "Silent", None, None)
    assert early == (True, "", scheduler.SILENT_MARKER, None)
    assert prompt is None and script_failed is False


def _classified(reason):
    return SimpleNamespace(
        reason=reason, billing_unverified=False, error_context={}, is_auth=False)


def _route_agent():
    return SimpleNamespace(
        compression_enabled=True,
        context_compressor=SimpleNamespace(
            context_length=300_000,
            update_model=lambda **_kwargs: None,
            _context_probed=False,
            _context_probe_persistable=True,
        ),
        model="fixture", base_url="https://provider.test/v1", api_key="key",
        provider="custom", api_mode="chat_completions", tools=[], log_prefix="",
        _fallback_index=0, _fallback_chain=[], _credential_pool=None,
        _rate_limit_state={},
        _buffer_vprint=lambda _message: None,
        _buffer_status=lambda _message: None,
        _flush_status_buffer=lambda: None,
        _vprint=lambda *_args, **_kwargs: None,
        _persist_session=lambda *_args: None,
    )


def _route_kwargs(agent, classified):
    return dict(
        agent=agent, api_error=SimpleNamespace(status_code=429), classified=classified,
        _retry=__import__("agent.turn_retry_state", fromlist=["TurnRetryState"]).TurnRetryState(),
        error_msg="fixture", error_context={}, recovered_with_pool=False,
        base_url=agent.base_url, model=agent.model,
        messages=[{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}],
        api_messages=[{"role": "user", "content": "one"}], system_message="system",
        active_system_prompt="system", conversation_history=[], retry_count=0, max_retries=3,
        compression_attempts=0, max_compression_attempts=3, api_call_count=1,
        effective_task_id="task",
    )


def test_long_context_tier_compresses_and_restarts(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    agent._compress_context = lambda *_args, **_kwargs: ([{"role": "user", "content": "short"}], "short")
    monkeypatch.setattr("agent.conversation_compression.conversation_history_after_compression",
                        lambda _agent, messages, _history: list(messages))
    monkeypatch.setattr("agent.model_metadata.estimate_request_tokens_rough", lambda *_args, **_kwargs: 123)
    monkeypatch.setattr("agent.turn_recovery.time.sleep", lambda _seconds: None)
    verdict = route_classified_error(**_route_kwargs(agent, _classified(FailoverReason.long_context_tier)))
    assert verdict.action == "break"
    assert verdict.provider_overflow_recovery_pending is True
    assert verdict.compression_attempts == 1


def test_zai_overload_raises_unbound_retry_ceiling(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    monkeypatch.setattr("agent.turn_recovery.is_zai_coding_overload_error", lambda **_kwargs: True)
    monkeypatch.setattr("agent.turn_recovery.zai_coding_overload_retry_ceiling", lambda: 9)
    verdict = route_classified_error(**_route_kwargs(agent, _classified(FailoverReason.overloaded)))
    assert verdict.action == "fallthrough" and verdict.max_retries == 9
    assert verdict.is_zai_coding_overload is True


def test_genuine_nous_rate_limit_reenters_loop_once(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    agent.provider = "nous"
    monkeypatch.setattr("agent.turn_recovery._is_genuine_nous_rate_limit",
                        lambda *_args, **_kwargs: True)
    kwargs = _route_kwargs(agent, _classified(FailoverReason.rate_limit))
    kwargs.update(max_retries=4, allow_status_fallback=False)
    verdict = route_classified_error(**kwargs)
    assert verdict.action == "continue" and verdict.retry_count == 3


def test_long_context_exhaustion_falls_through_without_compression():
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    agent._compress_context = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("compression must not run"))
    kwargs = _route_kwargs(agent, _classified(FailoverReason.long_context_tier))
    kwargs.update(compression_attempts=3, max_compression_attempts=3)
    assert route_classified_error(**kwargs).action == "fallthrough"


def test_long_context_compression_without_reduction_falls_through(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    agent.context_compressor.context_length = 200_000
    original = [{"role": "user", "content": "one"}]
    agent._compress_context = lambda *_args, **_kwargs: (original, "system")
    monkeypatch.setattr("agent.conversation_compression.conversation_history_after_compression",
                        lambda _agent, messages, _history: list(messages))
    monkeypatch.setattr("agent.model_metadata.estimate_request_tokens_rough", lambda *_args, **_kwargs: 123)
    kwargs = _route_kwargs(agent, _classified(FailoverReason.long_context_tier))
    kwargs["messages"] = original
    assert route_classified_error(**kwargs).action == "fallthrough"


def test_rate_limit_pool_recovery_defers_fallback(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    agent._fallback_chain = ["fallback"]
    agent._try_activate_fallback = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("fallback must wait for pool rotation"))
    monkeypatch.setattr("agent.conversation_loop._ra", lambda: SimpleNamespace(
        _pool_may_recover_from_rate_limit=lambda _pool: True))
    assert route_classified_error(
        **_route_kwargs(agent, _classified(FailoverReason.rate_limit))).action == "fallthrough"


def test_auth_fallback_decline_continues_to_normal_handling():
    from agent.error_classifier import FailoverReason
    from agent.turn_recovery import route_classified_error

    agent = _route_agent()
    agent._fallback_chain = ["fallback"]
    agent._try_activate_fallback = lambda **_kwargs: False
    classified = _classified(FailoverReason.auth)
    classified.is_auth = True
    kwargs = _route_kwargs(agent, classified)
    verdict = route_classified_error(**kwargs)
    assert verdict.action == "fallthrough"
    assert kwargs["_retry"].auth_failover_attempted is True


def _codex_test_helpers():
    from tests.agent import test_run_agent_codex_responses as helpers
    return helpers


def _codex_import_stubs(monkeypatch):
    class TransportError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(
        RemoteProtocolError=TransportError, ReadTimeout=TransportError, ConnectError=TransportError))
    try:
        import openai  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(APIConnectionError=TransportError))
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **_kwargs: SimpleNamespace())
    return TransportError


def test_codex_stream_close_failure_discards_reusable_client(monkeypatch, tmp_path):
    _codex_import_stubs(monkeypatch)
    helpers = _codex_test_helpers()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent._hermes_home", tmp_path)
    agent = helpers._build_agent(monkeypatch)
    aborted = []

    class BrokenCloseStream(helpers._FakeCreateStream):
        def close(self):
            raise RuntimeError("close failed")

    stream = BrokenCloseStream([
        SimpleNamespace(type="response.completed", response=SimpleNamespace(status="completed"))])
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: stream))
    monkeypatch.setattr(agent, "_abort_request_openai_client",
                        lambda active, *, reason: aborted.append((active, reason)))
    result = agent._run_codex_stream(helpers._codex_request_kwargs(), client=client)
    assert result.status == "completed"
    assert aborted == [(client, "codex_stream_close_failed")]


def test_codex_runtime_error_uses_relay_final_response(monkeypatch, tmp_path):
    _codex_import_stubs(monkeypatch)
    helpers = _codex_test_helpers()
    from agent import codex_runtime, relay_llm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent._hermes_home", tmp_path)
    agent = helpers._build_agent(monkeypatch)
    expected = SimpleNamespace(status="completed", output=[])
    stream = SimpleNamespace(final_response=expected, close=lambda: None)
    monkeypatch.setattr(relay_llm, "stream", lambda *_args, **_kwargs: stream)
    monkeypatch.setattr(codex_runtime, "_consume_codex_event_stream",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no terminal")))
    assert agent._run_codex_stream(helpers._codex_request_kwargs()) is expected


def test_codex_http_400_logs_only_shape_metadata(monkeypatch, tmp_path, caplog):
    _codex_import_stubs(monkeypatch)
    helpers = _codex_test_helpers()
    from agent import relay_llm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent._hermes_home", tmp_path)
    agent = helpers._build_agent(monkeypatch)

    class BadRequest(Exception):
        status_code = 400

    monkeypatch.setattr(relay_llm, "stream",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(BadRequest("secret prompt")))
    with caplog.at_level("ERROR", logger="agent.codex_runtime"), pytest.raises(BadRequest):
        agent._run_codex_stream(helpers._codex_request_kwargs())
    assert "Codex Responses HTTP 400: input_items=" in caplog.text
    assert "secret prompt" not in caplog.text


def test_codex_transport_exhaustion_logs_and_raises(monkeypatch, tmp_path, caplog):
    transport_error = _codex_import_stubs(monkeypatch)
    helpers = _codex_test_helpers()
    from agent import relay_llm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent._hermes_home", tmp_path)
    agent = helpers._build_agent(monkeypatch)
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise transport_error("connection lost")

    monkeypatch.setattr(relay_llm, "stream", fail)
    with caplog.at_level("WARNING", logger="agent.codex_runtime"), pytest.raises(transport_error):
        agent._run_codex_stream(helpers._codex_request_kwargs())
    assert len(calls) == 2
    assert "Codex Responses request failed" in caplog.text


def test_api_error_propagates_provider_overflow_restart(monkeypatch):
    from agent import turn_api_error as api
    from agent.error_classifier import FailoverReason
    from agent.turn_retry_state import TurnRetryState

    agent = SimpleNamespace(
        _http_status_policy={}, _api_max_retries=3, thinking_callback=None,
        provider="custom", model="fixture", log_prefix="", _interrupt_requested=False,
        _extract_api_error_context=lambda _error: {},
        _invoke_api_request_error_hook=lambda **_kwargs: None,
        _touch_activity=lambda _label: None,
    )
    classified = SimpleNamespace(
        reason=FailoverReason.context_overflow, status_code=400, retryable=True,
        should_compress=True, should_rotate_credential=False, should_fallback=False)
    monkeypatch.setattr(api, "recover_before_classification", lambda *args, **kwargs: (False, "system"))
    monkeypatch.setattr("tools.interpreter_shutdown.interpreter_shutting_down", lambda _error: False)
    monkeypatch.setattr(api, "classify_api_error", lambda *args, **kwargs: classified)
    monkeypatch.setattr(api, "recover_after_classification", lambda *args, **kwargs: (False, False))
    monkeypatch.setattr(api, "log_api_error_attempt",
                        lambda *args, **kwargs: ("Error", "overflow", "custom", "url", "fixture"))
    monkeypatch.setattr(api, "route_classified_error", lambda *args, **kwargs: SimpleNamespace(
        status_code=400, messages=[], active_system_prompt="system", conversation_history=[],
        retry_count=1, max_retries=3, compression_attempts=1, is_rate_limited=False,
        wrapped_output_cap_budget=None, is_zai_coding_overload=False,
        provider_overflow_recovery_pending=True, action="break", result=None))
    verdict = api.handle_api_error(
        agent, api_error=SimpleNamespace(status_code=400), _retry=TurnRetryState(),
        thinking_spinner=None, messages=[], api_messages=[], api_kwargs={}, system_message="system",
        active_system_prompt="system", conversation_history=[], approx_tokens=0,
        retry_count=0, max_retries=3, compression_attempts=0, max_compression_attempts=3,
        api_call_count=1, api_request_id="request", api_start_time=0,
        effective_task_id="task", turn_id="turn")
    assert verdict.action == "break" and verdict._provider_overflow_recovery_pending is True


def test_api_error_propagates_overflow_handler_restart(monkeypatch):
    from agent import turn_api_error as api
    from agent.error_classifier import FailoverReason
    from agent.turn_retry_state import TurnRetryState

    agent = SimpleNamespace(
        _http_status_policy={}, _api_max_retries=3, thinking_callback=None,
        provider="custom", model="fixture", log_prefix="", _interrupt_requested=False,
        _extract_api_error_context=lambda _error: {},
        _invoke_api_request_error_hook=lambda **_kwargs: None,
        _touch_activity=lambda _label: None)
    classified = SimpleNamespace(
        reason=FailoverReason.context_overflow, status_code=400, retryable=True,
        should_compress=True, should_rotate_credential=False, should_fallback=False)
    monkeypatch.setattr(api, "recover_before_classification", lambda *args, **kwargs: (False, "system"))
    monkeypatch.setattr("tools.interpreter_shutdown.interpreter_shutting_down", lambda _error: False)
    monkeypatch.setattr(api, "classify_api_error", lambda *args, **kwargs: classified)
    monkeypatch.setattr(api, "recover_after_classification", lambda *args, **kwargs: (False, False))
    monkeypatch.setattr(api, "log_api_error_attempt",
                        lambda *args, **kwargs: ("Error", "overflow", "custom", "url", "fixture"))
    monkeypatch.setattr(api, "route_classified_error", lambda *args, **kwargs: SimpleNamespace(
        status_code=400, messages=[], active_system_prompt="system", conversation_history=[],
        retry_count=1, max_retries=3, compression_attempts=1, is_rate_limited=False,
        wrapped_output_cap_budget=None, is_zai_coding_overload=False,
        provider_overflow_recovery_pending=False, action="fallthrough", result=None))
    monkeypatch.setattr(api, "recover_from_overflow", lambda *args, **kwargs: SimpleNamespace(
        messages=[], active_system_prompt="system", conversation_history=[], approx_tokens=10,
        compression_attempts=2, is_context_length_error=True,
        provider_overflow_recovery_pending=True, action="break", result=None))
    verdict = api.handle_api_error(
        agent, api_error=SimpleNamespace(status_code=400), _retry=TurnRetryState(),
        thinking_spinner=None, messages=[], api_messages=[], api_kwargs={}, system_message="system",
        active_system_prompt="system", conversation_history=[], approx_tokens=0,
        retry_count=0, max_retries=3, compression_attempts=0, max_compression_attempts=3,
        api_call_count=1, api_request_id="request", api_start_time=0,
        effective_task_id="task", turn_id="turn")
    assert verdict.action == "break" and verdict._provider_overflow_recovery_pending is True


def test_retry_exhaustion_activates_fallback_and_restarts(monkeypatch):
    from agent.error_classifier import FailoverReason
    from agent.turn_api_error import settle_unrecovered_error
    from agent.turn_retry_state import TurnRetryState

    retry = TurnRetryState()
    agent = SimpleNamespace(
        _try_recover_primary_transport=lambda *_args, **_kwargs: False,
        _has_pending_fallback=lambda: True,
        _buffer_status=lambda _message: None,
        _try_activate_fallback=lambda: True,
    )
    monkeypatch.setattr("agent.conversation_loop._arm_fallback_restart",
                        lambda _agent, _messages, _prompt, state: setattr(
                            state, "restart_with_rebuilt_messages", True) or "fallback-system")
    verdict = settle_unrecovered_error(
        agent, api_error=RuntimeError("unreachable"),
        classified=SimpleNamespace(
            retryable=True, should_compress=False, reason=FailoverReason.server_error,
            should_fallback=True),
        _retry=retry, status_code=503, error_msg="unreachable",
        is_context_length_error=False, is_rate_limited=False,
        _is_zai_coding_overload=False, _provider="custom", _base="url", _model="fixture",
        messages=[], api_messages=[], api_kwargs={}, active_system_prompt="system",
        conversation_history=[], approx_tokens=0, retry_count=3, max_retries=3,
        compression_attempts=2, api_call_count=1)
    assert verdict.action == "break"
    assert verdict.active_system_prompt == "fallback-system"
    assert verdict.retry_count == verdict.compression_attempts == 0


class _FakeCronScope:
    workdir = "/tmp/disposable-cron-workdir"
    task_id = "task"

    def __init__(self, *_args, **_kwargs):
        pass

    def enter(self):
        pass

    def exit(self):
        pass


def _patch_minimal_cron_run(monkeypatch, scheduler):
    monkeypatch.setattr(scheduler, "_prepare_job_prompt",
                        lambda *_args, **_kwargs: (None, "prompt", False))
    monkeypatch.setattr(scheduler, "_CronRunScope", _FakeCronScope)
    monkeypatch.setattr(scheduler, "_reload_dotenv_and_publish_delivery_target", lambda _job: None)
    monkeypatch.setattr(scheduler, "_load_cron_job_config",
                        lambda *_args: SimpleNamespace(cfg={}, model="model"))
    monkeypatch.setattr("cron.scheduler_detached_worker.defer_teardown_to_running_worker",
                        lambda *_args: False)
    monkeypatch.setattr(scheduler, "_teardown_cron_agent", lambda *_args: None)


def test_cron_blocked_setup_returns_before_session_open(monkeypatch):
    import cron.scheduler as scheduler

    _patch_minimal_cron_run(monkeypatch, scheduler)
    blocked = (False, "blocked", "", "policy")
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup",
                        lambda *_args: SimpleNamespace(blocked=blocked, model="model"))
    monkeypatch.setattr(scheduler, "_open_cron_session_db",
                        lambda _job: (_ for _ in ()).throw(AssertionError("session opened")))
    assert scheduler.run_job({"id": "blocked", "name": "Blocked"}) == blocked


def test_cron_unreachable_classifier_failure_cannot_mask_original(monkeypatch):
    import cron.scheduler as scheduler

    _patch_minimal_cron_run(monkeypatch, scheduler)
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("original failure")))
    monkeypatch.setattr("cron.unreachable_retry.is_model_unreachable_failure",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("classifier failure")))
    success, output, response, error = scheduler.run_job({"id": "failed", "name": "Failed"})
    assert success is False and response == ""
    assert "FAILED" in output and error == "RuntimeError: original failure"


def test_cron_failed_live_agent_is_marked_unreachable_and_deferred(monkeypatch):
    import cron.scheduler as scheduler

    _patch_minimal_cron_run(monkeypatch, scheduler)
    setup = SimpleNamespace(blocked=None, model="model")
    agent = SimpleNamespace()
    audit_errors = []
    monkeypatch.setattr(scheduler, "_resolve_cron_agent_setup", lambda *_args: setup)
    monkeypatch.setattr(scheduler, "_open_cron_session_db", lambda _job: None)
    monkeypatch.setattr(scheduler, "_construct_cron_agent", lambda *_args, **_kwargs: agent)
    monkeypatch.setattr(scheduler, "_FireAudit", lambda *_args: SimpleNamespace(
        write=lambda _result, error: audit_errors.append(error)))
    monkeypatch.setattr(scheduler, "_run_agent_with_watchdog",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr("cron.unreachable_retry.is_model_unreachable_failure",
                        lambda error, actual_agent: isinstance(error, ConnectionError)
                        and actual_agent is agent)
    deferred = []
    job = {"id": "offline", "name": "Offline"}
    success, output, response, error = scheduler.run_job(job, defer_agent_teardown=deferred)
    assert success is False and response == "" and "FAILED" in output
    assert error == "ConnectionError: offline"
    assert job["_model_unreachable"] is True
    assert deferred == [agent]
    assert audit_errors == ["ConnectionError: offline"]
