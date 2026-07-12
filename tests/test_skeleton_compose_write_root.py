"""Regression guard for okengine#140.

Hermes v0.17.0 bakes ENV HERMES_WRITE_SAFE_ROOT=/opt/data and denies every agent
file-tool write outside it as a "protected system/credential file". The skeleton mounts
the vault at WIKI_PATH (/opt/vault, outside /opt/data), so without a widened safe root
the agent silently cannot write the vault. Assert the gateway sets HERMES_WRITE_SAFE_ROOT
and that WIKI_PATH is INSIDE it — guarding the invariant against future mount changes.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml"


def _gateway_env(text: str, key: str):
    # gateway is the first service; both gateway and mcp set WIKI_PATH (both /opt/vault),
    # HERMES_WRITE_SAFE_ROOT is gateway-only — a first-match line scan is sufficient.
    m = re.search(rf"^\s*-\s*{re.escape(key)}=(\S+)", text, re.M)
    return m.group(1) if m else None


def test_vault_mount_is_under_write_safe_root():
    text = COMPOSE.read_text(encoding="utf-8")
    wiki = _gateway_env(text, "WIKI_PATH")
    root = _gateway_env(text, "HERMES_WRITE_SAFE_ROOT")
    assert root, ("gateway must set HERMES_WRITE_SAFE_ROOT — Hermes v0.17.0's baked "
                  "/opt/data default denies agent writes to the vault (okengine#140)")
    assert wiki, "gateway must set WIKI_PATH"
    assert wiki == root or wiki.startswith(root.rstrip("/") + "/"), (
        f"WIKI_PATH={wiki} is outside HERMES_WRITE_SAFE_ROOT={root} — the agent's "
        "file-tool writes to the vault will be silently denied (okengine#140)")


def test_bare_compose_sequence_seeds_the_mcp_token_before_up():
    """okengine#208: the skeleton's documented bare-compose sequence went build -> `up -d`, but the
    shipped default OKENGINE_MCP_TOKEN makes the mcp container FAIL CLOSED on its 0.0.0.0 container
    bind (#50) and crash-loop — only ensure-runtime (a real generated token) saves that path, and it
    was only run by deploy.sh. The header must document ensure-runtime BEFORE compose up."""
    text = (REPO / "templates" / "pack" / "skeleton" / "docker-compose.yml").read_text()
    header = text.split("services:")[0]
    assert "ensure-runtime.sh" in header, "bare-compose sequence must seed the runtime/token"
    assert header.index("build-engine-image.sh") < header.index("ensure-runtime.sh") \
        < header.index("docker compose up"), "ensure-runtime must sit between build and up"
    assert "crash-loops (okengine#208)" in header      # the WHY travels with the sequence
