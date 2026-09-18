import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
MIGRATION = REPO / "migrations" / "m_v0_13_8_v0_14_0_deepseek_v4_1_flash.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("deepseek_v41_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migrates_all_active_legacy_ids_without_changing_qwen(tmp_path):
    migration = _load_migration()
    profiles = tmp_path / ".okengine" / "model-profiles.yaml"
    profiles.parent.mkdir()
    profiles.write_text(
        "profiles:\n"
        "  deepseek-chat:\n"
        "    alias: '@deepseek-chat'\n"
        "    note: keep # previous model: deepseek-v4-pro\n"
        "    prose: don't change # model: deepseek-chat\n"
        "  deepseek: {provider: deepseek, model: deepseek-v4-pro}\n"
        "  flash: {provider: deepseek, model: deepseek-v4-flash}\n"
        "  routed: {provider: openrouter, model: deepseek/deepseek-chat}\n"
        "  qwen: {provider: custom, model: qwen3-coder:30b}\n",
        encoding="utf-8",
    )
    env = tmp_path / ".env"
    env.write_text('export API_SERVER_INFERENCE_MODEL="deepseek-chat" # active\n', encoding="utf-8")
    runtime = tmp_path / ".hermes-data" / "config.yaml"
    runtime.parent.mkdir()
    runtime.write_text("model: {default: deepseek-v4-pro}\n", encoding="utf-8")
    cron_models = tmp_path / ".okengine" / "cron-models.json"
    cron_models.write_text('{"brief": "deepseek-v4-pro"}\n', encoding="utf-8")
    extension_models = tmp_path / ".okengine" / "extension-models.json"
    extension_models.write_text('{"grade": "deepseek-v4-flash"}\n', encoding="utf-8")
    domain_crons = tmp_path / "crons" / "domain-crons.json"
    domain_crons.parent.mkdir()
    domain_crons.write_text(
        '[{"name": "thesis-refresh", "model": "deepseek-v4-flash"}]\n', encoding="utf-8"
    )
    pack_extension = tmp_path / "extensions" / "brief" / "extension.yaml"
    pack_extension.parent.mkdir(parents=True)
    pack_extension.write_text("operations:\n- model: deepseek-v4-pro\n", encoding="utf-8")
    operator_extension = tmp_path / ".okengine" / "extensions" / "grade" / "extension.yaml"
    operator_extension.parent.mkdir(parents=True)
    operator_extension.write_text("operations:\n- model: deepseek-v4-flash\n", encoding="utf-8")
    pack_dropin = tmp_path / "extensions" / "brief" / "crons" / "daily.cron.json"
    pack_dropin.parent.mkdir()
    pack_dropin.write_text('{"model": "deepseek-v4-pro"}\n', encoding="utf-8")
    operator_dropin = (
        tmp_path / ".okengine" / "extensions" / "grade" / "crons" / "daily.cron.json"
    )
    operator_dropin.parent.mkdir()
    operator_dropin.write_text('{"model": "deepseek-v4-flash"}\n', encoding="utf-8")

    changes = migration.apply(tmp_path, False)

    assert len(changes) == 10
    assert profiles.read_text(encoding="utf-8").count("deepseek-flash") == 2
    assert "  deepseek-chat:" in profiles.read_text(encoding="utf-8")
    assert "alias: '@deepseek-chat'" in profiles.read_text(encoding="utf-8")
    assert "# previous model: deepseek-v4-pro" in profiles.read_text(encoding="utf-8")
    assert "don't change # model: deepseek-chat" in profiles.read_text(encoding="utf-8")
    assert "deepseek/deepseek-v4.1-flash" in profiles.read_text(encoding="utf-8")
    assert "qwen3-coder:30b" in profiles.read_text(encoding="utf-8")
    assert env.read_text(encoding="utf-8") == \
        'export API_SERVER_INFERENCE_MODEL="deepseek-flash" # active\n'
    assert "deepseek-flash" in runtime.read_text(encoding="utf-8")
    assert "deepseek-flash" in cron_models.read_text(encoding="utf-8")
    assert "deepseek-flash" in extension_models.read_text(encoding="utf-8")
    assert "deepseek-flash" in domain_crons.read_text(encoding="utf-8")
    assert "deepseek-flash" in pack_extension.read_text(encoding="utf-8")
    assert "deepseek-flash" in operator_extension.read_text(encoding="utf-8")
    assert "deepseek-flash" in pack_dropin.read_text(encoding="utf-8")
    assert "deepseek-flash" in operator_dropin.read_text(encoding="utf-8")


def test_does_not_rewrite_longer_historical_model_ids(tmp_path):
    migration = _load_migration()
    profiles = tmp_path / ".okengine" / "model-profiles.yaml"
    profiles.parent.mkdir()
    original = (
        "models:\n"
        "- deepseek/deepseek-chat-v3.1\n"
        "- deepseek/deepseek-chat:free\n"
        "- deepseek-chat@2025\n"
        "- deepseek-chat+beta\n"
        "- deepseek-chat~1\n"
        "- deepseek-chat#snapshot\n"
        "- deepseek-chat=alias\n"
        "- @deepseek-chat\n"
        "- ollama:deepseek-chat\n"
        "- x@deepseek-chat\n"
        "selected:\n"
        "  model: 'deepseek-chat#snapshot'\n"
        '  default: "deepseek-chat # snapshot"\n'
    )
    profiles.write_text(original, encoding="utf-8")

    changes = migration.apply(tmp_path, False)
    assert changes == []
    assert profiles.read_text(encoding="utf-8") == original


def test_dry_run_reports_but_does_not_mutate(tmp_path):
    migration = _load_migration()
    profiles = tmp_path / ".okengine" / "model-profiles.yaml"
    profiles.parent.mkdir()
    original = "profiles:\n  deepseek: {model: deepseek-v4-pro}\n"
    profiles.write_text(original, encoding="utf-8")

    assert migration.apply(tmp_path, True)
    assert profiles.read_text(encoding="utf-8") == original


def test_current_model_is_idempotent(tmp_path):
    migration = _load_migration()
    profiles = tmp_path / ".okengine" / "model-profiles.yaml"
    profiles.parent.mkdir()
    original = "profiles:\n  deepseek: {model: deepseek-flash}\n"
    profiles.write_text(original, encoding="utf-8")

    assert migration.apply(tmp_path, False) == []
    assert profiles.read_text(encoding="utf-8") == original


def test_yaml_replacement_distinguishes_quotes_comments_and_mapping_boundaries():
    migration = _load_migration()
    original = (
        "# model: deepseek-v4-pro\n"
        "model: deepseek-v4-pro # replace this scalar\n"
        "model: 'deepseek-v4-flash' # preserve quote style\n"
        'default: "deepseek-chat"\n'
        "model: deepseek-chat#snapshot\n"
        "model_name: deepseek-chat\n"
        "note: '# model: deepseek-v4-pro'\n"
    )

    replaced, count = migration._replace_active_values(Path("config.yaml"), original)

    assert count == 3
    assert replaced == (
        "# model: deepseek-v4-pro\n"
        "model: deepseek-flash # replace this scalar\n"
        "model: 'deepseek-flash' # preserve quote style\n"
        'default: "deepseek-flash"\n'
        "model: deepseek-chat#snapshot\n"
        "model_name: deepseek-chat\n"
        "note: '# model: deepseek-v4-pro'\n"
    )


def test_env_replacement_requires_a_model_variable_and_exact_value():
    migration = _load_migration()
    original = (
        "MODEL=deepseek-chat\n"
        "export FALLBACK_MODEL='deepseek-reasoner' # selected\n"
        'API_MODEL="deepseek-v4-pro"\n'
        "NOT_A_PROVIDER=deepseek-chat\n"
        "MODEL=deepseek-chat:free\n"
    )

    replaced, count = migration._replace_active_values(Path(".env"), original)

    assert count == 3
    assert replaced == (
        "MODEL=deepseek-flash\n"
        "export FALLBACK_MODEL='deepseek-flash' # selected\n"
        'API_MODEL="deepseek-flash"\n'
        "NOT_A_PROVIDER=deepseek-chat\n"
        "MODEL=deepseek-chat:free\n"
    )


def test_json_replacement_changes_values_but_not_keys_or_longer_ids():
    migration = _load_migration()
    original = (
        '{"deepseek-chat": "alias", "primary": "deepseek-chat", '
        '"dated": "deepseek-chat-v3.1", "openrouter": '
        '"openrouter/deepseek/deepseek-v4-pro"}\n'
    )

    replaced, count = migration._replace_active_values(Path("cron-models.json"), original)

    assert count == 2
    assert replaced == (
        '{"deepseek-chat": "alias", "primary": "deepseek-flash", '
        '"dated": "deepseek-chat-v3.1", "openrouter": '
        '"openrouter/deepseek/deepseek-v4.1-flash"}\n'
    )


def test_scalar_parser_tracks_quotes_and_stops_at_the_first_real_comment():
    migration = _load_migration()
    source = (
        'note: "quoted # marker", model: deepseek-chat # model: deepseek-v4-pro\n'
        "note:'quoted # marker', default: 'deepseek-reasoner' # model: deepseek-v4-pro\n"
        'note="quoted # marker", model: "deepseek-v4-flash" # model: deepseek-v4-pro\n'
        'note:["quoted # marker"], model: deepseek-v4-pro # model: deepseek-chat\n'
        'note:{"quoted # marker"}, default: deepseek-v4-flash # model: deepseek-chat\n'
        'note,-"quoted # marker", model: deepseek-chat # model: deepseek-v4-pro\n'
    )

    rendered, count = migration._replace_active_values(Path("config.yaml"), source)

    assert count == 6
    assert rendered == (
        'note: "quoted # marker", model: deepseek-flash # model: deepseek-v4-pro\n'
        "note:'quoted # marker', default: 'deepseek-flash' # model: deepseek-v4-pro\n"
        'note="quoted # marker", model: "deepseek-flash" # model: deepseek-v4-pro\n'
        'note:["quoted # marker"], model: deepseek-flash # model: deepseek-chat\n'
        'note:{"quoted # marker"}, default: deepseek-flash # model: deepseek-chat\n'
        'note,-"quoted # marker", model: deepseek-flash # model: deepseek-v4-pro\n'
    )


def test_scalar_parser_distinguishes_env_json_and_yaml_modes():
    migration = _load_migration()

    env, env_count = migration._replace_active_values(
        Path(".env"),
        "MODEL=deepseek-chat\nMODEL_ALT='deepseek-reasoner'\n",
    )
    yaml, yaml_count = migration._replace_active_values(
        Path("!.env"),
        "MODEL=deepseek-chat\nmodel: deepseek-reasoner\n",
    )
    json_text, json_count = migration._replace_active_values(
        Path("models.json"),
        '{"model": "deepseek-chat", "note": "deepseek-v4-pro"}\n',
    )

    assert (env, env_count) == (
        "MODEL=deepseek-flash\nMODEL_ALT='deepseek-flash'\n",
        2,
    )
    assert (yaml, yaml_count) == (
        "MODEL=deepseek-chat\nmodel: deepseek-flash\n",
        1,
    )
    assert (json_text, json_count) == (
        '{"model": "deepseek-flash", "note": "deepseek-flash"}\n',
        2,
    )


def test_scalar_parser_handles_column_zero_quotes_and_comments():
    migration = _load_migration()
    source = (
        '# model: deepseek-chat\n'
        '"quoted # marker", model: deepseek-chat # first # model: deepseek-v4-pro\n'
        "'quoted # marker', default: deepseek-reasoner\t# model: deepseek-v4-pro\n"
    )

    rendered, count = migration._replace_active_values(Path("config.yaml"), source)

    assert count == 2
    assert rendered == (
        '# model: deepseek-chat\n'
        '"quoted # marker", model: deepseek-flash # first # model: deepseek-v4-pro\n'
        "'quoted # marker', default: deepseek-flash\t# model: deepseek-v4-pro\n"
    )


def test_apply_continues_past_unchanged_earlier_active_files(tmp_path):
    migration = _load_migration()
    env = tmp_path / ".env"
    env.write_text("MODEL=deepseek-flash\n", encoding="utf-8")
    profiles = tmp_path / ".okengine" / "model-profiles.yaml"
    profiles.parent.mkdir()
    profiles.write_text("model: deepseek-flash\n", encoding="utf-8")
    runtime = tmp_path / ".hermes-data" / "config.yaml"
    runtime.parent.mkdir()
    runtime.write_text("model: deepseek-v4-pro\n", encoding="utf-8")

    changes = migration.apply(tmp_path, False)

    assert changes == [
        "set 1 DeepSeek model selection(s) in .hermes-data/config.yaml to deepseek-flash"
    ]
    assert runtime.read_text(encoding="utf-8") == "model: deepseek-flash\n"


def test_openrouter_fallback_uses_v41_flash():
    source = (REPO / "plugins/model-providers/openrouter/__init__.py").read_text(encoding="utf-8")
    assert '"deepseek/deepseek-v4.1-flash"' in source
    assert '"deepseek/deepseek-chat"' not in source
