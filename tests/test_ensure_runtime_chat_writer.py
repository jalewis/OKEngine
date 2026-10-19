"""okengine#983: ensure-runtime moves the Agent Chat off the actor-less writer on the next deploy.

Every pre-#983 deployment lists `okengine-write` (no OKENGINE_WRITE_ACTOR, so admin) in
`platform_toolsets.api_server`. ensure-runtime must register the actor-bound `okengine-write-chat`
server and migrate the toolset onto it, honoring OKENGINE_EDITING, without touching operator
formatting or anything else in the file.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ensure-runtime.sh"

pytestmark = pytest.mark.integration

_PRE_983 = """\
mcp_servers:
  okengine-write:
    command: /opt/hermes/.venv/bin/python
    args:
    - /opt/hermes/okengine-mcp/write_server.py
    env:
      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml
platform_toolsets:
  cron:
    - hermes-cron
    - no_mcp
  api_server:
    - okengine
    - okengine-write
model:
  default: operator/model
"""


def _run(pack: Path, editing: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1")
    env.pop("OKENGINE_EDITING", None)
    if editing is not None:
        env["OKENGINE_EDITING"] = editing
    result = subprocess.run(["bash", str(SCRIPT), str(pack)], capture_output=True, text=True,
                            timeout=60, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _pack(tmp_path: Path, config: str) -> Path:
    (tmp_path / ".hermes-data").mkdir()
    (tmp_path / ".hermes-data" / "config.yaml").write_text(config, encoding="utf-8")
    return tmp_path / ".hermes-data" / "config.yaml"


def _cfg(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_a_pre_983_config_is_migrated_to_the_chat_writer(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    out = _run(tmp_path)
    data = _cfg(cfg)
    assert data["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]
    chat = data["mcp_servers"]["okengine-write-chat"]
    assert chat["env"]["OKENGINE_WRITE_ACTOR"] == "chat:api_server"
    assert chat["env"]["OKENGINE_WRITE_TOOLS"] == "create_entity,update_entity,append_to_section"
    assert chat["env"]["OKENGINE_POLICY_CATALOG"] == "/opt/hermes/config/policy/catalog.yaml"
    assert chat["tools"] == {"resources": False, "prompts": False}
    # a chat actor has no cron lane contract to bind
    assert "OKENGINE_CRON_JOBS" not in chat["env"]
    # the generic writer stays registered (operator CLI / no_agent lanes), just not for chat
    assert "okengine-write" in data["mcp_servers"]
    assert data["model"] == {"default": "operator/model"}
    assert "actor-less okengine-write is never bound" in out.stdout


def test_the_migration_is_idempotent(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    _run(tmp_path)
    first = cfg.read_text()
    _run(tmp_path)
    assert cfg.read_text() == first


def test_editing_off_drops_both_writers_from_chat(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    _run(tmp_path, editing="0")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


def test_editing_back_on_restores_only_the_chat_writer(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    _run(tmp_path, editing="off")
    _run(tmp_path, editing="1")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]


def test_editing_off_with_both_writers_listed_drops_both(tmp_path):
    cfg = _pack(tmp_path, _PRE_983.replace("    - okengine-write\n",
                                            "    - okengine-write\n    - okengine-write-chat\n"))
    _run(tmp_path, editing="no")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


def test_the_actorless_writer_is_replaced_in_place_without_the_read_server(tmp_path):
    cfg = _pack(tmp_path, _PRE_983.replace("    - okengine\n    - okengine-write\n",
                                            "    - web\n    - okengine-write\n"))
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["web", "okengine-write-chat"]


def test_a_listed_chat_writer_and_actorless_writer_collapse_to_one(tmp_path):
    cfg = _pack(tmp_path, _PRE_983.replace("    - okengine-write\n",
                                            "    - okengine-write-chat\n    - okengine-write\n"))
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]


@pytest.mark.parametrize(
    ("editing", "expected"),
    [(None, "  api_server: [okengine, okengine-write-chat]  # mine\n"),
     ("0", "  api_server: [okengine]  # mine\n")],
)
def test_an_inline_list_is_migrated_keeping_its_formatting(tmp_path, editing, expected):
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              "  api_server: [okengine, okengine-write]  # mine\n")
    cfg = _pack(tmp_path, config)
    _run(tmp_path, editing=editing)
    assert expected in cfg.read_text()


def test_an_inline_chat_writer_is_dropped_when_editing_is_off(tmp_path):
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              "  api_server: [okengine, 'okengine-write-chat']\n")
    cfg = _pack(tmp_path, config)
    _run(tmp_path, editing="false")
    assert "  api_server: [okengine]\n" in cfg.read_text()


def test_an_operator_chat_writer_entry_is_never_rewritten(tmp_path):
    config = _PRE_983.replace(
        "platform_toolsets:",
        "  okengine-write-chat:\n    command: /custom/python\n    env:\n"
        "      OKENGINE_WRITE_ACTOR: chat:api_server\n"
        "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n"
        "platform_toolsets:")
    cfg = _pack(tmp_path, config)
    _run(tmp_path)
    data = _cfg(cfg)
    assert data["mcp_servers"]["okengine-write-chat"]["command"] == "/custom/python"
    assert cfg.read_text().count("okengine-write-chat:") == 1


def test_a_config_without_mcp_servers_gains_no_chat_server(tmp_path):
    cfg = _pack(tmp_path, "terminal:\n  backend: local\n")
    _run(tmp_path)
    data = _cfg(cfg)
    assert "mcp_servers" not in data
    assert data["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]


def test_cron_actor_writers_still_bind_the_cron_contract_store(tmp_path):
    config = _PRE_983.replace(
        "platform_toolsets:",
        "  okengine-write-lane:\n    command: x\n    env:\n"
        "      OKENGINE_WRITE_ACTOR: cron:lane\n"
        "platform_toolsets:")
    cfg = _pack(tmp_path, config)
    _run(tmp_path)
    env = _cfg(cfg)["mcp_servers"]["okengine-write-lane"]["env"]
    assert env["OKENGINE_CRON_JOBS"] == "/opt/data/cron-plus/jobs.json"


# --- QA of !1241: every list spelling is migrated, or the deploy fails loudly ----------------


@pytest.mark.parametrize(("editing", "expected"), [(None, ["okengine", "okengine-write-chat"]),
                                                   ("0", ["okengine"])])
def test_an_item_with_a_trailing_comment_is_migrated(tmp_path, editing, expected):
    cfg = _pack(tmp_path, _PRE_983.replace("    - okengine-write\n",
                                            "    - okengine-write   # the chat writer\n"))
    _run(tmp_path, editing=editing)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == expected
    if editing is None:
        assert "    - okengine-write-chat   # the chat writer\n" in cfg.read_text()


def test_a_commented_chat_writer_is_dropped_when_editing_is_off(tmp_path):
    cfg = _pack(tmp_path, _PRE_983.replace("    - okengine-write\n",
                                            "    - 'okengine-write-chat'  # chat\n"))
    _run(tmp_path, editing="0")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


@pytest.mark.parametrize(("editing", "expected"), [(None, ["okengine", "okengine-write-chat"]),
                                                   ("0", ["okengine"])])
def test_a_multi_line_flow_list_is_migrated(tmp_path, editing, expected):
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              "  api_server: [okengine,  # read\n    okengine-write]  # write\n")
    cfg = _pack(tmp_path, config)
    _run(tmp_path, editing=editing)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == expected
    assert _cfg(cfg)["model"] == {"default": "operator/model"}


def test_a_multi_line_flow_list_without_the_actorless_writer_is_left_alone(tmp_path):
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              "  api_server: [okengine,\n    web]\n")
    cfg = _pack(tmp_path, config)
    _run(tmp_path)
    assert "  api_server: [okengine,\n    web]\n" in cfg.read_text()


def _run_raw(pack: Path, editing: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1")
    env.pop("OKENGINE_EDITING", None)
    if editing is not None:
        env["OKENGINE_EDITING"] = editing
    return subprocess.run(["bash", str(SCRIPT), str(pack)], capture_output=True, text=True,
                          timeout=60, env=env)


def test_an_unreachable_spelling_fails_the_deploy_loudly(tmp_path):
    """An item split over two lines is valid YAML the line edits do not model: the parsed-result
    check catches it instead of leaving the admin writer bound."""
    _pack(tmp_path, _PRE_983.replace("    - okengine-write\n", "    -\n      okengine-write\n"))
    result = _run_raw(tmp_path)
    assert result.returncode != 0
    assert "still binds the actor-less okengine-write" in result.stderr + result.stdout


def test_an_unterminated_flow_list_fails_loudly(tmp_path):
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              "  api_server: [okengine,\n    okengine-write\n")
    _pack(tmp_path, config)
    result = _run_raw(tmp_path)
    assert result.returncode != 0
    assert "unterminated platform_toolsets.api_server flow list" in result.stderr + result.stdout


def test_editing_off_with_an_unreachable_chat_spelling_fails_loudly(tmp_path):
    cfg = _pack(tmp_path, _PRE_983.replace("    - okengine-write\n",
                                            "    -\n      okengine-write-chat\n"))
    result = _run_raw(tmp_path, editing="0")
    assert result.returncode != 0
    assert "OKENGINE_EDITING is off" in result.stderr + result.stdout
    assert cfg.is_file()


@pytest.mark.parametrize("comment", ["# [x]", "# see [x], ok"])
@pytest.mark.parametrize(("editing", "listed"), [(None, "okengine, okengine-write-chat"),
                                                 ("0", "okengine")])
def test_a_bracket_in_the_trailing_comment_is_not_the_lists_end(tmp_path, editing, listed,
                                                                 comment):
    """QA round 2: a greedy match took the `]` of `# [x]` for the list's closing bracket."""
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              f"  api_server: [okengine, okengine-write]  {comment}\n")
    cfg = _pack(tmp_path, config)
    _run(tmp_path, editing=editing)
    assert f"  api_server: [{listed}]  {comment}\n" in cfg.read_text()


def test_a_nested_flow_list_fails_loudly(tmp_path):
    config = _PRE_983.replace("  api_server:\n    - okengine\n    - okengine-write\n",
                              "  api_server: [[okengine], okengine-write]\n")
    _pack(tmp_path, config)
    result = _run_raw(tmp_path)
    assert result.returncode != 0
    assert "is not a plain `[a, b]` list" in result.stderr + result.stdout


def test_deploy_order_validate_then_ensure_runtime_upgrades_a_pre_983_config(tmp_path):
    """The real deploy.sh order on an existing deployment: validate must not FAIL the config
    ensure-runtime is about to migrate, and after the migration the same gate is clean."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fvr_order", REPO / "scripts" / "framework_validate_runtime.py")
    fvr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fvr)

    class Report:
        def __init__(self):
            self.fails, self.warns = [], []

        def fail(self, _name, detail):
            self.fails.append(detail)

        def warn(self, _name, detail=""):
            self.warns.append(detail)

        def ok(self, *_a):
            pass

    cfg = _pack(tmp_path, _PRE_983)
    before = Report()
    fvr.check_chat_writer_binding(tmp_path, before, _cfg(cfg))
    assert before.fails == [] and before.warns, (before.fails, before.warns)

    _run(tmp_path)
    after = Report()
    fvr.check_chat_writer_binding(tmp_path, after, _cfg(cfg))
    assert after.fails == [] and after.warns == [], (after.fails, after.warns)


# --- okengine#991 [2]: the documented `.env` switch reaches the reconcile ----------------------
# The reader and the gateway see OKENGINE_EDITING only through the pack `.env` (compose env_file);
# nothing on the deploy path exports `.env` into ensure-runtime's process. Reading only the process
# environment left the chat writer bound while both containers reported editing off.


def _env_file(pack: Path, body: str) -> None:
    (pack / ".env").write_text(body, encoding="utf-8")


def test_editing_off_in_the_pack_env_file_drops_the_chat_writer(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, "OKENGINE_DEPLOYMENT=demo\nOKENGINE_EDITING=0\n")
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


@pytest.mark.parametrize("line", ['OKENGINE_EDITING="off"', "OKENGINE_EDITING='no'",
                                  "OKENGINE_EDITING=1\nOKENGINE_EDITING=false"])
def test_the_env_file_value_is_parsed_like_framework_validate_reads_it(tmp_path, line):
    """Quotes are stripped and a later line overrides an earlier one (framework_validate_runtime
    ._read_dotenv semantics)."""
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, line + "\n")
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


def test_a_commented_out_env_file_line_is_not_a_setting(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, "# OKENGINE_EDITING=0\n")
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]


def test_the_env_file_wins_over_a_disagreeing_process_value_and_says_so(tmp_path):
    """The containers read only `.env`, so the reconcile follows `.env`; a process value that
    disagrees would recreate the very split this fixes, in the other direction."""
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, "OKENGINE_EDITING=1\n")
    out = _run(tmp_path, editing="0")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine", "okengine-write-chat"]
    assert "OKENGINE_EDITING" in out.stderr and "ignored" in out.stderr


def test_a_process_value_still_applies_when_the_env_file_does_not_set_it(tmp_path):
    """Back-compat for callers that export the flag; the run warns that the containers will not
    see it, because only `.env` reaches them."""
    cfg = _pack(tmp_path, _PRE_983)
    out = _run(tmp_path, editing="0")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]
    assert "not in" in out.stderr and ".env" in out.stderr


@pytest.mark.parametrize("line", ["OKENGINE_EDITING=0 # read-only on the LAN",
                                  "export OKENGINE_EDITING=0",
                                  'OKENGINE_EDITING="0"  # off',
                                  "OKENGINE_EDITING=0\r"])
def test_the_env_file_is_read_as_compose_reads_it(tmp_path, line):
    """QA (!1251 B1): a split-and-strip parser read each of these as editing ON while compose
    hands the containers "0". ensure-runtime now uses the shared compose-parity reader."""
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, line + "\n")
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


def test_a_bare_env_file_key_inherits_the_process_value_as_compose_does(tmp_path):
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, "OKENGINE_EDITING\n")
    out = _run(tmp_path, editing="0")
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]
    assert "ignored" not in out.stderr


def test_the_skeleton_reader_receives_the_editing_switch():
    """QA (!1251 F1): the reader has no env_file, so without an explicit entry its read-only
    indicator never saw OKENGINE_EDITING=0 from .env."""
    import re
    text = (REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml").read_text()
    compose = yaml.safe_load(re.sub(r"\{\{[A-Z0-9_]+\}\}", "9000", text))
    reader = compose["services"]["okengine-reader"]
    assert "env_file" not in reader
    assert "OKENGINE_EDITING=${OKENGINE_EDITING:-}" in reader["environment"]


# --- okengine#993 R1: `${VAR}` in the pack .env resolves as compose resolves it -----------------


def test_a_forward_reference_in_the_env_file_resolves_as_compose_does(tmp_path):
    """compose resolves the env_file's `${EDIT}` through the whole project .env, so the gateway
    sees 0 (editing off). Reading it as "" left editing ON and the chat writer bound."""
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, "OKENGINE_EDITING=${EDIT}\nEDIT=0\n")
    _run(tmp_path)
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]


def test_a_process_value_interpolated_into_the_env_file_is_followed(tmp_path):
    """compose resolves `${EDIT_SRC}` from the invoking process first; ensure-runtime must too."""
    cfg = _pack(tmp_path, _PRE_983)
    _env_file(tmp_path, "OKENGINE_EDITING=${EDIT_SRC}\n")
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1", EDIT_SRC="0")
    env.pop("OKENGINE_EDITING", None)
    result = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True,
                            timeout=60, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _cfg(cfg)["platform_toolsets"]["api_server"] == ["okengine"]
