"""Local (custom/ollama) provider profile — sampling and reasoning contract.

Two defects this pins, both found by wire-capturing what the gateway actually sent
to ollama during an entity-backfill run (2026-07-24/25):

1. **Reasoning could not be enabled.** The enable branch emitted NOTHING, so
   `agent.reasoning_effort: high` was a silent no-op for local models — thinking-off
   simply was not requested and the model fell back to its own default. Confirmed on
   the wire: neither `think` nor `reasoning_effort` present.

2. **No temperature was ever sent**, so agentic lanes ran at the model default
   (gemma: 1.0). Over a 30+ tool-call trajectory that entropy compounds into contract
   drift. Hermes applies `profile.fixed_temperature` in the main agent transport and
   otherwise falls through to "caller's temperature if provided" — which is never.

Both directions are asserted: disable must still disable, and the override must still
be able to restore the previous defer-to-caller behaviour.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "plugins" / "model-providers" / "custom" / "__init__.py"


def load_profile(monkeypatch, temp_env=None):
    """Load the profile with Hermes' `providers` package stubbed.

    The profile imports `providers` / `providers.base`, which live in the Hermes
    image rather than this repo, so the test supplies the minimal surface it uses.
    """
    if temp_env is None:
        monkeypatch.delenv("OKENGINE_LOCAL_TEMPERATURE", raising=False)
    else:
        monkeypatch.setenv("OKENGINE_LOCAL_TEMPERATURE", temp_env)

    class _ProviderProfile:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

        def fetch_models(self, *, api_key=None, timeout=8.0):
            return [f"{api_key}:{timeout}"]

    providers = types.ModuleType("providers")
    providers.register_provider = lambda profile: None
    base = types.ModuleType("providers.base")
    base.ProviderProfile = _ProviderProfile
    providers.base = base
    monkeypatch.setitem(sys.modules, "providers", providers)
    monkeypatch.setitem(sys.modules, "providers.base", base)

    spec = importlib.util.spec_from_file_location("custom_profile_under_test", MOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── temperature ───────────────────────────────────────────────────────────────

def test_local_profile_pins_a_low_default_temperature(monkeypatch):
    mod = load_profile(monkeypatch)
    assert mod.custom.fixed_temperature == 0.2, (
        "local models default to ~1.0; an unpinned agentic lane drifts over a long "
        "trajectory"
    )


def test_temperature_is_overridable(monkeypatch):
    assert load_profile(monkeypatch, "0.05").custom.fixed_temperature == 0.05
    assert load_profile(monkeypatch, "0.7").custom.fixed_temperature == 0.7


def test_empty_override_restores_defer_to_caller(monkeypatch):
    """An explicit escape hatch back to the previous behaviour."""
    for value in ("", "   ", "default", "caller", "none"):
        assert load_profile(monkeypatch, value).custom.fixed_temperature is None, value


def test_absurd_or_malformed_temperature_never_bricks_local_writes(monkeypatch):
    """Clamp/fall back rather than raise — a bad env var must not break every lane."""
    assert load_profile(monkeypatch, "not-a-number").custom.fixed_temperature == 0.2
    assert load_profile(monkeypatch, "-5").custom.fixed_temperature == 0.0
    assert load_profile(monkeypatch, "99").custom.fixed_temperature == 2.0


# ── reasoning: disable ────────────────────────────────────────────────────────

def test_reasoning_disabled_by_default_on_both_paths(monkeypatch):
    mod = load_profile(monkeypatch)
    extra, top = mod.custom.build_api_kwargs_extras()
    assert extra["think"] is False, "native /api path"
    assert top["reasoning_effort"] == "none", "OpenAI /v1 path"


def test_reasoning_disabled_when_explicitly_off(monkeypatch):
    mod = load_profile(monkeypatch)
    for cfg in ({"enabled": False}, {"enabled": True, "effort": ""},
                {"enabled": True, "effort": "bogus"}, {}):
        extra, top = mod.custom.build_api_kwargs_extras(reasoning_config=cfg)
        assert extra["think"] is False and top["reasoning_effort"] == "none", cfg


# ── reasoning: enable (the regression) ────────────────────────────────────────

def test_reasoning_enable_actually_emits_the_effort(monkeypatch):
    """Regression: this branch previously emitted NOTHING, making
    `agent.reasoning_effort` a silent no-op for every local model."""
    mod = load_profile(monkeypatch)
    for effort in ("low", "medium", "high", "xhigh", "max"):
        extra, top = mod.custom.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort})
        assert top.get("reasoning_effort") == effort, f"{effort} not sent on /v1"
        assert extra.get("think") is True, f"{effort} not sent on the native path"
        assert top.get("reasoning_effort") != "none"


def test_enable_is_case_and_whitespace_tolerant(monkeypatch):
    mod = load_profile(monkeypatch)
    extra, top = mod.custom.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "  HIGH  "})
    assert top["reasoning_effort"] == "high" and extra["think"] is True


# ── num_ctx passthrough (unchanged behaviour) ────────────────────────────────

def test_num_ctx_is_passed_through_as_an_ollama_option(monkeypatch):
    mod = load_profile(monkeypatch)
    extra, _ = mod.custom.build_api_kwargs_extras(ollama_num_ctx=65536)
    assert extra["options"]["num_ctx"] == 65536


def test_num_ctx_absent_when_unset(monkeypatch):
    mod = load_profile(monkeypatch)
    extra, _ = mod.custom.build_api_kwargs_extras()
    assert "options" not in extra


def test_fetch_models_requires_url_and_delegates_when_configured(monkeypatch):
    mod = load_profile(monkeypatch)
    assert mod.custom.fetch_models() is None
    mod.custom.base_url = "http://localhost:11434/v1"
    assert mod.custom.fetch_models(api_key="key", timeout=2.5) == ["key:2.5"]
