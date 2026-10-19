"""okengine#983: no model-facing platform toolset may reach the actor-less `okengine-write`.

The write path treats a caller with no OKENGINE_WRITE_ACTOR as admin. Hermes binds MCP servers to
a platform by `platform_toolsets.<platform>`: `no_mcp` disables them, a list naming registered
servers allows exactly those, and any other list (or no entry at all) binds EVERY server. The rule
lives in hardening_lib (pure) and runs at two gates: `framework validate` before the deploy and
the in-gateway deployment check after it. The CLI is exempt by an explicit, named rule.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import hardening_lib as hl  # noqa: E402

_WRITER = {"command": "/opt/hermes/.venv/bin/python",
           "args": ["/opt/hermes/okengine-mcp/write_server.py"]}


def _servers(**extra) -> dict:
    servers = {
        "okengine": {"url": "http://okengine-mcp:8730/mcp"},
        "okengine-write": dict(_WRITER, env={"OKENGINE_POLICY_CATALOG": "c"}),
        "okengine-write-chat": dict(_WRITER, env={"OKENGINE_WRITE_ACTOR": "chat:api_server"}),
    }
    servers.update(extra)
    return servers


def _cfg(platform_toolsets=None, **extra) -> dict:
    cfg = {"mcp_servers": _servers()}
    if platform_toolsets is not None:
        cfg["platform_toolsets"] = platform_toolsets
    cfg.update(extra)
    return cfg


# --- the rule ------------------------------------------------------------------------------


def test_the_shipped_template_binds_no_actorless_writer_even_with_chat_on():
    cfg = yaml.safe_load((REPO / "config" / "config.yaml.template").read_text())
    assert hl.actorless_writer_bindings(cfg, chat_enabled=True) == []
    assert cfg["platform_toolsets"]["api_server"] == ["okengine", hl.CHAT_WRITER]
    env = cfg["mcp_servers"][hl.CHAT_WRITER]["env"]
    assert env["OKENGINE_WRITE_ACTOR"] == hl.CHAT_ACTOR


def test_negative_fixture_the_pre_983_api_server_toolset_fails():
    """The live configuration on every deployment before this fix."""
    errors = hl.actorless_writer_bindings(
        _cfg({"cron": ["hermes-cron", "no_mcp"], "api_server": ["okengine", "okengine-write"]}),
        chat_enabled=True)
    assert len(errors) == 1 and "'api_server'" in errors[0] and "okengine-write" in errors[0]
    assert "okengine#983" in errors[0]


def test_a_list_naming_no_mcp_server_binds_every_server():
    """`api_server: [web]` names no MCP server, so Hermes hands it all of them."""
    errors = hl.actorless_writer_bindings(_cfg({"api_server": ["web"]}))
    assert errors and "platform_toolsets.api_server = ['web']" in errors[0]


def test_api_server_unset_while_chat_is_enabled_fails():
    errors = hl.actorless_writer_bindings(_cfg({"cron": ["hermes-cron", "no_mcp"]}),
                                          chat_enabled=True)
    assert errors and "no platform_toolsets entry" in errors[0]


def test_api_server_unset_while_chat_is_off_is_not_judged():
    assert hl.actorless_writer_bindings(_cfg({"cron": ["hermes-cron", "no_mcp"]})) == []


def test_a_non_list_toolset_value_fails_like_an_unset_one():
    errors = hl.actorless_writer_bindings(_cfg({"api_server": "okengine"}))
    assert errors and "platform_toolsets.api_server = 'okengine'" in errors[0]


def test_an_enabled_platform_without_a_toolset_entry_fails():
    errors = hl.actorless_writer_bindings(_cfg({}, platforms={"telegram": {"enabled": True},
                                                              "discord": {"enabled": False}}))
    assert len(errors) == 1 and "'telegram'" in errors[0]


def test_no_mcp_disables_every_server():
    assert hl.actorless_writer_bindings(_cfg({"cron": ["hermes-cron", "no_mcp"],
                                              "api_server": ["okengine-write", "no_mcp"]})) == []


def test_the_actor_bound_chat_writer_passes():
    assert hl.actorless_writer_bindings(
        _cfg({"api_server": ["okengine", "okengine-write-chat", "web"]}), chat_enabled=True) == []


def test_the_cli_is_exempt_by_its_named_rule_not_by_omission():
    assert "cli" in hl.OPERATOR_SURFACES and "docker exec" in hl.OPERATOR_SURFACES["cli"]
    assert hl.actorless_writer_bindings(_cfg({"cli": ["okengine", "okengine-write"]})) == []
    assert hl.actorless_writer_bindings(_cfg({}, platforms={"cli": {"enabled": True}})) == []


def test_any_actorless_write_server_counts_not_just_the_generic_name():
    cfg = _cfg({"api_server": ["okengine", "my-writer"]})
    cfg["mcp_servers"]["my-writer"] = dict(_WRITER, env={"OKENGINE_WRITE_ACTOR": "  "})
    errors = hl.actorless_writer_bindings(cfg, chat_enabled=True)
    assert errors and "['my-writer']" in errors[0]


def test_no_actorless_writer_registered_means_nothing_to_reach():
    cfg = {"mcp_servers": {"okengine-write-chat": dict(_WRITER, env={
        "OKENGINE_WRITE_ACTOR": "chat:api_server"})}, "platform_toolsets": {"api_server": ["web"]}}
    assert hl.actorless_writer_bindings(cfg, chat_enabled=True) == []


def test_the_generic_writer_counts_by_name_even_without_write_server_args():
    name = "".join(["okengine-", "write"])          # not the interned literal: `==`, not `is`
    cfg = {"mcp_servers": {name: {"command": "/custom/launcher"}},
           "platform_toolsets": {"api_server": [name]}}
    errors = hl.actorless_writer_bindings(cfg)
    assert len(errors) == 1 and "['okengine-write']" in errors[0], errors
    renamed = {"mcp_servers": {"okengine-writf": {"command": "x"}},
               "platform_toolsets": {"api_server": ["okengine-writf"]}}
    assert hl.actorless_writer_bindings(renamed) == []


def test_an_exempt_operator_surface_does_not_stop_later_platforms_being_checked():
    errors = hl.actorless_writer_bindings(_cfg({"cli": ["okengine-write"],
                                                "telegram": ["okengine-write"]}))
    assert len(errors) == 1 and "'telegram'" in errors[0], errors


def test_an_explicit_null_toolset_reads_as_no_entry():
    errors = hl.actorless_writer_bindings(_cfg({"api_server": None}))
    assert errors and "no platform_toolsets entry" in errors[0], errors


def test_chat_enabled_is_keyword_only():
    with pytest.raises(TypeError):
        hl.actorless_writer_bindings(_cfg({}), True)  # type: ignore[misc]


@pytest.mark.parametrize("cfg", [{}, {"mcp_servers": "x"}, {"mcp_servers": {"bad": None}},
                                 {"mcp_servers": {"okengine-write": {"env": "x"}},
                                  "platform_toolsets": "x", "platforms": "x"}])
def test_malformed_shapes_never_crash(cfg):
    assert isinstance(hl.actorless_writer_bindings(cfg, chat_enabled=True), list)


# --- the pre-deploy gate: framework validate ------------------------------------------------


def _fvr():
    spec = importlib.util.spec_from_file_location(
        "fvr_983", REPO / "scripts" / "framework_validate_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Report:
    def __init__(self):
        self.fails, self.oks, self.warns = [], [], []

    def fail(self, name, detail):
        self.fails.append(detail)

    def ok(self, name, detail=""):
        self.oks.append(detail)

    def warn(self, name, detail=""):
        self.warns.append(detail)


@pytest.mark.parametrize(
    ("dotenv", "compose", "platforms"),
    [("API_SERVER_ENABLED=true\n", None, None),
     ("API_SERVER_KEY=sk-x\n", None, None),
     ("", "services:\n  gateway:\n    environment:\n      - API_SERVER_ENABLED=true\n", None),
     ("", None, {"api_server": {"enabled": True}})],
)
def test_framework_validate_detects_chat_enablement(tmp_path, dotenv, compose, platforms):
    (tmp_path / ".env").write_text(dotenv)
    if compose:
        (tmp_path / "docker-compose.yml").write_text(compose)
    cfg = _cfg({"cron": ["hermes-cron", "no_mcp"]})
    if platforms:
        cfg["platforms"] = platforms
    fvr = _fvr()
    assert fvr._chat_enabled(tmp_path, cfg) is True
    report = _Report()
    fvr.check_chat_writer_binding(tmp_path, report, cfg)
    assert report.fails and "no platform_toolsets entry" in report.fails[0]


def test_framework_validate_ignores_a_commented_compose_enablement(tmp_path):
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  gateway:\n    environment:\n      # - API_SERVER_ENABLED=true\n")
    assert _fvr()._chat_enabled(tmp_path, {}) is False


def test_framework_validate_passes_and_names_the_exemption(tmp_path):
    report = _Report()
    _fvr().check_chat_writer_binding(
        tmp_path, report, _cfg({"api_server": ["okengine", "okengine-write-chat"]}))
    assert report.fails == [] and "exempt operator surfaces: cli" in report.oks[0]


def test_validate_does_not_dead_end_the_upgrade_ensure_runtime_performs(tmp_path):
    """deploy.sh runs validate (step 1) before ensure-runtime (step 2), and ensure-runtime is what
    migrates a pre-#983 api_server list. Failing on that list stopped every existing deployment's
    upgrade before the migration could run (v0.15.3 release audit)."""
    report = _Report()
    _fvr().check_chat_writer_binding(
        tmp_path, report,
        _cfg({"cron": ["hermes-cron", "no_mcp"], "api_server": ["okengine", "okengine-write"]}))
    assert report.fails == []
    assert len(report.warns) == 1 and "pending: deploy.sh step 2" in report.warns[0]
    assert "'api_server'" in report.warns[0]


def test_a_pending_migration_onto_an_already_listed_chat_writer_is_not_a_duplicate(tmp_path):
    """A list naming both writers: ensure-runtime drops the actor-less one (the chat writer is
    already there), so the modeled result binds the chat writer once and the gate only WARNs."""
    fvr = _fvr()
    cfg = _cfg({"api_server": ["okengine", "okengine-write", "okengine-write-chat"]})
    migrated = fvr._api_server_after_migration(cfg)
    assert migrated["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]
    report = _Report()
    fvr.check_chat_writer_binding(tmp_path, report, cfg)
    assert report.fails == [] and len(report.warns) == 1


@pytest.mark.parametrize("platform_toolsets", [
    {"api_server": None},                                  # no list for ensure-runtime to edit
    {"api_server": "okengine-write"},                      # a scalar, not a list
    {"api_server": ["okengine-write-chat"], "telegram": ["okengine-write"]},  # another platform
])
def test_validate_still_fails_a_binding_ensure_runtime_does_not_migrate(tmp_path,
                                                                        platform_toolsets):
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg(platform_toolsets))
    assert report.fails and report.warns == [], (report.fails, report.warns)


def test_a_list_still_bound_after_the_migration_fails_rather_than_warns(tmp_path):
    """ensure-runtime swaps only `okengine-write`. A second actor-less writer on the same list
    survives it, so the earliest gate must FAIL (a message compare once excused it as pending)."""
    cfg = _cfg({"api_server": ["okengine", "okengine-write", "okengine-write-x"]})
    cfg["mcp_servers"]["okengine-write-x"] = dict(_WRITER, env={})
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, cfg)
    assert report.warns == [] and len(report.fails) == 1, (report.fails, report.warns)
    assert "okengine-write-x" in report.fails[0]


def test_actorless_writer_platforms_is_the_structured_form_of_the_messages():
    cfg = _cfg({"api_server": ["okengine", "okengine-write"], "telegram": None,
                "cron": ["hermes-cron", "no_mcp"]})
    platforms = hl.actorless_writer_platforms(cfg)
    assert platforms == {"api_server": (["okengine-write"], ["okengine", "okengine-write"]),
                         "telegram": (["okengine-write"], None)}
    assert [m.split("'")[1] for m in hl.actorless_writer_bindings(cfg)] == list(platforms)


def test_a_pending_api_server_migration_does_not_excuse_another_platform(tmp_path):
    report = _Report()
    _fvr().check_chat_writer_binding(
        tmp_path, report,
        _cfg({"api_server": ["okengine", "okengine-write"], "telegram": ["okengine-write"]}))
    assert len(report.fails) == 1 and "'telegram'" in report.fails[0]
    assert len(report.warns) == 1 and "'api_server'" in report.warns[0]


def test_framework_validate_runtime_config_runs_the_binding_check(tmp_path):
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    # A binding ensure-runtime does not migrate (the api_server list it does is a pending WARN).
    cfg = _cfg({"telegram": ["okengine", "okengine-write"]}, terminal={"backend": "local"})
    cfg["mcp_servers"]["okengine-write-source-quality"] = {}
    (rt / "config.yaml").write_text(yaml.safe_dump(cfg))

    class Report(_Report):
        def warn(self, *a):
            pass

        def info(self, *a):
            pass

    class Profiles:
        @staticmethod
        def validate_qwen_no_fallback(_data):
            return []

    report = Report()
    _fvr().check_runtime_config(tmp_path, report, yaml_module=yaml,
                                model_profiles=lambda: Profiles,
                                runtime_gitignored=lambda _p: False)
    assert any("actor-less" in f for f in report.fails), report.fails


# --- the post-roll gate: deployment checks ---------------------------------------------------


def _checks(tmp_path, monkeypatch, cfg):
    spec = importlib.util.spec_from_file_location(
        "deployment_checks_983", REPO / "scripts" / "cron" / "deployment_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    module.configure(tmp_path, data, tmp_path / "hermes")
    for key in ("OKENGINE_HARDENED", "OKENGINE_EDITING", "API_SERVER_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    module.reset()
    module.check_auth()
    return [(lvl, msg) for lvl, area, msg in module.F if area == "auth"]


def test_deployment_check_fails_the_pre_983_toolset(tmp_path, monkeypatch):
    found = _checks(tmp_path, monkeypatch, _cfg({"api_server": ["okengine", "okengine-write"]}))
    fails = [m for lvl, m in found if lvl == "FAIL"]
    assert any("actor-less" in m for m in fails), found
    assert any("non-allowlisted member(s) ['okengine-write']" in m for m in fails), found
    assert any(lvl == "INFO" and "'cli' is an operator surface" in m for lvl, m in found)


def test_deployment_check_passes_the_migrated_toolset(tmp_path, monkeypatch):
    found = _checks(tmp_path, monkeypatch,
                    _cfg({"api_server": ["okengine", "okengine-write-chat"]}))
    assert not [m for lvl, m in found if lvl == "FAIL"], found


def test_deployment_check_without_an_actorless_writer_has_no_cli_note(tmp_path, monkeypatch):
    cfg = {"mcp_servers": {"okengine": {}}, "platform_toolsets": {"api_server": ["okengine"]}}
    found = _checks(tmp_path, monkeypatch, cfg)
    assert found == [], found


# --- okengine#991 [3]: platforms Hermes enables from environment credentials -----------------
# The pinned Hermes enables a messaging platform from env credentials alone
# (gateway/config_env.py _ENV_STEPS, plus the bundled plugin platforms' env enablement). With no
# platform_toolsets entry that platform binds every MCP server, the actor-less writer included.

_TEMPLATE_TOOLSETS = {"cron": ["hermes-cron", "no_mcp"], "api_server": ["okengine", hl.CHAT_WRITER]}


def test_a_token_enabled_telegram_bot_fails_the_rule():
    errors = hl.actorless_writer_bindings(_cfg(_TEMPLATE_TOOLSETS),
                                          env={"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert len(errors) == 1 and "'telegram'" in errors[0], errors
    assert "TELEGRAM_BOT_TOKEN" in errors[0]


def test_the_shipped_skeleton_env_with_empty_tokens_enables_nothing():
    env = {}
    for raw in (REPO / "templates" / "pack" / "skeleton" / ".env.example").read_text().splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            env[key.strip()] = value.strip()
    assert "TELEGRAM_BOT_TOKEN" in env
    assert hl.env_enabled_platforms(env) == {}


@pytest.mark.parametrize(("env", "platform"), [
    ({"DISCORD_BOT_TOKEN": "t"}, "discord"),
    ({"SLACK_BOT_TOKEN": "t"}, "slack"),
    ({"WHATSAPP_ENABLED": "yes"}, "whatsapp"),
    ({"WHATSAPP_CLOUD_PHONE_NUMBER_ID": "1", "WHATSAPP_CLOUD_ACCESS_TOKEN": "t"}, "whatsapp_cloud"),
    ({"SIGNAL_HTTP_URL": "http://s", "SIGNAL_ACCOUNT": "+1"}, "signal"),
    ({"MATTERMOST_TOKEN": "t"}, "mattermost"),
    ({"MATRIX_PASSWORD": "p"}, "matrix"),
    ({"HASS_TOKEN": "t"}, "homeassistant"),
    ({"EMAIL_ADDRESS": "a", "EMAIL_PASSWORD": "p", "EMAIL_IMAP_HOST": "i",
      "EMAIL_SMTP_HOST": "s"}, "email"),
    ({"TWILIO_ACCOUNT_SID": "s"}, "sms"),
    ({"WEBHOOK_ENABLED": "1"}, "webhook"),
    ({"MSGRAPH_WEBHOOK_ENABLED": "true"}, "msgraph_webhook"),
    ({"YUANBAO_APP_KEY": "k", "YUANBAO_APP_SECRET": "s"}, "yuanbao"),
    ({"GATEWAY_RELAY_URL": "https://r"}, "relay"),
    ({"IRC_SERVER": "irc", "IRC_CHANNEL": "#c"}, "irc"),
    ({"TEAMS_CLIENT_ID": "i", "TEAMS_CLIENT_SECRET": "s", "TEAMS_TENANT_ID": "t"}, "teams"),
    ({"NTFY_TOPIC": "t"}, "ntfy"),
])
def test_each_env_enabled_platform_is_seen(env, platform):
    assert list(hl.env_enabled_platforms(env)) == [platform]
    assert platform in hl.actorless_writer_platforms(_cfg(_TEMPLATE_TOOLSETS), env=env)


@pytest.mark.parametrize("env", [
    {"SIGNAL_HTTP_URL": "http://s"},                       # every credential group is required
    {"EMAIL_ADDRESS": "a", "EMAIL_PASSWORD": "p"},
    {"WHATSAPP_ENABLED": "false"},                         # a flag must be truthy, not just set
    {"WEBHOOK_ENABLED": ""},
    {"TELEGRAM_BOT_TOKEN": ""},                            # an empty credential is absent
    {"API_SERVER_KEY": "k" * 32},                          # api_server is the chat_enabled input
])
def test_partial_or_empty_credentials_enable_nothing(env):
    assert hl.env_enabled_platforms(env) == {}


def test_an_explicit_config_disable_beats_env_credentials():
    """Pinned Hermes: `platforms.<x>.enabled: false` wins over env credentials."""
    cfg = _cfg(_TEMPLATE_TOOLSETS, platforms={"telegram": {"enabled": False}})
    env = {"TELEGRAM_BOT_TOKEN": "t"}
    assert hl.env_enabled_platforms(env, cfg) == {}
    assert hl.actorless_writer_bindings(cfg, env=env) == []


def test_an_env_enabled_platform_with_a_safe_toolset_passes():
    cfg = _cfg(dict(_TEMPLATE_TOOLSETS, telegram=["okengine"]))
    assert hl.actorless_writer_bindings(cfg, env={"TELEGRAM_BOT_TOKEN": "t"}) == []


def test_the_env_table_tracks_the_hermes_pin():
    """The table mirrors one Hermes release's env enablement. Bumping the pin without re-reading
    gateway/config_env.py and plugins/platforms/ would let the gate go stale silently."""
    manifest = yaml.safe_load((REPO / "engine-manifest.yaml").read_text())
    assert hl.HERMES_ENV_TABLE_PIN == manifest["runtime"]["pinned_tag"], (
        "Hermes pin moved: re-verify hardening_lib.ENV_ENABLED_PLATFORMS against the new tag's "
        "gateway/config_env.py and plugins/platforms/*, then update HERMES_ENV_TABLE_PIN")


def test_framework_validate_reads_platform_credentials_from_the_pack_env(tmp_path):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg(_TEMPLATE_TOOLSETS))
    assert len(report.fails) == 1 and "'telegram'" in report.fails[0], report.fails


def test_deployment_check_reads_platform_credentials_from_the_gateway_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    found = _checks(tmp_path, monkeypatch, _cfg(_TEMPLATE_TOOLSETS))
    assert any(lvl == "FAIL" and "'discord'" in m for lvl, m in found), found


def test_actorless_writer_platforms_defaults_chat_off_and_takes_keywords_only():
    """The structured form shares the message form's signature contract: chat is off unless a
    caller says so, and the flags cannot be passed positionally."""
    cfg = _cfg({"cron": ["hermes-cron", "no_mcp"]})
    assert hl.actorless_writer_platforms(cfg) == {}
    assert "api_server" in hl.actorless_writer_platforms(cfg, chat_enabled=True)
    with pytest.raises(TypeError):
        hl.actorless_writer_platforms(cfg, True)  # type: ignore[misc]


# --- QA (!1251 F2): Hermes also loads $HERMES_HOME/.env = .hermes-data/.env, with override ---


def _hermes_env(pack: Path, text: str) -> None:
    (pack / ".hermes-data").mkdir(exist_ok=True)
    (pack / ".hermes-data" / ".env").write_text(text)


def test_framework_validate_reads_platform_credentials_from_the_hermes_home_env(tmp_path):
    _hermes_env(tmp_path, "TELEGRAM_BOT_TOKEN=123:abc\n")
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg(_TEMPLATE_TOOLSETS))
    assert len(report.fails) == 1 and "'telegram'" in report.fails[0], report.fails


def test_the_hermes_home_env_overrides_the_pack_env(tmp_path):
    (tmp_path / ".env").write_text("WHATSAPP_ENABLED=true\n")
    _hermes_env(tmp_path, "WHATSAPP_ENABLED=false\n")
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg(_TEMPLATE_TOOLSETS))
    assert report.fails == [], report.fails


def test_framework_validate_detects_chat_enabled_by_a_hermes_home_key(tmp_path):
    _hermes_env(tmp_path, "API_SERVER_KEY=" + "k" * 32 + "\n")
    assert _fvr()._chat_enabled(tmp_path, {}) is True


def test_framework_validate_reads_the_pack_env_as_compose_does(tmp_path):
    (tmp_path / ".env").write_text("export TELEGRAM_BOT_TOKEN=t  # bot\n")
    assert _fvr()._read_dotenv(tmp_path) == {"TELEGRAM_BOT_TOKEN": "t"}


def test_deployment_check_reads_platform_credentials_from_the_data_env(tmp_path, monkeypatch):
    for key in hl.ENV_ENABLED_PLATFORMS["slack"]:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".env").write_text("SLACK_BOT_TOKEN=t\n")
    found = _checks_in(tmp_path, monkeypatch, _cfg(_TEMPLATE_TOOLSETS))
    assert any(lvl == "FAIL" and "'slack'" in m for lvl, m in found), found


def test_deployment_check_detects_chat_enabled_by_a_data_env_key(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".env").write_text("API_SERVER_KEY=" + "k" * 32 + "\n")
    found = _checks_in(tmp_path, monkeypatch, _cfg({}), chat_env=False)
    assert any(lvl == "FAIL" and "'api_server'" in m for lvl, m in found), found


def _checks_in(tmp_path, monkeypatch, cfg, chat_env=True):
    """_checks, for a data dir the test already created (it holds a .env)."""
    spec = importlib.util.spec_from_file_location(
        "deployment_checks_991f2", REPO / "scripts" / "cron" / "deployment_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / "data"
    (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    module.configure(tmp_path, data, tmp_path / "hermes")
    for key in ("OKENGINE_HARDENED", "OKENGINE_EDITING", "API_SERVER_KEY", "API_SERVER_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    if chat_env:
        monkeypatch.setenv("API_SERVER_ENABLED", "true")
    module.reset()
    module.check_auth()
    return [(lvl, msg) for lvl, area, msg in module.F if area == "auth"]
