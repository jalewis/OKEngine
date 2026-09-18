"""Native v0.21.3 OpenRouter replaces the old OKEngine module overlay."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent.transports.chat_completions import ChatCompletionsTransport
from providers.base import ProviderProfile


ENGINE = Path(__file__).resolve().parents[3]
SOURCE = Path.cwd()
NATIVE_FILE = SOURCE / "plugins/model-providers/openrouter/__init__.py"
OLD_FILE = ENGINE / "plugins/model-providers/openrouter/__init__.py"
TARGET_SHA = "345cd2b057a452236de401d3534b8502a7465e8d"


def _load(name: str, source: Path):
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None, f"missing provider source: {source}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


commit = subprocess.run(["git", "-C", str(SOURCE), "rev-parse", "HEAD"],
                        text=True, capture_output=True, check=False)
assert commit.returncode == 0 and commit.stdout.strip() == TARGET_SHA, \
    "OpenRouter contracts require the exact peeled v0.21.3 checkout"
NATIVE = _load("hermes_v0213_openrouter_native", NATIVE_FILE)


def test_native_openrouter_file_is_exact_pinned_artifact():
    result = subprocess.run(["bash", str(ENGINE / "scripts/verify_native_openrouter.sh"), str(SOURCE)],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_native_verifier_rejects_manifest_drift(tmp_path):
    provider = tmp_path / "plugins/model-providers/openrouter"
    provider.mkdir(parents=True)
    (provider / "__init__.py").write_bytes(NATIVE_FILE.read_bytes())
    (provider / "plugin.yaml").write_text("name: replaced\n", encoding="utf-8")
    result = subprocess.run(["bash", str(ENGINE / "scripts/verify_native_openrouter.sh"), str(tmp_path)],
                            text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert "manifest drifted or was replaced" in result.stderr


def test_native_profile_retains_current_catalog_and_real_class():
    profile = NATIVE.openrouter
    assert isinstance(profile, ProviderProfile)
    assert profile.fallback_models[0] == "anthropic/claude-sonnet-4.6"
    assert "google/gemini-3.8-flash" in profile.fallback_models
    assert "openai/gpt-6-astra-fast" in NATIVE.OPENROUTER_ENDPOINT_PINS


def test_native_speed_tier_rewrites_wire_model_and_pins_endpoint():
    profile = NATIVE.openrouter
    body = profile.build_extra_body(model="openai/gpt-6-astra-fast", session_id="session-fixture")
    assert body["provider"]["only"] == ["openai/fast"]
    wire = ChatCompletionsTransport().build_kwargs(
        "openai/gpt-6-astra-fast", [{"role": "user", "content": "fixture"}],
        provider_profile=profile, base_url=profile.base_url, session_id="session-fixture",
    )
    assert wire["model"] == "openai/gpt-6-astra"
    assert wire["extra_body"]["provider"]["only"] == ["openai/fast"]


def test_native_base_tier_respects_explicit_provider_only():
    profile = NATIVE.openrouter
    body = profile.build_extra_body(
        model="openai/gpt-6-astra", provider_preferences={"only": ["approved-endpoint"]},
    )
    assert body["provider"]["only"] == ["approved-endpoint"]


def test_native_affinity_scopes_session_and_grok_header_consistently():
    profile = NATIVE.openrouter
    body = profile.build_extra_body(session_id="session-fixture", model="x-ai/grok-4.6")
    extra, top = profile.build_api_kwargs_extras(
        session_id="session-fixture", model="x-ai/grok-4.6",
    )
    assert extra == {}
    assert body["session_id"] == top["extra_headers"]["x-grok-conv-id"]


def test_native_mandatory_anthropic_reasoning_omits_unsafe_field():
    profile = NATIVE.openrouter
    extra, top = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": False, "effort": "none"},
        supports_reasoning=True, model="anthropic/claude-sonnet-4.6",
    )
    assert extra == {}
    assert top == {}
    enabled_extra, enabled_top = profile.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"},
        supports_reasoning=True, model="anthropic/claude-sonnet-4.6",
    )
    assert enabled_extra == {}
    assert enabled_top == {"verbosity": "high"}


def test_native_catalog_effort_clamps_at_real_boundary():
    from hermes_cli.models_reasoning_caps import openrouter_model_reasoning_capabilities

    with patch("hermes_cli.models_reasoning_caps.openrouter_model_reasoning_capabilities",
               autospec=True, return_value={"supports_reasoning": True, "mandatory": False,
                                            "supported_efforts": ["low", "medium", "high"]}) as caps:
        extra, _top = NATIVE.openrouter.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "ultra"},
            supports_reasoning=True, model="fixture/reasoning-model",
        )
    assert extra["reasoning"]["effort"] == "high"
    caps.assert_called_once_with("fixture/reasoning-model")
    assert callable(openrouter_model_reasoning_capabilities)


def test_native_fetch_models_keeps_target_base_url_and_public_catalog():
    NATIVE._CACHE = None
    with patch.object(ProviderProfile, "fetch_models", autospec=True,
                      return_value=["fixture-model"]) as fetch:
        assert NATIVE.openrouter.fetch_models(
            api_key="fixture-key", base_url="https://proxy.example.test/v1", timeout=4.0,
        ) == ["fixture-model"]
    fetch.assert_called_once_with(NATIVE.openrouter, api_key=None,
                                  base_url="https://proxy.example.test/v1", timeout=4.0)


def test_native_pareto_plugin_is_model_gated():
    profile = NATIVE.openrouter
    pareto = profile.build_extra_body(model="openrouter/pareto-code",
                                      openrouter_min_coding_score=0.7)
    ordinary = profile.build_extra_body(model="anthropic/claude-sonnet-4.6",
                                        openrouter_min_coding_score=0.7)
    assert pareto["plugins"] == [{"id": "pareto-router", "min_coding_score": 0.7}]
    assert "plugins" not in ordinary


def test_old_overlay_negative_missing_speed_pin_and_mandatory_guard():
    old = _load("okengine_old_openrouter_profile", OLD_FILE)
    old_body = old.openrouter.build_extra_body(model="openai/gpt-6-astra-fast")
    old_extra, _old_top = old.openrouter.build_api_kwargs_extras(
        reasoning_config={"enabled": False, "effort": "none"},
        supports_reasoning=True, model="anthropic/claude-sonnet-4.6",
    )
    assert "provider" not in old_body
    assert old_extra["reasoning"] == {"enabled": False, "effort": "none"}
    native_body = NATIVE.openrouter.build_extra_body(model="openai/gpt-6-astra-fast")
    native_extra, _native_top = NATIVE.openrouter.build_api_kwargs_extras(
        reasoning_config={"enabled": False, "effort": "none"},
        supports_reasoning=True, model="anthropic/claude-sonnet-4.6",
    )
    assert native_body["provider"]["only"] == ["openai/fast"]
    assert native_extra == {}
