# Fast gateway hotfix overlay for a write_server.py change (the enforced okengine-write MCP).
#
# WHY THIS EXISTS: write_server runs IN the gateway from the BAKED /opt/hermes/okengine-mcp/
# write_server.py — NOT any staged copy. So a change to the enforced write path does not go live by
# staging; it needs the gateway image. This thin overlay rebakes the one changed file onto the
# CURRENT gateway image without re-running build-engine-image.sh (which re-clones Hermes). The next
# proper build-engine-image.sh release bakes it canonically, so there is no drift to track.
#
# USAGE (from the engine repo root):
#   docker tag hermes-agent:latest hermes-agent:pre-degen-guard                          # backup, reversible
#   docker build -f scripts/gateway-write-server-overlay.Dockerfile -t hermes-agent:latest .
#   (cd <deploy> && ENGINE_DIR=<engine-dir> docker compose up -d --force-recreate --no-deps gateway)
#
# Pin BASE to a versioned tag instead of :latest with --build-arg BASE=hermes-agent:okengine-<ver>.
ARG BASE=hermes-agent:latest
FROM ${BASE}
COPY okengine-mcp/write_server.py /opt/hermes/okengine-mcp/write_server.py
