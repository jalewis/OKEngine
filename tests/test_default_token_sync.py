"""okengine#326 [31]: DEFAULT_LOCAL_TOKEN must AGREE across the fail-closed surfaces — the two MCP
servers, the hardening lib, and the compose skeleton default. Before this the sync was enforced only
by a comment; a drift means the exposed-default-token guards (which refuse to serve when the token is
the well-known default on a non-loopback bind) compare against DIFFERENT 'defaults' on different
surfaces, so one guard stops recognizing the well-known token and the fail-closed posture silently
develops a hole. The deterministic audit's constant-drift dimension flags this too; this is the
unit-gate red test that makes it a merge blocker."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_PY = re.compile(r'DEFAULT_LOCAL_TOKEN\s*=\s*"([^"]+)"')
_COMPOSE = re.compile(r'OKENGINE_MCP_TOKEN=\$\{OKENGINE_MCP_TOKEN:-([^}]+)\}')

SURFACES = (
    ("okengine-mcp/server.py", _PY),
    ("okengine-mcp/write_server.py", _PY),
    ("scripts/cron/hardening_lib.py", _PY),
    ("templates/pack/skeleton/docker-compose.yml", _COMPOSE),
)


def test_default_local_token_agrees_across_surfaces():
    vals = {}
    for rel, pat in SURFACES:
        p = REPO / rel
        if not p.is_file():                      # publish-excluded surface absent on the scrubbed tree
            continue
        m = pat.search(p.read_text(encoding="utf-8"))
        assert m, f"DEFAULT_LOCAL_TOKEN / compose default not found in {rel} — did the declaration form change?"
        vals[rel] = m.group(1)
    assert len(vals) >= 3, f"expected the token on multiple surfaces, found {vals}"
    assert len(set(vals.values())) == 1, (
        f"DEFAULT_LOCAL_TOKEN drifted across the fail-closed surfaces: {vals} — the well-known-token "
        f"exposure guards would compare against different defaults. Keep them in lockstep.")
