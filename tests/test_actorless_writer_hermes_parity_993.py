"""okengine#993 F3/F4: the actor-less writer gate judges platform enablement the way pinned
Hermes does.

Pinned Hermes (v2026.9.14) reads a platform block from four places, not just `platforms.<x>`:
gateway/config_loader.py merge_platform_sections merges `gateway.platforms.<x>`, then
`platforms.<x>`, then `gateway.<x>` (later wins), and bridge_platform_shared_keys lets a
top-level `<x>:` block override `enabled` on top of all three. Any block's `enabled` key marks the
platform explicit, so env credentials no longer enable it (config_env._enable_from_env). A
`gateway.relay_url` or a relay block's `relay_url` enables the relay. The photon plugin also
enables itself from credentials stored in $HERMES_HOME/auth.json (plugins/platforms/photon/auth.py
load_project_credentials), with no env var at all.

A gate that reads fewer places than Hermes misses an enabled platform: a false negative leaves an
admin writer on a model-facing platform.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "cron"))
import hardening_lib as hl  # noqa: E402

_WRITER = {"command": "/opt/hermes/.venv/bin/python",
           "args": ["/opt/hermes/okengine-mcp/write_server.py"]}
_TOOLSETS = {"cron": ["hermes-cron", "no_mcp"], "api_server": ["okengine", hl.CHAT_WRITER]}


def _cfg(**extra) -> dict:
    cfg = {"mcp_servers": {
        "okengine": {"url": "http://okengine-mcp:8730/mcp"},
        "okengine-write": dict(_WRITER, env={}),
        hl.CHAT_WRITER: dict(_WRITER, env={"OKENGINE_WRITE_ACTOR": hl.CHAT_ACTOR}),
    }, "platform_toolsets": dict(_TOOLSETS)}
    cfg.update(extra)
    return cfg


def _reported(cfg, env=None, auth=None) -> list[str]:
    return sorted(hl.actorless_writer_platforms(cfg, env=env, auth=auth))


# --- F3: where Hermes reads a platform block -------------------------------------------------


def test_a_top_level_block_enable_beats_a_platforms_disable():
    """The QA false negative: `platforms.telegram.enabled: false` plus a top-level
    `telegram.enabled: true`. Hermes runs telegram (bridge_platform_shared_keys)."""
    cfg = _cfg(platforms={"telegram": {"enabled": False}}, telegram={"enabled": True})
    assert _reported(cfg) == ["telegram"]


def test_a_top_level_block_disable_beats_env_credentials_and_a_platforms_enable():
    cfg = _cfg(platforms={"telegram": {"enabled": True}}, telegram={"enabled": False})
    assert _reported(cfg, env={"TELEGRAM_BOT_TOKEN": "t"}) == []


def test_a_top_level_block_without_enabled_leaves_the_nested_value():
    cfg = _cfg(platforms={"telegram": {"enabled": True}}, telegram={"require_mention": True})
    assert _reported(cfg) == ["telegram"]


def test_a_gateway_platforms_block_enables_a_platform():
    assert _reported(_cfg(gateway={"platforms": {"discord": {"enabled": True}}})) == ["discord"]


def test_a_gateway_subsection_enables_a_platform():
    assert _reported(_cfg(gateway={"slack": {"enabled": True}})) == ["slack"]


def test_a_gateway_subsection_is_merged_after_platforms_and_wins():
    """merge_platform_sections merges gateway.<x> LAST, so its enable beats platforms.<x>."""
    cfg = _cfg(platforms={"slack": {"enabled": False}}, gateway={"slack": {"enabled": True}})
    assert _reported(cfg) == ["slack"]


def test_platforms_is_merged_after_gateway_platforms_and_wins():
    cfg = _cfg(gateway={"platforms": {"slack": {"enabled": False}}},
               platforms={"slack": {"enabled": True}})
    assert _reported(cfg) == ["slack"]


def test_a_gateway_platforms_disable_is_explicit_and_beats_env_credentials():
    cfg = _cfg(gateway={"platforms": {"telegram": {"enabled": False}}})
    assert _reported(cfg, env={"TELEGRAM_BOT_TOKEN": "t"}) == []


def test_an_enabled_key_anywhere_marks_the_platform_explicit():
    """A later block without `enabled` keeps the earlier block's explicit disable."""
    cfg = _cfg(gateway={"platforms": {"telegram": {"enabled": False}}},
               platforms={"telegram": {"require_mention": True}})
    assert _reported(cfg, env={"TELEGRAM_BOT_TOKEN": "t"}) == []


@pytest.mark.parametrize(("value", "enabled"), [
    (True, True), ("yes", True), (" On ", True), (1, True),
    (False, False), ("false", False), ("no", False), ("garbage", False), (None, False), (0, False),
])
def test_enabled_is_coerced_as_hermes_coerces_it(value, enabled):
    """PlatformConfig.from_dict: _coerce_bool(enabled, False). A string Hermes does not
    recognize reads as disabled, and the key still makes the disable explicit."""
    cfg = _cfg(platforms={"telegram": {"enabled": value}})
    assert _reported(cfg) == (["telegram"] if enabled else [])
    assert _reported(cfg, env={"TELEGRAM_BOT_TOKEN": "t"}) == (["telegram"] if enabled else [])


def test_a_top_level_key_that_is_not_a_hermes_platform_is_not_a_platform():
    cfg = _cfg(memory={"enabled": True}, gateway={"streaming": {"enabled": True}})
    assert _reported(cfg) == []


def test_a_bundled_plugin_platform_counts_at_the_top_level():
    assert _reported(_cfg(irc={"enabled": True})) == ["irc"]


@pytest.mark.parametrize("gateway", ["x", None, ["telegram"]])
def test_a_malformed_gateway_section_is_ignored(gateway):
    assert _reported(_cfg(gateway=gateway, platforms={"telegram": {"enabled": True}})) == [
        "telegram"]


def test_a_non_mapping_platform_block_is_ignored():
    cfg = _cfg(telegram="on", gateway={"platforms": {"slack": True}, "discord": ["x"]})
    assert _reported(cfg) == []


def test_a_top_level_local_block_is_never_read():
    """bridge_platform_shared_keys skips Platform.LOCAL's top-level block. The name is built at
    runtime so an identity compare cannot pass for an equality one."""
    local = "".join(["lo", "cal"])
    assert hl.platform_config_state({"local": {"enabled": True}}, local) == (False, False)
    assert hl.platform_config_state({"gateway": {"local": {"enabled": True}}}, local) == (True, True)


def test_platform_toolset_bindings_defaults_chat_off_and_takes_keywords_only():
    cfg = _cfg(platform_toolsets={})
    assert hl.platform_toolset_bindings(cfg) == {}
    assert "api_server" in hl.platform_toolset_bindings(cfg, chat_enabled=True)
    with pytest.raises(TypeError):
        hl.platform_toolset_bindings(cfg, True)  # type: ignore[misc]


# --- F3: the relay URL in config -------------------------------------------------------------


def test_a_gateway_relay_url_enables_the_relay():
    """gateway/relay/__init__.py relay_url(): env GATEWAY_RELAY_URL, then gateway.relay_url."""
    assert _reported(_cfg(gateway={"relay_url": "wss://connector/relay"})) == ["relay"]


@pytest.mark.parametrize("block", [{"relay_url": "wss://c/relay"},
                                   {"extra": {"relay_url": "wss://c/relay"}}])
def test_a_relay_block_url_enables_the_relay(block):
    """config_env._relay reads the relay block's relay_url (from_dict lands it in extra)."""
    assert _reported(_cfg(platforms={"relay": block})) == ["relay"]


def test_a_top_level_relay_block_url_enables_the_relay():
    assert _reported(_cfg(relay={"relay_url": "wss://c/relay"})) == ["relay"]


@pytest.mark.parametrize("url", ["", "   ", None])
def test_a_blank_relay_url_enables_nothing(url):
    assert _reported(_cfg(gateway={"relay_url": url})) == []


def test_an_explicit_relay_disable_vetoes_a_configured_url():
    """relay_explicitly_disabled() vetoes even the URL (relay_url())."""
    cfg = _cfg(gateway={"relay_url": "wss://c/relay"}, platforms={"relay": {"enabled": False}})
    assert _reported(cfg, env={"GATEWAY_RELAY_URL": "wss://c/relay"}) == []


# --- F4: photon enables itself from $HERMES_HOME/auth.json ------------------------------------


def _photon(**entry) -> dict:
    return {"credential_pool": {"photon_project": [entry]}}


@pytest.mark.parametrize("entry", [
    {"spectrum_project_id": "p", "project_secret": "s"},
    {"project_id": "p", "project_secret": "s"},             # back-compat key (auth.py:164)
])
def test_stored_photon_credentials_enable_photon_with_no_env(entry):
    assert _reported(_cfg(), auth=_photon(**entry)) == ["photon"]


def test_the_message_names_the_stored_credentials():
    errors = hl.actorless_writer_bindings(_cfg(), auth=_photon(spectrum_project_id="p",
                                                                project_secret="s"))
    assert len(errors) == 1 and "'photon'" in errors[0], errors
    assert "auth.json" in errors[0] and "photon_project" in errors[0]


def test_an_explicit_photon_disable_beats_stored_credentials():
    cfg = _cfg(platforms={"photon": {"enabled": False}})
    assert _reported(cfg, auth=_photon(spectrum_project_id="p", project_secret="s")) == []


@pytest.mark.parametrize("auth", [
    None, {}, {"credential_pool": {}}, {"credential_pool": {"photon_project": []}},
    {"credential_pool": {"photon_project": "x"}}, {"credential_pool": {"photon_project": ["x"]}},
    {"credential_pool": "x"}, ["not", "a", "mapping"],
    _photon(name="no credentials"),
])
def test_no_usable_stored_photon_record_enables_nothing(auth):
    assert _reported(_cfg(), auth=auth) == []


def test_only_the_first_stored_photon_record_counts():
    """auth.py _pool_first: Hermes reads credential_pool.photon_project[0] and nothing after it."""
    auth = {"credential_pool": {"photon_project": [
        {"name": "no credentials"}, {"spectrum_project_id": "p", "project_secret": "s"}]}}
    assert _reported(_cfg(), auth=auth) == []


def test_read_auth_json_matches_hermes_on_missing_and_malformed_files(tmp_path):
    """auth.py _load_auth: a missing or malformed file is {} (Hermes then enables nothing)."""
    assert hl.read_auth_json(tmp_path / "auth.json") == ({}, None)
    (tmp_path / "auth.json").write_text("{not json")
    assert hl.read_auth_json(tmp_path / "auth.json") == ({}, None)
    (tmp_path / "auth.json").write_text(json.dumps(_photon(project_id="p")))
    assert hl.read_auth_json(tmp_path / "auth.json") == (_photon(project_id="p"), None)


def test_an_unreadable_auth_json_is_reported_not_passed(tmp_path):
    """A directory reads as OSError even as root (CI's mutation lanes run as root)."""
    (tmp_path / "auth.json").mkdir()
    data, error = hl.read_auth_json(tmp_path / "auth.json")
    assert data == {} and error and "auth.json" in error


# --- the two gates read the same places --------------------------------------------------------


def _fvr():
    spec = importlib.util.spec_from_file_location(
        "fvr_993", REPO / "scripts" / "framework_validate_runtime.py")
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


def _hermes_home(pack: Path) -> Path:
    home = pack / ".hermes-data"
    home.mkdir(exist_ok=True)
    return home


def test_framework_validate_sees_stored_photon_credentials(tmp_path):
    (_hermes_home(tmp_path) / "auth.json").write_text(
        json.dumps(_photon(spectrum_project_id="p", project_secret="s")))
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg())
    assert len(report.fails) == 1 and "'photon'" in report.fails[0], report.fails


def test_framework_validate_warns_when_auth_json_is_unreadable(tmp_path):
    (_hermes_home(tmp_path) / "auth.json").mkdir()
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg())
    assert report.fails == [] and len(report.warns) == 1, (report.fails, report.warns)
    assert "undetectable" in report.warns[0] and "photon" in report.warns[0]


def test_framework_validate_does_not_warn_about_auth_json_with_no_actorless_writer(tmp_path):
    (_hermes_home(tmp_path) / "auth.json").mkdir()
    cfg = _cfg()
    del cfg["mcp_servers"]["okengine-write"]
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, cfg)
    assert report.fails == [] and report.warns == [], (report.fails, report.warns)


def test_framework_validate_sees_a_top_level_platform_enable(tmp_path):
    report = _Report()
    _fvr().check_chat_writer_binding(tmp_path, report, _cfg(
        platforms={"telegram": {"enabled": False}}, telegram={"enabled": True}))
    assert len(report.fails) == 1 and "'telegram'" in report.fails[0], report.fails


@pytest.mark.parametrize("cfg", [{"api_server": {"enabled": True}},
                                 {"gateway": {"api_server": {"enabled": True}}},
                                 {"gateway": {"platforms": {"api_server": {"enabled": "yes"}}}}])
def test_framework_validate_detects_chat_enabled_in_every_hermes_spelling(tmp_path, cfg):
    assert _fvr()._chat_enabled(tmp_path, cfg) is True


def test_framework_validate_honors_a_top_level_chat_disable(tmp_path):
    cfg = {"platforms": {"api_server": {"enabled": True}}, "api_server": {"enabled": False}}
    assert _fvr()._chat_enabled(tmp_path, cfg) is False


def _checks(tmp_path, monkeypatch, cfg, *, auth=None, auth_dir=False):
    spec = importlib.util.spec_from_file_location(
        "deployment_checks_993", REPO / "scripts" / "cron" / "deployment_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump(cfg))
    if auth is not None:
        (data / "auth.json").write_text(json.dumps(auth))
    if auth_dir:
        (data / "auth.json").mkdir()
    module.configure(tmp_path, data, tmp_path / "hermes")
    for key in ("OKENGINE_HARDENED", "OKENGINE_EDITING", "API_SERVER_KEY", "API_SERVER_ENABLED",
                "TELEGRAM_BOT_TOKEN", "PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    module.reset()
    module.check_auth()
    return [(lvl, msg) for lvl, area, msg in module.F if area == "auth"]


def test_deployment_check_sees_stored_photon_credentials(tmp_path, monkeypatch):
    found = _checks(tmp_path, monkeypatch, _cfg(),
                    auth=_photon(spectrum_project_id="p", project_secret="s"))
    assert any(lvl == "FAIL" and "'photon'" in m for lvl, m in found), found


def test_deployment_check_warns_when_auth_json_is_unreadable(tmp_path, monkeypatch):
    found = _checks(tmp_path, monkeypatch, _cfg(), auth_dir=True)
    assert [lvl for lvl, m in found if "undetectable" in m] == ["WARN"], found
    assert not [m for lvl, m in found if lvl == "FAIL"], found


def test_deployment_check_sees_a_gateway_subsection_enable(tmp_path, monkeypatch):
    found = _checks(tmp_path, monkeypatch, _cfg(gateway={"telegram": {"enabled": True}}))
    assert any(lvl == "FAIL" and "'telegram'" in m for lvl, m in found), found


def test_deployment_check_locks_down_chat_enabled_by_a_top_level_block(tmp_path, monkeypatch):
    """The api_server toolset lockdown judges chat enablement the same way."""
    cfg = _cfg(api_server={"enabled": True})
    cfg["platform_toolsets"] = {"api_server": "okengine"}
    found = _checks(tmp_path, monkeypatch, cfg)
    assert any(lvl == "FAIL" and "is not a list" in m for lvl, m in found), found
