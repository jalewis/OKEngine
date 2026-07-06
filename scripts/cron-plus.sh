#!/usr/bin/env bash
# cron-plus host wrapper — runs the plugin's CLI inside the gateway container.
#
# Usage:
#   bash scripts/cron-plus.sh list
#   bash scripts/cron-plus.sh run <id>
#   bash scripts/cron-plus.sh create '0 10 * * 0' --name foo --workdir /opt/vault --prompt 'do x' --deliver telegram
#   bash scripts/cron-plus.sh tick
#
# Why this exists: Hermes' plugin loader only registers plugin CLI
# subcommands during agent invocations (`hermes chat` etc.), not for
# arbitrary `hermes <name>` calls. Until that's fixed upstream we shell
# into the container and run the plugin's CLI directly.

set -euo pipefail

# Run as the SAME uid the gateway runs as. A deployment that overrides HERMES_UID
# (e.g. to the host operator's uid) owns /opt/data 700 with that uid, so a `-u 10000`
# exec can't even traverse /opt/data and fails "can't open file … cli.py" (okengine#136).
# An explicit HERMES_UID wins; otherwise AUTO-DETECT it from the running gateway (the
# owner of /opt/data) once we've found the container — never assume the 10000 default.
HERMES_UID="${HERMES_UID:-}"

# Target the pack's gateway. With CRON_PACK_DIR set, scope to THAT pack's compose project
# (the right gateway on a multi-pack host, okengine#108). Without it, fall back to the gateway
# label — but REFUSE if more than one matches, rather than silently picking the wrong pack.
if [ -n "${CRON_PACK_DIR:-}" ]; then
    CONTAINER="$(docker compose -f "$CRON_PACK_DIR/docker-compose.yml" ps -q gateway 2>/dev/null | head -1)"
else
    mapfile -t _GWS < <(docker ps --filter 'label=com.docker.compose.service=gateway' \
                                  --filter 'status=running' --format '{{.Names}}')
    if [ "${#_GWS[@]}" -gt 1 ]; then
        echo "ERROR: ${#_GWS[@]} gateways running (${_GWS[*]}); set CRON_PACK_DIR=<pack> to pick one." >&2
        exit 1
    fi
    CONTAINER="${_GWS[0]:-}"
fi
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up? set CRON_PACK_DIR=<pack>)." >&2
    exit 1
fi

# Auto-detect the gateway's uid from the owner of /opt/data (the uid that owns it and
# can traverse it), unless the operator set HERMES_UID explicitly (okengine#136).
if [ -z "$HERMES_UID" ]; then
    HERMES_UID="$(docker exec "$CONTAINER" stat -c %u /opt/data 2>/dev/null || true)"
    HERMES_UID="${HERMES_UID:-10000}"
fi

exec docker exec -i -u "$HERMES_UID" "$CONTAINER" \
    /opt/hermes/.venv/bin/python /opt/data/plugins/cron-plus/cli.py "$@"
