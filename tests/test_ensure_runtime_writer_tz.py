"""okengine#1042 [36]: ensure-runtime states the deployment timezone in every writer's `env:` map.

Hermes hands a stdio MCP server only a safe baseline (PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL,
TMPDIR, XDG_*) plus its own `env:` map, so the gateway's TZ never reached a write server. Its
`_today()` then dated wiki/log.md ledger lines in UTC while the in-process writers and the
review/rotation lanes used the deployment's local day; a live ledger carried both conventions
(85 UTC-dated and 72 local-dated lines in the evening window). The renderer binds the zone compose
gives the gateway (`TZ: ${TZ:-UTC}`) into every `okengine-write*` map and corrects a stale one.

Run as deploy.sh runs it: the real script, as a subprocess, on a deployment directory.
"""
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
    "      TZ: Europe/Berlin\n"
    "platform_toolsets:\n"
    "  cron: [no_mcp]\n"
)


def _run(pack: Path, tz: str | None = None):
    env = dict(os.environ, HERMES_UID=str(os.getuid()), HERMES_GID=str(os.getgid()),
               OKENGINE_CRON_PLUS_SKIP="1")
    env.pop("TZ", None)
    if tz is not None:
        env["TZ"] = tz
    # --fix-perms: a root run (the mutation lanes) resolves the gateway uid to 10000, which the
    # scratch tree must then be writable by; for an ordinary user it changes nothing.
    return subprocess.run(["bash", str(SCRIPT), str(pack), "--fix-perms"], capture_output=True,
                          text=True, timeout=60, env=env)


def _deployment(tmp_path: Path, dotenv: str | None) -> Path:
    (tmp_path / ".hermes-data").mkdir()
    (tmp_path / ".hermes-data" / "config.yaml").write_text(_CONFIG)
    (tmp_path / "pack.yaml").write_text("name: okpack-a\n")
    if dotenv is not None:
        (tmp_path / ".env").write_text(dotenv)
    return tmp_path / ".hermes-data" / "config.yaml"


def _writer_zones(cfg: Path) -> dict:
    servers = yaml.safe_load(cfg.read_text())["mcp_servers"]
    return {name: (spec.get("env") or {}).get("TZ")
            for name, spec in servers.items() if name.startswith("okengine-write")}


def test_every_writer_env_map_carries_the_pack_env_zone(tmp_path):
    cfg = _deployment(tmp_path, "TZ=America/New_York  # local ledger day\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    zones = _writer_zones(cfg)
    assert set(zones) >= {"okengine-write", "okengine-write-entity-backfill",
                          "okengine-write-stale", "okengine-write-chat",
                          "okengine-write-source-quality"}, zones
    assert set(zones.values()) == {"America/New_York"}, zones   # the stale Berlin is corrected
    assert "TZ" not in str(yaml.safe_load(cfg.read_text())["mcp_servers"]["okengine"])
    assert "bound timezone America/New_York" in result.stdout

    again = _run(tmp_path)                                     # idempotent
    assert again.returncode == 0, again.stderr
    assert "bound timezone" not in again.stdout


def test_without_a_zone_the_writers_run_in_compose_default_utc(tmp_path):
    """UTC is compose's default and a writer's default alike: no TZ is bound (a fresh config stays
    as seeded), and the stale Berlin zone is dropped."""
    cfg = _deployment(tmp_path, None)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert set(_writer_zones(cfg).values()) == {None}
    assert "dropped a stale timezone from 1 writer env(s); UTC" in result.stdout
    assert "timezone" not in _run(tmp_path).stdout


def test_the_invoking_environment_wins_as_it_does_for_compose(tmp_path):
    """`TZ=${TZ:-UTC}` in the gateway service resolves from the shell running compose before the
    project .env, so the writers follow the same precedence."""
    cfg = _deployment(tmp_path, "TZ=America/New_York\n")
    assert _run(tmp_path, tz="America/Chicago").returncode == 0
    assert set(_writer_zones(cfg).values()) == {"America/Chicago"}



def test_an_exported_empty_tz_binds_compose_default_utc(tmp_path):
    """`TZ=${TZ:-UTC}`: an exported empty TZ is unset for `:-`, so compose gives UTC and does not
    fall back to the .env value; the writers follow (review F4)."""
    cfg = _deployment(tmp_path, "TZ=America/New_York\n")
    assert _run(tmp_path, tz="").returncode == 0
    assert set(_writer_zones(cfg).values()) == {None}
