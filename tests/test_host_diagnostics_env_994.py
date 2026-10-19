"""okengine#994 [16]: host `framework status/doctor` (and post-deploy [9]) judge the gateway's env.

On the host the shared checks read the operator's SHELL, not what compose hands the gateway. A pack
whose `API_SERVER_ENABLED=true` lives only in the pack `.env` (seen live on one deployment) reported the chat
toolset lockdown as not applicable, while `framework validate` and the gateway FAILed it; a value
exported only in the shell counted although no container sees it. The host env is now the pack
`.env` read as compose reads it, with `.hermes-data/.env` over it as Hermes loads it.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
_NOT_A_LIST = "Agent Chat (api_server) is enabled but platform_toolsets.api_server is not a list"
_HOST_ONLY = ("API_SERVER_ENABLED", "API_SERVER_KEY", "TZ", "OKENGINE_PACK", "OKENGINE_TRUST",
              "OKENGINE_BIND", "OKENGINE_READER_PASSWORD", "OKENGINE_HARDENED")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def diagnostics(monkeypatch):
    for key in _HOST_ONLY:
        monkeypatch.delenv(key, raising=False)
    checks = _load("deployment_checks", REPO / "scripts" / "cron" / "deployment_checks.py")
    module = _load("framework_diagnostics", REPO / "scripts" / "framework_diagnostics.py")
    yield module, checks
    checks.configure(checks.VAULT)            # never leak a host env into a later in-process test


def _deployment(tmp_path, pack_env=None, hermes_env=None):
    dep = tmp_path / "dep"
    (dep / "wiki").mkdir(parents=True)
    (dep / ".hermes-data").mkdir()
    (dep / "schema.yaml").write_text("types: {actor: {}}\n")
    # No platform_toolsets: with chat enabled, Hermes would hand the api_server its broad default.
    (dep / ".hermes-data" / "config.yaml").write_text("model: x\n")
    if pack_env is not None:
        (dep / ".env").write_text(pack_env)
    if hermes_env is not None:
        (dep / ".hermes-data" / ".env").write_text(hermes_env)
    return dep


def _auth(diagnostics, dep):
    module, _checks = diagnostics
    resolved, error = module.resolve(str(dep))
    assert resolved == dep and error == ""
    return [msg for level, _area, msg in module.run(["auth"]) if level == "FAIL"]


def test_chat_enabled_only_in_the_pack_env_is_seen_on_the_host(diagnostics, tmp_path):
    dep = _deployment(tmp_path, pack_env="API_SERVER_ENABLED=true # chat on the LAN\n")
    assert any(_NOT_A_LIST in msg for msg in _auth(diagnostics, dep))


def test_a_shell_only_value_is_not_the_gateways(diagnostics, tmp_path, monkeypatch):
    monkeypatch.setenv("API_SERVER_ENABLED", "true")
    dep = _deployment(tmp_path, pack_env="OKENGINE_TRUST=public\n")
    assert not any(_NOT_A_LIST in msg for msg in _auth(diagnostics, dep))


def test_the_pack_env_is_read_with_compose_grammar(diagnostics, tmp_path):
    # last assignment wins; `export`; a forward reference resolves through the project .env parse
    dep = _deployment(tmp_path, pack_env="API_SERVER_ENABLED=false\n"
                                         "export API_SERVER_ENABLED=${ON} # chat\nON=true\n")
    assert any(_NOT_A_LIST in msg for msg in _auth(diagnostics, dep))


@pytest.mark.parametrize(("pack_env", "hermes_env", "enabled"), [
    ("API_SERVER_ENABLED=true\n", "API_SERVER_ENABLED=false\n", False),
    ("API_SERVER_ENABLED=false\n", "API_SERVER_ENABLED=true\n", True),
    (None, "API_SERVER_KEY=k\n", True),
])
def test_hermes_home_env_overrides_the_pack_env(diagnostics, tmp_path, pack_env, hermes_env,
                                                enabled):
    """Hermes loads `.hermes-data/.env` over the container environment (override=True)."""
    dep = _deployment(tmp_path, pack_env=pack_env, hermes_env=hermes_env)
    assert any(_NOT_A_LIST in msg for msg in _auth(diagnostics, dep)) is enabled


def test_the_timezone_check_reads_the_pack_tz(diagnostics, tmp_path):
    module, _checks = diagnostics
    dep = _deployment(tmp_path, pack_env="TZ=Not/AZone\n")
    module.resolve(str(dep))
    assert any(level == "FAIL" and "Not/AZone" in msg for level, _a, msg in module.run(["timezone"]))


def test_host_env_is_the_compose_env_of_the_pack(diagnostics, tmp_path):
    _module, checks = diagnostics
    dep = _deployment(tmp_path, pack_env="A=${FROM_SHELL}\nB=1 # c\nB=2\n")
    # TZ is the gateway's `environment:` value, `${TZ:-UTC}` (okengine#1042 [36]).
    assert checks.host_env(dep, {"FROM_SHELL": "s", "ONLY_SHELL": "x"}) == {"A": "s", "B": "2",
                                                                            "TZ": "UTC"}
    assert checks.host_env(tmp_path / "absent", {"X": "1"}) == {"TZ": "UTC"}


def test_configure_without_env_returns_to_the_process_environment(diagnostics, tmp_path,
                                                                  monkeypatch):
    _module, checks = diagnostics
    checks.configure(tmp_path, env={"API_SERVER_ENABLED": "true"})
    assert checks.ENV == {"API_SERVER_ENABLED": "true"}
    checks.configure(tmp_path)
    monkeypatch.setenv("API_SERVER_ENABLED", "later")
    assert checks.ENV is __import__("os").environ and checks.ENV["API_SERVER_ENABLED"] == "later"


def test_post_deploy_self_checks_use_the_host_env():
    """Step [9]'s driver configures the checks with the pack .env (okengine#994 [16]).

    tests/cron/test_post_deploy_verify_checks.py::test_step9_configures_the_checks_with_the_host_env
    proves it by running the driver; this pins it in the driver's source.

    CANNOT DETECT: whether configure() is reached; that is the behavioral test's job.
    """
    text = (REPO / "scripts" / "post_deploy_selfchecks.py").read_text(encoding="utf-8")
    block = text[text.index("def run_checks"):]
    assert "env=checks.host_env(deployment)" in block.split("checks.run([name])")[0]


# --- framework_diagnostics' own contract (registered as a mutation target with okengine#994) -------
# The finding levels are built at runtime so a `==` that degrades to `is` is caught: equal strings
# from different sources are not one object.
def _level(text):
    return "".join(list(text))


_MIXED = [(_level("FAIL"), "a", ""), (_level("WARN"), "b", ""), (_level("INFO"), "c", ""),
          (_level("FAIL"), "d", ""), (_level("INFO"), "e", ""), (_level("INFO"), "f", ""),
          (_level("INFO"), "k", ""), ("ERR", "g", ""), ("ZZZ", "h", ""), ("ZZY", "j", ""),
          ("A", "i", "")]


def test_counts_tallies_each_level_exactly(diagnostics):
    module, _checks = diagnostics
    assert module.counts(_MIXED) == (2, 1, 4)
    assert module.counts([]) == (0, 0, 0)


@pytest.mark.parametrize(("levels", "code"), [
    ([_level("FAIL")], 1), ([_level("WARN"), _level("INFO")], 0), (["ERR", "A", "ZZZ"], 0), ([], 0),
])
def test_exit_code_fails_only_on_a_fail(diagnostics, levels, code):
    module, _checks = diagnostics
    assert module.exit_code([(level, "x", "") for level in levels]) == code
    assert (module.EXIT_OK, module.EXIT_FAIL, module.EXIT_USAGE) == (0, 1, 2)


def test_run_orders_fail_warn_info_then_anything_else_and_ties_by_area(diagnostics, monkeypatch):
    module, checks = diagnostics
    raw = [("ERR", "a", "m1"), ("INFO", "b", "m2"), ("WARN", "c", "m3"), ("FAIL", "d", "m4"),
           ("INFO", "a", "m5"), ("FAIL", "e", "m6")]
    seen = []
    monkeypatch.setattr(checks, "run", lambda names=None: seen.append(names) or list(raw))
    assert [(lv, area) for lv, area, _m in module.run(["auth"])] == [
        ("FAIL", "d"), ("FAIL", "e"), ("WARN", "c"), ("INFO", "a"), ("INFO", "b"), ("ERR", "a")]
    assert seen == [["auth"]]


def test_emit_json_prints_sorted_two_space_json(diagnostics, capsys):
    module, _checks = diagnostics
    module.emit_json({"b": 1, "a": {"d": 2, "c": 3}})
    assert capsys.readouterr().out == (
        '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n')


@pytest.mark.parametrize("marker", ["schema.yaml", "wiki"])
def test_either_marker_makes_a_deployment(diagnostics, tmp_path, marker):
    module, _checks = diagnostics
    dep = tmp_path / "dep"
    dep.mkdir()
    (dep / marker).mkdir() if marker == "wiki" else (dep / marker).write_text("types: {}\n")
    assert module.resolve(str(dep)) == (dep, "")


def test_resolve_rejects_a_missing_or_unmarked_directory(diagnostics, tmp_path):
    module, _checks = diagnostics
    path, error = module.resolve(str(tmp_path / "absent"))
    assert path is None and "deployment directory not found" in error
    path, error = module.resolve(str(tmp_path))
    assert path is None and "does not look like a deployment" in error


def test_the_checks_library_is_imported_from_its_sibling_cron_dir(monkeypatch):
    """The module puts scripts/cron FIRST on sys.path, so an unrelated `deployment_checks` earlier
    on the path can never shadow the engine's own."""
    checks = _load("deployment_checks", REPO / "scripts" / "cron" / "deployment_checks.py")
    monkeypatch.setattr(sys, "path", ["/nonexistent-first", "/nonexistent-second"])
    monkeypatch.setitem(sys.modules, "deployment_checks", checks)
    _load("framework_diagnostics", REPO / "scripts" / "framework_diagnostics.py")
    assert sys.path[0] == str(REPO / "scripts" / "cron")


def _model_policy_fails(checks):
    checks.reset()
    checks.check_crons()
    return [msg for level, area, msg in checks.F if level == "FAIL" and area == "DeepSeek model policy"]


def test_a_legacy_model_only_in_the_operators_shell_is_not_the_gateways(diagnostics, tmp_path,
                                                                         monkeypatch):
    """The model-policy check judged the host's shell: a legacy model exported there FAILed
    `framework doctor` although no container sees it (QA repro on !1273)."""
    module, checks = diagnostics
    monkeypatch.setenv("OPERATOR_SCRATCH_MODEL", "deepseek-chat")
    dep = _deployment(tmp_path, pack_env="TZ=UTC\n")
    module.resolve(str(dep))
    assert _model_policy_fails(checks) == []


def test_a_legacy_model_in_the_gateway_environment_still_fails(diagnostics, tmp_path,
                                                               monkeypatch):
    _module, checks = diagnostics
    monkeypatch.delenv("OPERATOR_SCRATCH_MODEL", raising=False)
    checks.configure(tmp_path, data=tmp_path / "data", hermes=tmp_path / "h",
                     env={"OPERATOR_SCRATCH_MODEL": "deepseek-chat"})
    fails = _model_policy_fails(checks)
    assert any(msg.startswith("gateway environment OPERATOR_SCRATCH_MODEL") for msg in fails), fails
