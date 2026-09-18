"""llm_lib — the ONE sanctioned direct-LLM-call path for engine/pack scripts.

Why this exists (the blind-spot fix): gateway agent lanes inherit thinking policy from the
Hermes provider profiles (plugins/model-providers/custom disables qwen thinking by default;
deepseek keeps V4 reasoning). A script that curls the model endpoint DIRECTLY bypasses that
layer entirely — a qwen3.x reasoning model then spends the whole max_tokens budget thinking
and returns content='' + finish_reason=length, which reads as failure (a real 90-page bulk
classify returned 88 false-"uncertain" exactly this way). A policy enforced in one client is
not a policy; so direct calls get ONE blessed path with the policy baked in, and
tests/test_llm_call_discipline.py FAILS the build on raw retired-endpoint calls anywhere else
in scripts/ or extensions/.

Policy: reasoning/thinking is DISABLED by default on every call (`reasoning_effort: "none"`,
the knob Ollama-style /v1 endpoints honor — `/no_think` / `enable_thinking` / \
`chat_template_kwargs` are NOT honored). A caller that genuinely needs multi-step reasoning
opts in explicitly (reasoning_effort="high"); a caller hitting a provider that rejects the
key can pass reasoning_effort=None to omit it.

Env (deployment-provided): OKENGINE_LLM_BASE_URL (must include the /v1 suffix, e.g.
http://<host>:11436/v1) · OKENGINE_LLM_MODEL · OKENGINE_LLM_API_KEY (optional).

Extensions vendor a copy (self-containment rule), keeping the filename `llm_lib.py` — the
discipline gate allowlists the name, not the path.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 500
DEFAULT_RETRIES = 2
_RETRYABLE_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
)


class LLMError(RuntimeError):
    """A model call failed after retries, or returned something unusable."""


class LLMTruncation(LLMError):
    """finish_reason=length with EMPTY content — the reasoning-ate-the-budget signature.
    Either raise max_tokens, or (if you didn't opt into reasoning) the endpoint ignored
    reasoning_effort:"none" — check the serving layer."""


def _resolve(base_url: str | None, model: str | None) -> tuple[str, str]:
    url = (base_url or os.environ.get("OKENGINE_LLM_BASE_URL", "")).rstrip("/")
    mdl = model or os.environ.get("OKENGINE_LLM_MODEL", "")
    if not url or not mdl:
        raise LLMError("llm_lib: no endpoint/model — pass base_url+model or set "
                       "OKENGINE_LLM_BASE_URL + OKENGINE_LLM_MODEL")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LLMError("llm_lib: endpoint must be an http(s) URL with a host")
    return url, mdl


def normalize_input_items(messages: list[dict]) -> list[dict]:
    """Return Responses input with an explicit type on every role-based message.

    llama.cpp cannot infer the item type for assistant history. Copy items so
    callers retain ownership of their history, and leave typed non-message
    items (function calls, function outputs, reasoning, and similar) intact.
    """
    normalized = []
    for item in messages:
        if not isinstance(item, dict):
            raise LLMError("llm_lib: every Responses input item must be an object")
        copied = dict(item)
        if copied.get("role") is not None and copied.get("type") in (None, "message"):
            copied["type"] = "message"
        normalized.append(copied)
    return normalized


def build_body(messages: list[dict], model: str, *, max_tokens: int = 256,
               temperature: float = 0.0, reasoning_effort: str | None = "none",
               stream: bool = True) -> dict:
    """Build an OpenAI Responses request with the policy applied. Pure and testable.

    reasoning_effort: "none" (DEFAULT — thinking off) · a real effort ("low".."max",
    explicit opt-in) · None (omit the key, for providers that reject it)."""
    body = {
        "model": model,
        "input": normalize_input_items(messages),
        "max_output_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if reasoning_effort is not None:
        body["reasoning"] = {"effort": reasoning_effort}
    return body


def _identity_headers(*, client_id: str | None = None,
                      conversation_id: str | None = None,
                      oneshot: bool = True) -> dict[str, str]:
    """Build inference-pool routing/attribution headers.

    An explicit client id wins. Otherwise deployment identity comes from
    OKENGINE_DEPLOYMENT (falling back to OKENGINE_PACK for compatibility) and
    optional lane identity from OKENGINE_LLM_COMPONENT.
    """
    explicit = client_id or os.environ.get("OKENGINE_LLM_CLIENT_ID", "").strip()
    deployment = os.environ.get("OKENGINE_DEPLOYMENT", "").strip()
    pack = os.environ.get("OKENGINE_PACK", "").strip()
    component = os.environ.get("OKENGINE_LLM_COMPONENT", "").strip()
    identity_name = deployment or pack
    identity = explicit or (f"okengine/{identity_name}" if identity_name else "okengine/unknown")
    if component and not explicit:
        identity = f"{identity}/{component}"
    headers = {"X-Client-Id": identity[:96]}
    conversation = conversation_id or os.environ.get("OKENGINE_LLM_CONVERSATION_ID", "").strip()
    if conversation:
        headers["X-Conversation-Id"] = conversation[:96]
    elif oneshot:
        headers["X-Oneshot"] = "1"
    return headers


def parse_stream(response) -> str:
    """Parse OpenAI Responses SSE output while retaining a completed fallback payload."""
    chunks: list[str] = []
    completed = None
    for raw in response:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        event_type = str(event.get("type") or "")
        if event_type == "response.output_text.delta":
            chunks.append(str(event.get("delta") or ""))
        elif event_type in {"response.completed", "response.incomplete"}:
            completed = event.get("response") or event
    content = "".join(chunks)
    if content:
        return content
    if isinstance(completed, dict):
        return parse_content(completed)
    raise LLMError("llm_lib: streaming response contained no output text")


def parse_content(resp: dict) -> str:
    """Extract Responses output text and expose an exhausted output budget as truncation."""
    chunks = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                chunks.append(str(part.get("text") or ""))
    content = "".join(chunks)
    incomplete = resp.get("status") == "incomplete"
    details = resp.get("incomplete_details") or {}
    exhausted = details.get("reason") in {"max_output_tokens", "length"}
    if not content.strip() and incomplete and exhausted:
        raise LLMTruncation("empty content + finish_reason=length — reasoning consumed the "
                            "token budget before the answer")
    if not content.strip() and not resp.get("output"):
        raise LLMError(f"llm_lib: malformed Responses payload: {str(resp)[:200]}")
    return content


def chat(prompt_or_messages, *, model: str | None = None, base_url: str | None = None,
         api_key: str | None = None, max_tokens: int = 256, temperature: float = 0.0,
         reasoning_effort: str | None = "none", timeout: int = DEFAULT_TIMEOUT,
         retries: int = DEFAULT_RETRIES, stream: bool = True,
         client_id: str | None = None, conversation_id: str | None = None,
         oneshot: bool = True) -> str:
    """One Responses call, policy applied. Accepts a prompt string or a messages list;
    returns the content string. Retries transient transport errors with backoff."""
    url, mdl = _resolve(base_url, model)
    messages = ([{"role": "user", "content": prompt_or_messages}]
                if isinstance(prompt_or_messages, str) else list(prompt_or_messages))
    body = build_body(messages, mdl, max_tokens=max_tokens, temperature=temperature,
                      reasoning_effort=reasoning_effort, stream=stream)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    headers.update(_identity_headers(client_id=client_id, conversation_id=conversation_id,
                                     oneshot=oneshot))
    key = api_key or os.environ.get("OKENGINE_LLM_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{url}/responses",
                                 data=json.dumps(body).encode(), headers=headers)
    last: Exception | None = None
    delay = 2.0
    attempts_made = 0
    for attempt in range(retries + 1):
        attempts_made += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
                return parse_stream(r) if stream else parse_content(json.load(r))
        except LLMError:
            raise                       # a parsed-but-unusable answer won't improve on retry
        except urllib.error.HTTPError as e:
            last = e
            retryable = e.code in {408, 429, 502, 503} or (e.code == 500 and attempt == 0)
            if not retryable or attempt >= retries:
                break
            time.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, 60.0)
        except _RETRYABLE_TRANSPORT_ERRORS as e:
            last = e
            if attempt < retries:
                time.sleep(delay + random.uniform(0, delay))
                delay = min(delay * 2, 60.0)
    raise LLMError(f"llm_lib: call failed after {attempts_made} attempt(s): {last}")


def classify(text: str, labels: list[str], *, uncertain: str = "uncertain",
             model: str | None = None, base_url: str | None = None,
             max_tokens: int = 16, **kw) -> str:
    """Single-label classification — the recurring engine use (dedup #165, relevance #167).
    Returns one of `labels`, else `uncertain` (a model that can't commit defers — never
    guess-parse). Thinking is off (inherits chat()'s default), so the tiny max_tokens is safe."""
    opts = ", ".join(labels)
    prompt = f"{text}\n\nAnswer with exactly one token from: {opts}. If unsure: {uncertain}."
    out = chat(prompt, model=model, base_url=base_url, max_tokens=max_tokens, **kw).lower()
    for lab in labels:
        if lab.lower() in out:
            return lab
    return uncertain
