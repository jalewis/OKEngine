"""Target custom profile contracts against the actual Hermes v0.21.3 classes."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from openai import APIConnectionError

from agent.codex_runtime import run_codex_stream
from agent.transports.chat_completions import ChatCompletionsTransport
from providers.base import ProviderProfile


OVERLAY = Path.cwd() / "plugins/model-providers/custom/__init__.py"
OLD_OVERLAY = Path(__file__).resolve().parents[3] / "plugins/model-providers/custom/__init__.py"
assert OVERLAY.read_bytes() == (
    Path(__file__).resolve().parents[1] / "plugins/model-providers/custom/__init__.py"
).read_bytes(), "target checkout custom overlay differs from staged source"


def _load(name: str, source: Path):
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TARGET = _load("okengine_target_custom_profile", OVERLAY)
# Synthetic local non-Ollama endpoint; the live llama.cpp route is never
# contacted by these contracts or embedded as fleet-specific test data.
LLAMACPP_URL = "http://localhost:11436/v1"
OLLAMA_URL = "http://localhost:11434/v1"
HOSTED_URL = "https://gateway.example.test/v1"


def _extras(url: str, *, reasoning=None, ctx=65536):
    return TARGET.custom.build_api_kwargs_extras(
        reasoning_config=reasoning, ollama_num_ctx=ctx, base_url=url,
    )


def _wire(url: str, *, reasoning=None, ctx=65536):
    transport = ChatCompletionsTransport()
    return transport.build_kwargs(
        "qwen3-coder:30b", [{"role": "user", "content": "fixture"}],
        provider_profile=TARGET.custom, reasoning_config=reasoning,
        base_url=url, ollama_num_ctx=ctx, temperature=0.9,
    )


def test_target_profile_preserves_v0213_class_and_fetch_models_signature():
    assert isinstance(TARGET.custom, ProviderProfile)
    assert TARGET.custom.fixed_temperature is None
    with patch.object(ProviderProfile, "fetch_models", autospec=True,
                      return_value=["fixture-model"]) as fetch:
        assert TARGET.custom.fetch_models(base_url=LLAMACPP_URL) == ["fixture-model"]
    fetch.assert_called_once_with(TARGET.custom, api_key=None,
                                  base_url=LLAMACPP_URL, timeout=8.0)


def test_llamacpp_local_route_gets_temperature_not_ollama_fields(monkeypatch):
    monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    extra, top = _extras(LLAMACPP_URL)
    assert extra == {}, "llama.cpp route received Ollama-native request body"
    assert top == {"temperature": 0.2}
    wire = _wire(LLAMACPP_URL)
    assert wire["temperature"] == 0.2
    assert "extra_body" not in wire
    assert "reasoning_effort" not in wire


def test_old_overlay_leaks_ollama_fields_on_real_llamacpp_route(monkeypatch):
    monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    old = _load("okengine_old_custom_profile", OLD_OVERLAY)
    old_extra, old_top = old.custom.build_api_kwargs_extras(
        reasoning_config=None, ollama_num_ctx=65536, base_url=LLAMACPP_URL,
    )
    assert old_extra == {"options": {"num_ctx": 65536}, "think": False}
    assert old_top == {"reasoning_effort": "none"}
    target_extra, _target_top = _extras(LLAMACPP_URL)
    assert target_extra == {}


def test_identified_ollama_keeps_local_defaults_and_context(monkeypatch):
    monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    extra, top = _extras(OLLAMA_URL)
    assert extra == {"options": {"num_ctx": 65536}, "think": False}
    assert top == {"temperature": 0.2, "reasoning_effort": "none"}
    wire = _wire(OLLAMA_URL)
    assert wire["extra_body"] == extra
    assert wire["reasoning_effort"] == "none"


def test_hosted_custom_route_has_no_local_policy_fields(monkeypatch):
    monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    extra, top = _extras(HOSTED_URL)
    assert extra == {}
    assert top == {}
    wire = _wire(HOSTED_URL)
    assert wire["temperature"] == 0.9  # caller value, not local override
    assert "extra_body" not in wire


def test_explicit_reasoning_is_clamped_without_think_true(monkeypatch):
    monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    extra, top = _extras(OLLAMA_URL, reasoning={"enabled": True, "effort": "ultra"})
    assert "think" not in extra
    assert top["reasoning_effort"] == "max"
    disabled_extra, disabled_top = _extras(OLLAMA_URL,
                                           reasoning={"enabled": False, "effort": "high"})
    assert disabled_extra["think"] is False
    assert disabled_top["reasoning_effort"] == "none"


def test_enabled_ollama_without_effort_uses_default_no_thinking(monkeypatch):
    monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    extra, top = _extras(OLLAMA_URL, reasoning={"enabled": True})
    assert extra["think"] is False
    assert top["reasoning_effort"] == "none"


def test_disabled_reasoning_on_llamacpp_never_sends_ollama_think():
    extra, top = _extras(LLAMACPP_URL, reasoning={"enabled": False})
    assert extra == {}
    assert top["reasoning_effort"] == "none"


def test_malformed_endpoint_port_is_not_ollama():
    assert TARGET._looks_like_ollama_endpoint("http://localhost:notaport/v1") is False


def test_fetch_models_without_configured_url_returns_none():
    assert TARGET.custom.fetch_models() is None


@pytest.mark.parametrize("value,expected", [
    ("", None), ("caller", None), ("-5", 0.0), ("500", 2.0),
    ("bad", 0.2), ("nan", 0.2), ("inf", 0.2),
])
def test_local_temperature_override_boundary(monkeypatch, value, expected):
    monkeypatch.setenv("OKENGINE_LOCAL_TEMPERATURE", value)
    _extra, top = _extras(LLAMACPP_URL)
    assert top.get("temperature") == expected


@pytest.mark.parametrize("ctx", [None, 0, -1, True, "65536"])
def test_invalid_context_setting_never_reaches_ollama_body(ctx):
    extra, _top = _extras(OLLAMA_URL, ctx=ctx)
    assert "options" not in extra


def test_malformed_reasoning_and_base_url_fail_without_ollama_fields():
    extra, top = TARGET.custom.build_api_kwargs_extras(
        reasoning_config={"effort": 123}, ollama_num_ctx=65536, base_url=None,
    )
    assert extra == {}
    assert "reasoning_effort" not in top
    assert "temperature" not in top


def test_strict_responses_preflight_preserves_transport_failure_diagnostics(caplog):
    request_content = b'{"input":"payload"}'
    request = httpx.Request("POST", "https://example.invalid/responses",
                            content=request_content)
    transport_error = httpx.RemoteProtocolError("disconnected", request=request)
    connection_error = APIConnectionError(request=request)
    connection_error.__cause__ = transport_error

    class FailingResponses:
        def create(self, **_kwargs):
            raise connection_error

    agent = SimpleNamespace(
        _interrupt_requested=False, _current_api_request_id="request-id",
        _fallback_index=0, is_subagent=False, model="gpt-5.6-sol",
        provider="openai-codex", session_id="",
    )
    api_kwargs = {
        "model": "gpt-5.6-sol",
        "instructions": "Exercise transport diagnostics.",
        "input": [{"role": "user", "content": "fixture"}],
    }

    with caplog.at_level(logging.WARNING, logger="agent.codex_runtime"):
        with pytest.raises(APIConnectionError):
            run_codex_stream(agent, api_kwargs,
                             client=SimpleNamespace(responses=FailingResponses()))

    message = caplog.messages[-1]
    assert f"serialized_request_body_bytes={len(request_content)}" in message
    assert "stream_opened=false" in message
    assert "exception_chain=APIConnectionError <- RemoteProtocolError" in message
    assert "payload" not in message
    assert "example.invalid" not in message
