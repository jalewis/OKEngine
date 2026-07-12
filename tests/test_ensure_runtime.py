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
    e = dict(os.environ, HERMES_UID=SELF_UID, HERMES_GID=SELF_GID,
             OKENGINE_CRON_PLUS_SKIP="1")   # hermetic: no plugin clone in tests
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
    # seeded verbatim from the template, except ensure-runtime rewrites the MCP Bearer to a
    # generated secret token (okengine#105) — compare modulo that one line.
    import re
    _norm = lambda s: re.sub(r"Bearer \S+", "Bearer X", s)
    assert _norm(cfg.read_text()) == _norm(TEMPLATE.read_text())


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
    e = dict(os.environ, HERMES_UID="99999", HERMES_GID="99999", OKENGINE_CRON_PLUS_SKIP="1")
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)],
                       capture_output=True, text=True, timeout=30, env=e)
    assert r.returncode == 1
    assert "not writable by 99999" in r.stderr
    assert "--fix-perms" in r.stderr and "chown" in r.stderr


def test_fix_perms_makes_tree_writable(tmp_path):
    """--fix-perms makes the tree world-writable so a non-matching gateway uid can
    write it (the documented local-deploy remedy)."""
    e = dict(os.environ, HERMES_UID="99999", HERMES_GID="99999", OKENGINE_CRON_PLUS_SKIP="1")
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path), "--fix-perms"],
                       capture_output=True, text=True, timeout=30, env=e)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "fix-perms" in r.stdout
    # .hermes-data is now other-writable
    mode = oct((tmp_path / ".hermes-data").stat().st_mode)[-3:]
    assert mode[-1] in ("6", "7")   # other has write


def test_generates_secret_token_on_fresh_deploy(tmp_path):
    """okengine#105: a fresh pack (no .env) must get a GENERATED secret OKENGINE_MCP_TOKEN,
    not the built-in 'okengine-local' (which the read MCP refuses on its 0.0.0.0 bind →
    crash-loop), and the seeded config's Bearer must be rewritten to match."""
    import re
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    env = (tmp_path / ".env").read_text()
    m = re.search(r"(?m)^OKENGINE_MCP_TOKEN=(\S+)", env)
    assert m, f"no OKENGINE_MCP_TOKEN written to .env:\n{env}"
    tok = m.group(1)
    assert tok != "okengine-local" and len(tok) >= 24, f"not a generated secret: {tok!r}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert f"Bearer {tok}" in cfg, "config Bearer not synced to the generated token"
    assert "Bearer okengine-local" not in cfg, "default token still present in config"


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


# (okengine#105) No-token-in-.env no longer leaves the built-in "okengine-local" default —
# ensure-runtime generates a secret; covered by test_generates_secret_token_on_fresh_deploy.


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


def test_mcp_url_uses_service_name_not_host_port(tmp_path):
    """okengine#138 (supersedes #137): the gateway shares the compose bridge with okengine-mcp and
    reaches the read-MCP by SERVICE NAME on the container port (okengine-mcp:8730) — no host port,
    no port_offset, no cross-pack collision. So an offset pack's MCP url is the service name, NOT a
    rewritten localhost host-port."""
    (tmp_path / "pack.yaml").write_text("name: demo\nport_offset: 100\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert "http://okengine-mcp:8730/mcp" in cfg      # service name on the bridge
    assert "localhost:8830" not in cfg                # not the superseded #137 offset host-port form
    # (the template's explanatory comment legitimately mentions localhost:8730 — don't over-assert)


def test_no_offset_keeps_default_mcp_url(tmp_path):
    """No port_offset -> the template default :8730 is kept (back-compat)."""
    (tmp_path / "pack.yaml").write_text("name: demo\n")
    r = _run([str(tmp_path)])
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    cfg = (tmp_path / ".hermes-data" / "config.yaml").read_text()
    assert "http://localhost:8730/mcp" in cfg


def test_iwe_only_in_mcp():
    """okengine#179: backlinks are built by an in-process link-scanner, so iwe is used ONLY by
    the MCP's graph tools (kb_graph). It must stay sha-pinned in the MCP image, and must NOT be
    fetched anywhere it's no longer needed (reader/cockpit images, ensure-runtime staging)."""
    import re
    mcp = (REPO / "okengine-mcp" / "Dockerfile").read_text()
    assert re.search(r'ARG IWE_VERSION=[0-9.]+', mcp) and re.search(r'ARG IWE_SHA256=[0-9a-f]{64}', mcp), \
        "MCP must keep iwe pinned + sha-verified — it's the sole remaining iwe user"
    for f in ("okengine-reader/Dockerfile", "okengine-cockpit/Dockerfile", "scripts/ensure-runtime.sh"):
        assert "iwe-org/iwe/releases" not in (REPO / f).read_text(), \
            f"{f} still downloads iwe, but it no longer needs it (backlinks are in-process, #179)"


def test_template_api_server_toolset_is_locked():
    """Secure-by-default: the engine config template's api_server (Agent Chat) toolset must grant
    ONLY vault read + enforced write — never web/terminal/code_execution/file/computer_use — so a
    deployment that enables chat can't inherit a broad toolset by omission (docs/agent-chat.md's
    'do not skip this' footgun, made the default). Add `web` per-deployment, deliberately."""
    import yaml
    cfg = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    api = (cfg.get("platform_toolsets") or {}).get("api_server")
    assert api == ["okengine", "okengine-write"], api
    forbidden = {"terminal", "code_execution", "coding", "file", "computer_use", "web", "shell"}
    assert not (set(api) & forbidden), f"chat toolset leaked a dangerous tool: {set(api) & forbidden}"


def test_pins_hermes_uid_into_env_for_bare_compose(tmp_path):
    """invariant-audit HIGH #1: ensure-runtime seeded the runtime but never pinned HERMES_UID into
    .env, so a bare `docker compose up` (the documented step-by-step path) interpolated
    ${HERMES_UID:-10000} and ran the gateway as a DIFFERENT uid than the tree — a silently dead
    scheduler. It must pin the resolved uid:gid into .env (like deploy.sh) so compose uses the same."""
    (tmp_path / "wiki").mkdir()
    (tmp_path / ".env").write_text("OKENGINE_MCP_TOKEN=okengine-local\n")
    # NO HERMES_UID in the env -> the resolver must fall to the tree owner (this uid) and pin it
    e = dict(os.environ, OKENGINE_CRON_PLUS_SKIP="1")
    e.pop("HERMES_UID", None); e.pop("HERMES_GID", None)
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True,
                       timeout=30, env=e)
    assert r.returncode == 0, r.stderr
    env_txt = (tmp_path / ".env").read_text()
    assert f"HERMES_UID={SELF_UID}" in env_txt, f"HERMES_UID not pinned:\n{env_txt}"
    assert f"HERMES_GID={SELF_GID}" in env_txt


def test_existing_uid_pin_is_not_clobbered(tmp_path):
    """A deliberate operator pin (portable/shared vault) must survive — pin only when absent."""
    (tmp_path / "wiki").mkdir()
    # pin our OWN uid (so the writability gate passes) — the point is no DUPLICATE line is appended
    (tmp_path / ".env").write_text(f"HERMES_UID={SELF_UID}\nHERMES_GID={SELF_GID}\nOKENGINE_MCP_TOKEN=x\n")
    e = dict(os.environ, OKENGINE_CRON_PLUS_SKIP="1", HERMES_UID=SELF_UID, HERMES_GID=SELF_GID)
    r = subprocess.run(["bash", str(SCRIPT), str(tmp_path)], capture_output=True, text=True,
                       timeout=30, env=e)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".env").read_text().count("HERMES_UID=") == 1
    assert (tmp_path / ".env").read_text().count("HERMES_GID=") == 1
