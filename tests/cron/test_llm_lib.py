"""llm_lib: the reasoning-off policy is applied by default, opt-in passes through, and the
thinking-truncation signature raises instead of silently reading as a bad answer."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("llm_lib", REPO / "scripts" / "cron" / "llm_lib.py")
L = importlib.util.module_from_spec(spec); sys.modules["llm_lib"] = L
spec.loader.exec_module(L)

MSGS = [{"role": "user", "content": "hi"}]


def test_policy_off_by_default():
    body = L.build_body(MSGS, "qwen3.5:27b")
    assert body["reasoning_effort"] == "none"        # the whole point


def test_explicit_optin_passes_through():
    assert L.build_body(MSGS, "m", reasoning_effort="high")["reasoning_effort"] == "high"


def test_none_omits_the_key():
    assert "reasoning_effort" not in L.build_body(MSGS, "m", reasoning_effort=None)


def test_truncation_signature_raises():
    resp = {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    with pytest.raises(L.LLMTruncation):
        L.parse_content(resp)


def test_clean_answer_parses():
    resp = {"choices": [{"finish_reason": "stop", "message": {"content": "generic-ml"}}]}
    assert L.parse_content(resp) == "generic-ml"


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
