"""okengine#1027: ensure-runtime states the deployment pack in every writer's `env:` map.

Hermes hands a stdio writer only its own `env:` map (plus a safe baseline), so the gateway's
OKENGINE_PACK never arrived and no rendered map carried it. The writer derives its pack from
pack.yaml and refuses to start when an env value disagrees, so the renderer writes the pack.yaml
name into every `okengine-write*` map and corrects a stale one."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ensure-runtime.sh"

pytestmark = pytest.mark.integration

_CONFIG = (
    "mcp_servers:\n"
    "  okengine:\n"
    "    url: http://okengine-mcp:8730/mcp\n"
    "  okengine-write:\n"
    "    command: /opt/hermes/.venv/bin/python\n"
    "    env:\n"
    "      OKENGINE_POLICY_CATALOG: /opt/hermes/config/policy/catalog.yaml\n"
    "  okengine-write-entity-backfill:\n"
    "    command: /opt/hermes/.venv/bin/python\n"
    "    env: {OKENGINE_WRITE_ACTOR: 'cron:entity-backfill'}\n"
    "  okengine-write-stale:\n"
    "    command: /opt/hermes/.venv/bin/python\n"
    "    env:\n"
    "      OKENGINE_WRITE_ACTOR: cron:stale\n"
    "      OKENGINE_PACK: okpack-old\n"
    "platform_toolsets:\n"
    "  cron: [no_mcp]\n"
)


def _run(pack: Path):
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1")
    # --fix-perms: a root run (the mutation lanes) resolves the gateway uid to 10000, which the
    # scratch tree must then be writable by; for an ordinary user it changes nothing.
    return subprocess.run(["bash", str(SCRIPT), str(pack), "--fix-perms"], capture_output=True,
                          text=True, timeout=60, env=env)


def _deployment(tmp_path: Path, pack_yaml: str | None) -> Path:
    (tmp_path / ".hermes-data").mkdir()
    (tmp_path / ".hermes-data" / "config.yaml").write_text(_CONFIG)
    if pack_yaml is not None:
        (tmp_path / "pack.yaml").write_text(pack_yaml)
    return tmp_path / ".hermes-data" / "config.yaml"


def _writer_packs(cfg: Path) -> dict:
    servers = yaml.safe_load(cfg.read_text())["mcp_servers"]
    return {name: (spec.get("env") or {}).get("OKENGINE_PACK")
            for name, spec in servers.items() if name.startswith("okengine-write")}


def test_every_writer_env_map_carries_the_pack_yaml_name(tmp_path):
    cfg = _deployment(tmp_path, "name: okpack-a\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    packs = _writer_packs(cfg)
    assert set(packs) >= {"okengine-write", "okengine-write-entity-backfill",
                          "okengine-write-stale", "okengine-write-chat",
                          "okengine-write-source-quality"}, packs
    assert set(packs.values()) == {"okpack-a"}, packs        # the stale value is corrected
    assert "okengine:" in cfg.read_text() and "OKENGINE_PACK" not in \
        str(yaml.safe_load(cfg.read_text())["mcp_servers"]["okengine"])
    assert "bound pack okpack-a" in result.stdout

    again = _run(tmp_path)                                  # idempotent
    assert again.returncode == 0, again.stderr
    assert "bound pack" not in again.stdout


def test_a_name_that_is_not_a_plain_token_is_quoted(tmp_path):
    cfg = _deployment(tmp_path, "name: 'pack: odd #1'\n")
    assert _run(tmp_path).returncode == 0
    assert set(_writer_packs(cfg).values()) == {"pack: odd #1"}
    assert _run(tmp_path).stdout.count("bound pack") == 0


def test_an_unreadable_pack_yaml_fails_the_render_and_binds_nothing(tmp_path):
    """okengine#1029: the lane writers are rendered from this pack's own fleet, which cannot be
    built without its name, so an unparseable pack.yaml now stops ensure-runtime. Nothing is
    guessed on the way out: no writer gains a pack."""
    cfg = _deployment(tmp_path, "name: [\n")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "cannot generate this deployment's cron fleet" in result.stderr, result.stderr
    packs = _writer_packs(cfg)
    assert packs["okengine-write-stale"] == "okpack-old"
    assert {v for k, v in packs.items() if k != "okengine-write-stale"} == {None}


@pytest.mark.parametrize("pack_yaml", [None, "version: 1\n"])
def test_without_a_pack_name_nothing_is_guessed(tmp_path, pack_yaml):
    cfg = _deployment(tmp_path, pack_yaml)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    packs = _writer_packs(cfg)
    assert packs["okengine-write-stale"] == "okpack-old"
    assert {v for k, v in packs.items() if k != "okengine-write-stale"} == {None}
