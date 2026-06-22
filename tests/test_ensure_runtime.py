"""Smoke tests for scripts/ensure-runtime.sh — seeds a pack's .hermes-data/
config.yaml (host-owned) before docker compose up, for git-cloned library packs
that have no committed runtime."""
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "ensure-runtime.sh"
TEMPLATE = REPO / "config" / "config.yaml.template"

# Run as the current uid so the writability check passes (the gateway-uid mismatch
# path is exercised separately below).
SELF_UID = str(os.getuid())
SELF_GID = str(os.getgid())


def _run(args, **env):
    e = dict(os.environ, HERMES_UID=SELF_UID, HERMES_GID=SELF_GID)
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=30, env=e)


def test_script_exists_and_parses():
    assert SCRIPT.is_file()
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_seeds_missing_runtime(tmp_path):
    """A fresh clone (no .hermes-data) gets config.yaml + qmd/ + logs/."""
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = tmp_path / ".hermes-data" / "config.yaml"
    assert cfg.is_file()
    assert (tmp_path / ".hermes-data" / "qmd").is_dir()
    assert (tmp_path / ".hermes-data" / "logs").is_dir()
    assert (tmp_path / ".hermes-data" / ".gitkeep").is_file()
    assert cfg.read_text() == TEMPLATE.read_text()


def test_idempotent_does_not_clobber(tmp_path):
    """An existing config.yaml (the operator's, with secrets/edits) is untouched."""
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    cfg = rt / "config.yaml"
    cfg.write_text("# my edited config\nterminal:\n  backend: local\n")
    before = cfg.read_text()
    r = _run([str(tmp_path)])
    assert r.returncode == 0
    assert cfg.read_text() == before          # not clobbered
    assert "already present" in r.stdout


def test_fails_when_not_writable_by_gateway_uid(tmp_path):
    """If the pack tree isn't writable by HERMES_UID, fail BEFORE compose with an
    actionable message (issue #16) — not a silent broken start."""
    e = dict(os.environ, HERMES_UID="99999", HERMES_GID="99999")
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)],
                       capture_output=True, text=True, timeout=30, env=e)
    assert r.returncode == 1
    assert "not writable by 99999" in r.stderr
    assert "--fix-perms" in r.stderr and "chown" in r.stderr


def test_fix_perms_makes_tree_writable(tmp_path):
    """--fix-perms makes the tree world-writable so a non-matching gateway uid can
    write it (the documented local-deploy remedy)."""
    e = dict(os.environ, HERMES_UID="99999", HERMES_GID="99999")
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path), "--fix-perms"],
                       capture_output=True, text=True, timeout=30, env=e)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "fix-perms" in r.stdout
    # .hermes-data is now other-writable
    mode = oct((tmp_path / ".hermes-data").stat().st_mode)[-3:]
    assert mode[-1] in ("6", "7")   # other has write


def test_seeded_config_passes_validator(tmp_path):
    """The seeded config.yaml satisfies the validator's runtime-config key checks
    (so a clone -> ensure-runtime -> validate flow is clean on that check)."""
    import importlib.util
    import sys
    _run([str(tmp_path)])
    spec = importlib.util.spec_from_file_location(
        "framework_validate", REPO / "scripts" / "framework_validate.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["framework_validate"] = m
    spec.loader.exec_module(m)
    r = m.Report()
    m.check_runtime_config(tmp_path, r)
    assert not any(s == "FAIL" for s, c, d in r.rows), [(c, d) for s, c, d in r.rows]


def test_mcp_token_synced_from_env(tmp_path):
    """When .env sets OKENGINE_MCP_TOKEN, the seeded read-MCP Authorization header
    is rewritten to match (okengine#32) — otherwise the gateway agent 401s."""
    (tmp_path / ".env").write_text('OKENGINE_MCP_TOKEN="s3cr3t-xyz"\n')
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert 'Authorization: "Bearer s3cr3t-xyz"' in cfg
    assert "<OKENGINE_MCP_TOKEN" not in cfg, "template placeholder must be gone"


def test_mcp_token_defaults_to_local(tmp_path):
    """No token in .env → header is the built-in local default the read server
    accepts, never the unsubstituted placeholder (okengine#32)."""
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert 'Authorization: "Bearer okengine-local"' in cfg
    assert "<OKENGINE_MCP_TOKEN" not in cfg


def test_mcp_token_synced_unquoted_header(tmp_path):
    """The header may be unquoted YAML (`Authorization: Bearer x`), as seeded by
    older templates. Sync must handle that form too — not just the quoted
    template — and must run on an already-present config (okengine#32)."""
    rt = tmp_path / ".hermes-data"
    rt.mkdir()
    (rt / "config.yaml").write_text(
        "terminal:\n  backend: local\n"
        "mcp_servers:\n  okengine:\n    headers:\n"
        "      Authorization: Bearer <OKENGINE_MCP_TOKEN from pack .env>\n"
        "  okengine-write:\n    command: x\n"
    )
    (tmp_path / ".env").write_text("OKENGINE_MCP_TOKEN=abc123\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (rt / "config.yaml").read_text()
    assert "Authorization: Bearer abc123" in cfg, cfg
    assert "<OKENGINE_MCP_TOKEN" not in cfg
