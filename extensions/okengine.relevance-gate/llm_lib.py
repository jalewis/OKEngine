"""llm_lib — the ONE sanctioned direct-LLM-call path for engine/pack scripts.

Why this exists (the blind-spot fix): gateway agent lanes inherit thinking policy from the
Hermes provider profiles (plugins/model-providers/custom disables qwen thinking by default;
deepseek keeps V4 reasoning). A script that curls the model endpoint DIRECTLY bypasses that
layer entirely — a qwen3.x reasoning model then spends the whole max_tokens budget thinking
and returns content='' + finish_reason=length, which reads as failure (a real 90-page bulk
classify returned 88 false-"uncertain" exactly this way). A policy enforced in one client is
not a policy; so direct calls get ONE blessed path with the policy baked in, and
tests/test_llm_call_discipline.py FAILS the build on raw chat-completions calls anywhere else
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
import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 120
DEFAULT_RETRIES = 2


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
    return url, mdl


def build_body(messages: list[dict], model: str, *, max_tokens: int = 256,
               temperature: float = 0.0, reasoning_effort: str | None = "none") -> dict:
    """The request body with the policy applied. Pure — unit-testable.

    reasoning_effort: "none" (DEFAULT — thinking off) · a real effort ("low".."max",
    explicit opt-in) · None (omit the key, for providers that reject it)."""
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    return body


def parse_content(resp: dict) -> str:
    """Extract message content; raise LLMTruncation on the thinking-truncation signature."""
    try:
        choice = resp["choices"][0]
    except (KeyError, IndexError) as e:
        raise LLMError(f"llm_lib: malformed response: {str(resp)[:200]}") from e
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip() and choice.get("finish_reason") == "length":
        raise LLMTruncation("empty content + finish_reason=length — reasoning consumed the "
                            "token budget before the answer")
    return content


def chat(prompt_or_messages, *, model: str | None = None, base_url: str | None = None,
         api_key: str | None = None, max_tokens: int = 256, temperature: float = 0.0,
         reasoning_effort: str | None = "none", timeout: int = DEFAULT_TIMEOUT,
         retries: int = DEFAULT_RETRIES) -> str:
    """One chat completion, policy applied. Accepts a prompt string or a messages list;
    returns the content string. Retries transient transport errors with backoff."""
    url, mdl = _resolve(base_url, model)
    messages = ([{"role": "user", "content": prompt_or_messages}]
                if isinstance(prompt_or_messages, str) else list(prompt_or_messages))
    body = build_body(messages, mdl, max_tokens=max_tokens, temperature=temperature,
                      reasoning_effort=reasoning_effort)
    headers = {"Content-Type": "application/json"}
    key = api_key or os.environ.get("OKENGINE_LLM_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{url}/chat/completions",
                                 data=json.dumps(body).encode(), headers=headers)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return parse_content(json.load(r))
        except LLMError:
            raise                       # a parsed-but-unusable answer won't improve on retry
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise LLMError(f"llm_lib: call failed after {retries + 1} attempt(s): {last}")


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
