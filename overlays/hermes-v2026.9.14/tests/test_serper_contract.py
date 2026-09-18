"""Run against the pinned Hermes v0.21.3 source, not old provider mocks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import create_autospec

import httpx
import pytest

from agent.web_search_provider import WebSearchProvider
from plugins.web._common import BaseWebSearchProvider


PROVIDER_FILE = Path.cwd() / "plugins/web/serper/provider.py"
OLD_PROVIDER_FILE = Path(__file__).resolve().parents[3] / "plugins/web/serper/provider.py"
assert PROVIDER_FILE.read_bytes() == (
    Path(__file__).resolve().parents[1] / "plugins/web/serper/provider.py"
).read_bytes(), "target checkout Serper provider differs from staged source"
SPEC = importlib.util.spec_from_file_location("okengine_target_serper_provider", PROVIDER_FILE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SerperWebSearchProvider = MODULE.SerperWebSearchProvider
ENDPOINT = "https://google.serper.dev/search"
ORIGINAL_POST = httpx.post


def _config_key(monkeypatch, value: str | None):
    from hermes_cli import config

    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    getter = create_autospec(config.get_env_value, return_value=value)
    monkeypatch.setattr(config, "get_env_value", getter)
    return getter


def _post_response(monkeypatch, status: int, *, payload=None, text: str | None = None):
    request = httpx.Request("POST", ENDPOINT)
    if text is None:
        response = httpx.Response(status, json=payload, request=request)
    else:
        response = httpx.Response(status, text=text, request=request)
    post = create_autospec(ORIGINAL_POST, return_value=response)
    monkeypatch.setattr(MODULE.httpx, "post", post)
    return post


def test_serper_implements_real_target_abc_and_config_credentials(monkeypatch):
    getter = _config_key(monkeypatch, "fixture-key")
    provider = SerperWebSearchProvider()
    assert isinstance(provider, BaseWebSearchProvider)
    assert isinstance(provider, WebSearchProvider)
    assert provider.name == "serper"
    assert provider.is_available() is True
    assert provider.supports_search() is True
    assert provider.supports_extract() is False
    assert provider.get_setup_schema()["env_vars"][0]["key"] == "SERPER_API_KEY"
    getter.assert_called_with("SERPER_API_KEY")


def test_serper_registration_uses_copied_target_plugin_entry_point():
    entry = Path.cwd() / "plugins/web/serper/__init__.py"
    assert entry.read_bytes() == (
        Path(__file__).resolve().parents[1] / "plugins/web/serper/__init__.py"
    ).read_bytes(), "target checkout Serper registration differs from staged source"
    spec = importlib.util.spec_from_file_location("okengine_target_serper_entry", entry)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class RegistrationRecorder:
        def __init__(self):
            self.providers = []

        def register_web_search_provider(self, provider):
            self.providers.append(provider)

    recorder = RegistrationRecorder()
    module.register(recorder)
    assert len(recorder.providers) == 1
    assert isinstance(recorder.providers[0], BaseWebSearchProvider)
    assert recorder.providers[0].name == "serper"


def test_config_only_key_reproduces_old_plugin_defect(monkeypatch):
    _config_key(monkeypatch, "fixture-key")
    old_spec = importlib.util.spec_from_file_location("okengine_old_serper_provider", OLD_PROVIDER_FILE)
    assert old_spec is not None and old_spec.loader is not None
    old_module = importlib.util.module_from_spec(old_spec)
    old_spec.loader.exec_module(old_module)
    assert old_module.SerperWebSearchProvider().is_available() is False
    assert SerperWebSearchProvider().is_available() is True


def test_search_normalizes_serper_rows_and_clamps_count(monkeypatch):
    _config_key(monkeypatch, "fixture-key")
    post = _post_response(monkeypatch, 200, payload={"organic": [
        {"title": "First", "link": "https://example.test/1", "snippet": "One", "position": 3},
        {"title": "Second", "link": "https://example.test/2", "snippet": "Two"},
    ]})
    result = SerperWebSearchProvider().search("fixture query", limit=1)
    assert result == {"success": True, "data": {"web": [
        {"title": "First", "url": "https://example.test/1", "description": "One", "position": 3},
    ]}}
    post.assert_called_once()
    assert post.call_args.kwargs["json"] == {"q": "fixture query", "num": 1}
    assert post.call_args.kwargs["headers"]["X-API-KEY"] == "fixture-key"


def test_missing_config_key_fails_without_http(monkeypatch):
    _config_key(monkeypatch, None)
    post = create_autospec(ORIGINAL_POST)
    monkeypatch.setattr(MODULE.httpx, "post", post)
    provider = SerperWebSearchProvider()
    assert provider.is_available() is False
    assert provider.search("query") == {"success": False, "error": "SERPER_API_KEY is not set"}
    post.assert_not_called()


@pytest.mark.parametrize("payload", [None, {"organic": "wrong"}, {"organic": [None]}])
def test_malformed_vendor_payload_is_failure_envelope(monkeypatch, payload):
    _config_key(monkeypatch, "fixture-key")
    if payload is None:
        _post_response(monkeypatch, 200, text="null")
    else:
        _post_response(monkeypatch, 200, payload=payload)
    result = SerperWebSearchProvider().search("query")
    assert result["success"] is False
    assert "invalid organic" in result["error"]


def test_http_503_and_bad_json_are_failure_envelopes(monkeypatch):
    _config_key(monkeypatch, "fixture-key")
    _post_response(monkeypatch, 503, payload={"error": "unavailable"})
    result = SerperWebSearchProvider().search("query")
    assert result == {"success": False, "error": "Serper returned HTTP 503"}
    _post_response(monkeypatch, 200, text="not-json")
    result = SerperWebSearchProvider().search("query")
    assert result == {"success": False, "error": "Could not parse Serper response as JSON"}


def test_transport_request_error_is_failure_envelope(monkeypatch):
    _config_key(monkeypatch, "fixture-key")
    request = httpx.Request("POST", ENDPOINT)
    post = create_autospec(ORIGINAL_POST,
                           side_effect=httpx.ConnectError("fixture transport down", request=request))
    monkeypatch.setattr(MODULE.httpx, "post", post)
    result = SerperWebSearchProvider().search("query")
    assert result["success"] is False
    assert "Could not reach Serper" in result["error"]
    post.assert_called_once()


def test_null_limit_fails_before_http_and_negative_limit_clamps(monkeypatch):
    _config_key(monkeypatch, "fixture-key")
    post = _post_response(monkeypatch, 200, payload={"organic": []})
    malformed = SerperWebSearchProvider().search("query", limit=None)
    assert malformed["success"] is False
    post.assert_not_called()
    valid = SerperWebSearchProvider().search("query", limit=-3)
    assert valid == {"success": True, "data": {"web": []}}
    assert post.call_args.kwargs["json"]["num"] == 1


def test_interrupted_search_never_sends_request(monkeypatch):
    from tools import interrupt

    _config_key(monkeypatch, "fixture-key")
    stopped = create_autospec(interrupt.is_interrupted, return_value=True)
    monkeypatch.setattr(interrupt, "is_interrupted", stopped)
    post = create_autospec(ORIGINAL_POST)
    monkeypatch.setattr(MODULE.httpx, "post", post)
    assert SerperWebSearchProvider().search("query") == {"success": False, "error": "Interrupted"}
    post.assert_not_called()
