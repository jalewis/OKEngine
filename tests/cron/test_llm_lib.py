"""llm_lib: the reasoning-off policy is applied by default, opt-in passes through, and the
thinking-truncation signature raises instead of silently reading as a bad answer."""
import importlib.util
import sys
import urllib.error
import io
from types import SimpleNamespace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("llm_lib", REPO / "scripts" / "cron" / "llm_lib.py")
L = importlib.util.module_from_spec(spec); sys.modules["llm_lib"] = L
spec.loader.exec_module(L)

MSGS = [{"role": "user", "content": "hi"}]


def test_policy_off_by_default():
    body = L.build_body(MSGS, "qwen3.5:27b")
    assert body["reasoning"] == {"effort": "none"}        # the whole point
    assert body["input"] == [{"type": "message", "role": "user", "content": "hi"}]
    assert MSGS == [{"role": "user", "content": "hi"}]
    assert body["max_output_tokens"] == 256
    assert body["stream"] is True
    assert "messages" not in body
    assert "max_tokens" not in body


def test_explicit_optin_passes_through():
    assert L.build_body(MSGS, "m", reasoning_effort="high")["reasoning"] == {"effort": "high"}


def test_none_omits_the_key():
    assert "reasoning" not in L.build_body(MSGS, "m", reasoning_effort=None)


def test_multiturn_history_types_every_role_based_message():
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
        {"role": "user", "content": "continue"},
    ]
    items = L.build_body(messages, "m")["input"]
    assert [item["type"] for item in items] == ["message"] * 4
    assert items[2]["content"] == [{"type": "output_text", "text": "hello"}]


def test_typed_nonmessage_items_are_preserved():
    items = [
        {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "message", "role": "assistant", "content": "done"},
    ]
    assert L.build_body(items, "m")["input"] == items


def test_nonobject_input_item_fails_closed():
    with pytest.raises(L.LLMError, match="must be an object"):
        L.build_body([{"role": "user", "content": "hi"}, "bad"], "m")


def test_truncation_signature_raises():
    resp = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": ""}]}],
    }
    with pytest.raises(L.LLMTruncation):
        L.parse_content(resp)


def test_clean_answer_parses():
    resp = {
        "status": "completed",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "generic-ml"}],
        }],
    }
    assert L.parse_content(resp) == "generic-ml"


def test_parser_ignores_nonmessage_and_nontext_parts_and_rejects_missing_output():
    response = {"output": [
        "scalar",
        {"type": "tool_call", "content": []},
        {"type": "message", "content": ["scalar", {"type": "refusal", "text": "no"},
                                         {"type": "output_text", "text": "yes"}]},
    ]}
    assert L.parse_content(response) == "yes"
    with pytest.raises(L.LLMError, match="malformed Responses payload"):
        L.parse_content({})


def test_classify_maps_to_label_or_uncertain(monkeypatch):
    monkeypatch.setattr(L, "chat", lambda *a, **k: "The answer is generic-ml.")
    assert L.classify("x", ["generic-ml", "security-market"]) == "generic-ml"
    monkeypatch.setattr(L, "chat", lambda *a, **k: "hard to say")
    assert L.classify("x", ["generic-ml", "security-market"]) == "uncertain"


def test_unconfigured_raises_helpfully(monkeypatch):
    monkeypatch.delenv("OKENGINE_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OKENGINE_LLM_MODEL", raising=False)
    with pytest.raises(L.LLMError, match="OKENGINE_LLM_BASE_URL"):
        L.chat("hi")


def test_endpoint_rejects_non_http_scheme():
    with pytest.raises(L.LLMError, match=r"http\(s\)"):
        L._resolve("file:///etc/passwd", "model")


def test_provider_failure_retries_responses_only_then_fails_terminally(monkeypatch):
    attempted = []

    def unavailable(request, **_kwargs):
        attempted.append(request.full_url)
        raise urllib.error.URLError("local provider unavailable")

    monkeypatch.setattr(L.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(L.time, "sleep", lambda _seconds: None)
    with pytest.raises(L.LLMError, match="failed after 3 attempt"):
        L.chat("fixture", model="qwen3-coder:30b",
               base_url="http://local-qwen/v1", retries=2)

    assert attempted == ["http://local-qwen/v1/responses"] * 3


def test_pool_contract_timeout_identity_and_jittered_exponential_retry(monkeypatch):
    attempted, sleeps = [], []
    monkeypatch.setenv("OKENGINE_PACK", "market-intel")
    monkeypatch.setenv("OKENGINE_LLM_COMPONENT", "entity-backfill")
    monkeypatch.setattr(L.random, "uniform", lambda _low, high: high / 2)
    monkeypatch.setattr(L.time, "sleep", sleeps.append)

    def unavailable(request, **kwargs):
        attempted.append((request, kwargs))
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)

    monkeypatch.setattr(L.urllib.request, "urlopen", unavailable)
    with pytest.raises(L.LLMError):
        L.chat("x", model="m", base_url="http://pool:11436/v1", retries=2)
    assert [kwargs["timeout"] for _, kwargs in attempted] == [500, 500, 500]
    assert attempted[0][0].get_header("X-client-id") == "okengine/market-intel/entity-backfill"
    assert attempted[0][0].get_header("X-oneshot") == "1"
    assert sleeps == [3.0, 6.0]


def test_permanent_http_error_is_not_retried(monkeypatch):
    attempted = []
    def bad_request(request, **_kwargs):
        attempted.append(request)
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)
    monkeypatch.setattr(L.urllib.request, "urlopen", bad_request)
    with pytest.raises(L.LLMError, match="after 1 attempt"):
        L.chat("x", model="m", base_url="http://local/v1", retries=2)
    assert len(attempted) == 1


def test_stream_parser_and_conversation_routing():
    response = io.BytesIO(
        b'data: {"type":"response.output_text.delta","delta":"hel"}\n\n'
        b'data: {"type":"response.output_text.delta","delta":"lo"}\n\n'
        b'data: [DONE]\n\n'
    )
    assert L.parse_stream(response) == "hello"
    headers = L._identity_headers(client_id="okengine/test", conversation_id="run-1")
    assert headers == {"X-Client-Id": "okengine/test", "X-Conversation-Id": "run-1"}


def test_stream_parser_completed_fallback_and_malformed_events():
    completed = {
        "status": "completed",
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "fallback"},
        ]}],
    }
    response = io.BytesIO(
        b"event: response.completed\n"
        b"data:\n"
        b"data: not-json\n"
        b'data: {"type":"unrelated"}\n'
        + ("data: " + L.json.dumps({"type": "response.completed", "response": completed})
           + "\n").encode()
    )
    assert L.parse_stream(response) == "fallback"

    terminal = dict(completed, type="response.incomplete")
    assert L.parse_stream(io.BytesIO(("data: " + L.json.dumps(terminal) + "\n").encode())) == "fallback"
    with pytest.raises(L.LLMError, match="no output text"):
        L.parse_stream(io.BytesIO(b"event: ping\ndata: [DONE]\n"))


def test_identity_headers_environment_override_and_no_routing(monkeypatch):
    monkeypatch.setenv("OKENGINE_LLM_CLIENT_ID", "x" * 120)
    monkeypatch.setenv("OKENGINE_PACK", "ignored")
    monkeypatch.setenv("OKENGINE_LLM_COMPONENT", "ignored")
    monkeypatch.setenv("OKENGINE_LLM_CONVERSATION_ID", "conversation-from-env")
    assert L._identity_headers() == {
        "X-Client-Id": "x" * 96,
        "X-Conversation-Id": "conversation-from-env",
    }
    monkeypatch.delenv("OKENGINE_LLM_CLIENT_ID")
    monkeypatch.delenv("OKENGINE_PACK")
    monkeypatch.delenv("OKENGINE_LLM_COMPONENT")
    monkeypatch.delenv("OKENGINE_LLM_CONVERSATION_ID")
    assert L._identity_headers(oneshot=False) == {"X-Client-Id": "okengine/unknown"}


def test_identity_headers_prefer_deployment_over_composed_pack(monkeypatch):
    monkeypatch.setenv("OKENGINE_DEPLOYMENT", "okcti")
    monkeypatch.setenv("OKENGINE_PACK", "okpack-threat-actors")
    monkeypatch.setenv("OKENGINE_LLM_COMPONENT", "review")
    assert L._identity_headers(oneshot=False) == {
        "X-Client-Id": "okengine/okcti/review",
    }


def test_chat_sets_authorization_and_reraises_parsed_errors(monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(L.urllib.request, "urlopen", lambda request, **_kw: seen.append(request) or Response())
    monkeypatch.setattr(L.json, "load", lambda _response: {})
    with pytest.raises(L.LLMError, match="malformed Responses payload"):
        L.chat("x", model="m", base_url="http://local/v1", api_key="secret", retries=0,
               stream=False)
    assert seen[0].get_header("Authorization") == "Bearer secret"
