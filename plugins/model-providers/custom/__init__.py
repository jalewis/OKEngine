"""Custom / Ollama (local) provider profile.

Covers any endpoint registered as provider="custom", including local
Ollama instances. Key quirks:
  - ollama_num_ctx → extra_body.options.num_ctx (local context window)
  - reasoning_config disabled → extra_body.think = False
  - reasoning_config enabled  → top-level reasoning_effort (the /v1 path)
  - a low default temperature (local instruct models default to ~1.0)
"""

import os
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

# Local instruct models ship high sampling defaults (gemma: temperature 1.0,
# top_k 64, top_p 0.95) and Hermes' agent loop sends no temperature of its own,
# so an agentic cron lane runs at the model default. Over a 30+ tool-call
# trajectory that entropy compounds into contract drift — the lane stops
# emitting the structured receipt it was told to emit.
#
# Hosted providers do not need this (their defaults are tuned for assistant use
# and their instruction-following is stronger), so it is scoped to the local
# profile. Override with OKENGINE_LOCAL_TEMPERATURE; set it empty to restore the
# previous behaviour of deferring to the caller/model default.
_DEFAULT_LOCAL_TEMPERATURE = "0.2"


def _local_temperature() -> float | None:
    """Parse OKENGINE_LOCAL_TEMPERATURE; None => defer to the caller/model."""
    raw = os.environ.get("OKENGINE_LOCAL_TEMPERATURE", _DEFAULT_LOCAL_TEMPERATURE)
    raw = (raw or "").strip()
    if not raw or raw.lower() in {"default", "caller", "none"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        return float(_DEFAULT_LOCAL_TEMPERATURE)
    # Clamp rather than reject: a nonsensical value must not brick every local write.
    return max(0.0, min(value, 2.0))


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think toggle, num_ctx and sampling support."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        ollama_num_ctx: int | None = None,
        **ctx: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Ollama context window
        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        # Disable thinking for local Ollama models BY DEFAULT. Reasoning models
        # (qwen3.x etc.) otherwise spend the whole output budget on <think> and
        # return empty content + finish_reason=length, which the agent loop reads
        # as truncation -> dead continuation retries -> job failure (the local
        # fallback's documented failure mode). Thinking stays on only when
        # reasoning is EXPLICITLY enabled with a real effort.
        #
        # Ollama's OpenAI-compatible /v1 endpoint (what Hermes uses) IGNORES the
        # native `think` field but DOES honor top-level `reasoning_effort`, so we
        # send BOTH: `think` (extra_body, native /api path) and `reasoning_effort`
        # (top-level, the /v1 path that actually works).
        thinking_off = True
        effort = ""
        if reasoning_config and isinstance(reasoning_config, dict):
            effort = (reasoning_config.get("effort") or "").strip().lower()
            if reasoning_config.get("enabled") is True and effort in {
                "low", "medium", "high", "xhigh", "max"
            }:
                thinking_off = False
        if thinking_off:
            extra_body["think"] = False
            top_level["reasoning_effort"] = "none"
        else:
            # This branch previously emitted NOTHING, which made
            # `agent.reasoning_effort: high` a silent no-op for local models:
            # thinking-off was simply not requested and the model fell back to its
            # own default. Send the enable form on BOTH paths, mirroring above.
            extra_body["think"] = True
            top_level["reasoning_effort"] = effort

        return extra_body, top_level

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Custom/Ollama: base_url is user-configured; fetch if set."""
        if not self.base_url:
            return None
        return super().fetch_models(api_key=api_key, timeout=timeout)


custom = CustomProfile(
    name="custom",
    aliases=(
        "ollama",
        "local",
        "vllm",
        "llamacpp",
        "llama.cpp",
        "llama-cpp",
    ),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    fixed_temperature=_local_temperature(),
)

register_provider(custom)
