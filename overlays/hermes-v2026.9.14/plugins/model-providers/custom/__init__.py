"""v0.21.3 custom profile with OKEngine's endpoint-scoped local policy.

The provider name covers Ollama, llama.cpp, vLLM, and hosted OpenAI-compatible
routes. Ollama-native body fields must never be sent merely because the route
uses provider="custom".
"""

import math
import os
from typing import Any
from urllib.parse import urlparse

from agent.reasoning_effort import OPENAI_COMPAT_WIRE_EFFORTS, clamp_effort
from providers import register_provider
from providers.base import ProviderProfile


_DEFAULT_LOCAL_TEMPERATURE = 0.2


def _local_temperature() -> float | None:
    """None restores the model/caller default; invalid finite values use 0.2."""
    raw = (os.getenv("OKENGINE_LOCAL_TEMPERATURE", "0.2") or "").strip()
    if not raw or raw.lower() in {"default", "caller", "none"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_LOCAL_TEMPERATURE
    if not math.isfinite(value):
        return _DEFAULT_LOCAL_TEMPERATURE
    return max(0.0, min(value, 2.0))


def _looks_like_ollama_endpoint(base_url: str | None) -> bool:
    """True only for explicit Ollama signatures (port 11434 or an ``ollama`` host label).
    ``think`` is Ollama-native; strict hosts (Mistral, Groq) 422 on it, and
    arbitrary localhost may be llama.cpp / vLLM / LM Studio."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    try:  # urlparse raises ValueError on malformed ports ("host:99999"); treat as not-Ollama.
        if parsed.port == 11434:
            return True
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and (host == "ollama.com" or host.endswith(".ollama.com") or "ollama" in host.split("."))


class CustomProfile(ProviderProfile):
    """Local sampling and Ollama-only body fields on their actual endpoint."""

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, ollama_num_ctx: int | None = None, **ctx: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        base_url = ctx.get("base_url")
        base_url = base_url if isinstance(base_url, str) else ""
        ollama = _looks_like_ollama_endpoint(base_url)
        # Lazy import avoids a provider/model-metadata initialization cycle.
        from agent.model_metadata import is_local_endpoint
        if is_local_endpoint(base_url):
            temperature = _local_temperature()
            if temperature is not None:
                top_level["temperature"] = temperature
        if ollama and isinstance(ollama_num_ctx, int) and not isinstance(ollama_num_ctx, bool) and ollama_num_ctx > 0:
            extra_body["options"] = {"num_ctx": ollama_num_ctx}
        # disabled -> top-level reasoning_effort="none" (Ollama's /v1 ignores
        # extra_body.think) plus think=False only on Ollama URLs; enabled+effort ->
        # top-level reasoning_effort clamped to the OpenAI-compat wire (GLM/ARK,
        # vLLM and SGLang all top out at "max"; "ultra" verbatim 400s); enabled
        # without effort -> omit so the server default applies. Never emit
        # think=True (Ollama-only flag).
        if isinstance(reasoning_config, dict):
            raw_effort = reasoning_config.get("effort")
            effort = raw_effort.strip().lower() if isinstance(raw_effort, str) else ""
            if effort == "none" or reasoning_config.get("enabled", True) is False:
                # See #14820.
                top_level["reasoning_effort"] = "none"
                if ollama:
                    extra_body["think"] = False
            elif effort:
                top_level["reasoning_effort"] = clamp_effort(effort, OPENAI_COMPAT_WIRE_EFFORTS)
            elif ollama:
                # OKEngine's local Ollama default: no unrequested thinking.
                # A real effort above intentionally leaves it enabled.
                top_level["reasoning_effort"] = "none"
                extra_body["think"] = False
        elif ollama:
            top_level["reasoning_effort"] = "none"
            extra_body["think"] = False
        return extra_body, top_level

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """base_url is user-configured; fetch only if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom", aliases=("ollama", "local", "vllm", "llamacpp", "llama.cpp", "llama-cpp"),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # An arbitrary client ceiling can exceed a local server's actual output limit.
    # The endpoint owns its generation default.
)

register_provider(custom)
