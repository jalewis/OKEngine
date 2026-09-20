"""Fast direct contracts for model/schedule validation without unrelated pack checks."""
import pytest
import json

from scripts import model_profiles
from scripts.framework_validate_models import ModelChecks
from scripts.framework_validate_report import Report
from scripts.cron import deepseek_policy


@pytest.mark.parametrize("definition, expected", [
    ({"schedule": {"expr": " 0 5 * * * "}}, "0 5 * * *"),
    ({"schedule": " 0 5 * * * "}, "0 5 * * *"),
    ({"expr": " 0 5 * * * "}, "0 5 * * *"),
    ({}, ""),
])
def test_model_schedule_expression_forms(definition, expected):
    checks = ModelChecks(model_profiles=lambda: model_profiles)
    assert checks._cron_expr(definition) == expected


def test_model_schedule_step_and_empty_profile_inputs(tmp_path):
    checks = ModelChecks(model_profiles=lambda: model_profiles)
    assert checks._fixed_cron_hours("0 1-5/2 * * *") == [1, 3, 5]
    assert checks._fixed_cron_hours("0 * * * *") == []
    assert checks._collect_model_refs(tmp_path, model_profiles) == set()


def test_model_policy_malformed_runtime_is_rejected_by_owning_parse_check(tmp_path):
    from scripts import framework_validate

    config = tmp_path / ".hermes-data" / "config.yaml"
    config.parent.mkdir()
    config.write_text("[broken")
    report = Report()
    checks = ModelChecks(model_profiles=lambda: model_profiles)
    checks.check_deepseek_model_policy(tmp_path, report)
    framework_validate.check_runtime_config(tmp_path, report)
    assert any(level == "FAIL" and "YAML error" in detail
               for level, _area, detail in report.rows)
    assert any(level == "FAIL" and "cannot inspect" in detail for level, _area, detail in report.rows)
    assert not any(level == "OK" for level, _area, _detail in report.rows)


@pytest.mark.parametrize("relative", [
    ".env", ".hermes-data/.env", ".hermes/.env", "config.yaml", "config.yml",
    ".hermes/config.yaml", ".hermes/config.yml",
    ".hermes-data/config.yaml", ".okengine/model-profiles.yaml",
    ".okengine/cron-models.json", ".okengine/extension-models.json", "crons/domain-crons.json",
    "extensions/classify/extension.yaml", ".okengine/extensions/classify/extension.yaml",
    "extensions/classify/crons/daily.cron.json", ".okengine/extensions/classify/crons/daily.cron.json",
])
@pytest.mark.parametrize("model, fails", [("deepseek-v4-pro", True), ("deepseek-flash", False)])
def test_model_policy_all_migration_surfaces(tmp_path, relative, model, fails):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name == ".env":
        content = f'export OKENGINE_LLM_MODEL="{model}" # retained comment\n'
    elif relative in (".okengine/cron-models.json", ".okengine/extension-models.json"):
        content = json.dumps({"brief": model})
    elif path.suffix == ".json":
        content = json.dumps({"model": model})
    else:
        content = f"operations:\n  classify:\n    model: {model}\n"
    path.write_text(content)
    checks = ModelChecks(model_profiles=lambda: model_profiles)
    report = Report()

    checks.check_deepseek_model_policy(tmp_path, report)

    assert any(level == "FAIL" for level, _area, _detail in report.rows) is fails
    assert path.read_text() == content
    assert path.relative_to(tmp_path) in deepseek_policy.active_config_paths(tmp_path)


@pytest.mark.parametrize("relative, content", [
    (".env", 'OKENGINE_LLM_MODEL="unterminated'),
    ("config.yml", "[broken"), (".okengine/cron-models.json", "{broken"),
])
def test_model_policy_unreadable_surface_never_claims_clean(tmp_path, relative, content):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    report = Report()
    ModelChecks(model_profiles=lambda: model_profiles).check_deepseek_model_policy(tmp_path, report)
    assert report.rows == [("FAIL", "DeepSeek model policy",
                            f"{relative}: cannot inspect active configuration")]


def test_model_policy_environment_keys_and_descriptive_values():
    assert list(deepseek_policy.env_model_values(
        '# MODEL=deepseek-chat\nTOKEN=private\nMODEL="deepseek-flash" # comment\n'
        "export API_SERVER_INFERENCE_MODEL='qwen3-coder:30b'\nEMPTY_MODEL=\n")) == [
            ("MODEL", "deepseek-flash"), ("API_SERVER_INFERENCE_MODEL", "qwen3-coder:30b"),
            ("EMPTY_MODEL", "")]
    assert list(deepseek_policy.model_values({
        "env": {"OKENGINE_LLM_MODEL": "deepseek-chat"},
        "model": {"default": "qwen3-coder:30b"}, "note": "deepseek-v4-pro",
        "fallback_providers": [{"model": "deepseek-flash"}],
    })) == ["deepseek-chat", "qwen3-coder:30b", "deepseek-flash"]


@pytest.mark.parametrize("prefix", ["", "deepseek/", "openrouter/deepseek/"])
@pytest.mark.parametrize("model", ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro",
                                   "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"])
def test_model_policy_every_retired_alias(prefix, model):
    value = prefix + model
    assert deepseek_policy.legacy_deepseek_model(" " + value.upper() + " ") == value


@pytest.mark.parametrize("model", [None, "", "qwen3-coder:30b", "deepseek-flash",
                                   "deepseek/deepseek-v4.1-flash", "deepseek-chat-v3.1"])
def test_model_policy_preserves_nonlegacy_selections(model):
    assert deepseek_policy.legacy_deepseek_model(model) is None
