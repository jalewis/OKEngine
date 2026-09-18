"""okengine#665: secret-bearing files must never be world-readable.

`ensure-runtime.sh --fix-perms` did `chmod -R a+rwX "$PACK"` -- the whole pack tree, including
`.env` (model keys, OKENGINE_MCP_TOKEN, Postgres passwords) and `.hermes-data/config.yaml`
(the Bearer token). INSTALL.md claimed `.env` is mode 600; nothing ever set it (both creators
did `: > .env` under the caller's umask). Contract pinned here:

  * `.env` is created 0600 by deploy.sh and ensure-runtime.sh and stays 0600 through --fix-perms
    (it is read by docker compose on the HOST, never by the container, so owner-only is always
    safe);
  * --fix-perms opens only the trees the container writes (.hermes-data, wiki, raw, .okengine),
    never pack.yaml / schema.yaml / crons;
  * when the container uid must read config.yaml under --fix-perms, the script says so by name
    instead of silently leaving the token world-readable.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENSURE = REPO / "scripts" / "ensure-runtime.sh"
DEPLOY = REPO / "scripts" / "deploy.sh"


def _mode(p: Path) -> str:
    return oct(stat.S_IMODE(p.stat().st_mode))[-3:]


def _ensure(tmp_path: Path, *args, **env):
    e = dict(os.environ, OKENGINE_CRON_PLUS_SKIP="1", **env)
    return subprocess.run(["bash", str(ENSURE), str(tmp_path), *args],
                          capture_output=True, text=True, timeout=60, env=e)


@pytest.fixture
def permissive_umask():
    """The permissive default the bug hid behind; restored so the rest of the session is untouched."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def test_ensure_runtime_creates_env_owner_only(tmp_path, permissive_umask):
    r = _ensure(tmp_path)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert _mode(tmp_path / ".env") == "600"


def test_ensure_runtime_tightens_a_pre_existing_world_readable_env(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OKENGINE_MCP_TOKEN=secret\n")
    env.chmod(0o644)
    r = _ensure(tmp_path)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert _mode(env) == "600"


def test_fix_perms_opens_runtime_trees_but_not_secrets_or_pack_sources(tmp_path):
    (tmp_path / "pack.yaml").write_text("name: x\n")
    (tmp_path / "wiki").mkdir()
    r = _ensure(tmp_path, "--fix-perms", HERMES_UID="99999", HERMES_GID="99999")
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert _mode(tmp_path / ".hermes-data")[-1] in ("6", "7"), "runtime tree is other-writable"
    assert _mode(tmp_path / "wiki")[-1] in ("6", "7"), "corpus tree is other-writable"
    assert _mode(tmp_path / ".env") == "600", ".env is host-only; never opened"
    assert _mode(tmp_path / "pack.yaml")[-1] not in ("6", "7"), "pack sources are not world-writable"
    assert "config.yaml" in r.stdout and "world-readable" in r.stdout, r.stdout


def test_deploy_creates_env_owner_only_before_the_validate_gate(tmp_path, permissive_umask):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    r = subprocess.run(["bash", str(DEPLOY), str(tmp_path)], capture_output=True, text=True,
                       timeout=60, env={"PYTHON": sys.executable, "PATH": os.environ["PATH"]})
    assert r.returncode == 1 and "validation failed" in r.stderr   # the documented abort
    assert (tmp_path / ".env").is_file()
    assert _mode(tmp_path / ".env") == "600"
