#!/usr/bin/env bash
# cron-plus log tailer — surface daemon + per-job activity.
#
# Hermes' gateway only sends WARNING+ to stderr (so `docker compose logs
# gateway` doesn't show cron-plus INFO). The full log stream lives INSIDE the
# gateway container under /opt/data/logs/ (the mounted pack .hermes-data/logs),
# NOT the host ~/.hermes — okengine deployments are containerized (#138), so
# this helper shells into the pack's gateway to read them, exactly like its
# sibling cron-plus.sh does.
#
# Usage:
#   bash scripts/cron-plus-logs.sh                  # follow daemon ticker activity
#   bash scripts/cron-plus-logs.sh runs             # list recent per-job log files
#   bash scripts/cron-plus-logs.sh runs <name|id>   # tail latest run for a specific job
#   bash scripts/cron-plus-logs.sh errors           # WARNING+ across all cron-plus loggers
#
# Scope the pack on a multi-pack host with CRON_PACK_DIR=<pack> (same as cron-plus.sh).
#
# Log locations (inside the gateway container):
#   /opt/data/logs/agent.log              — gateway centralized log (daemon ticker)
#   /opt/data/logs/cron-plus/*.log        — per-spawn subprocess logs

set -euo pipefail

LOGS="/opt/data/logs"
AGENT_LOG="$LOGS/agent.log"
CRON_PLUS_DIR="$LOGS/cron-plus"

# Target the pack's gateway. With CRON_PACK_DIR set, scope to THAT pack's compose project
# (the right gateway on a multi-pack host, okengine#108). Without it, fall back to the gateway
# label — but REFUSE if more than one matches, rather than silently picking the wrong pack.
if [ -n "${CRON_PACK_DIR:-}" ]; then
    CONTAINER="$(docker compose -f "$CRON_PACK_DIR/docker-compose.yml" ps -q gateway 2>/dev/null | head -1 || true)"
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

# Run as the SAME uid the gateway runs as (the owner of /opt/data, 700), unless the operator
# set HERMES_UID explicitly — a `-u 10000` exec can't traverse a pack that overrides the uid
# (okengine#136). Auto-detect from the owner of /opt/data.
HERMES_UID="${HERMES_UID:-}"
if [ -z "$HERMES_UID" ]; then
    HERMES_UID="$(docker exec "$CONTAINER" stat -c %u /opt/data 2>/dev/null || true)"
    HERMES_UID="${HERMES_UID:-10000}"
fi

dx() { docker exec -i -u "$HERMES_UID" "$CONTAINER" "$@"; }

# FAIL LOUDLY if the container has no log dir yet — a missing dir must be distinguishable from a
# healthy-but-quiet scheduler, not silently swallowed by a 2>/dev/null tail (#15).
if ! dx test -d "$LOGS"; then
    echo "ERROR: $LOGS not found in gateway $CONTAINER — the runtime log dir is missing." >&2
    echo "       (has the gateway started and seeded .hermes-data/logs? check ensure-runtime.sh)" >&2
    exit 1
fi

case "${1:-tail}" in
    tail|"")
        if ! dx test -f "$AGENT_LOG"; then
            echo "ERROR: $AGENT_LOG not found in gateway $CONTAINER — nothing to follow yet." >&2
            exit 1
        fi
        # Daemon ticker activity — tick claims, spawns, completions
        exec dx tail -F "$AGENT_LOG" | grep --line-buffered -E "cron-?plus|cron_plus"
        ;;

    runs)
        if ! dx test -d "$CRON_PLUS_DIR"; then
            echo "ERROR: $CRON_PLUS_DIR not found in gateway $CONTAINER — no per-job runs logged yet." >&2
            exit 1
        fi
        if [ -n "${2:-}" ]; then
            # Find latest log for a specific job (matches name or id)
            latest="$(dx sh -c "ls -t '$CRON_PLUS_DIR'/*'$2'* 2>/dev/null | head -1")"
            if [ -z "$latest" ]; then
                echo "No log found matching: $2" >&2
                echo "Recent runs:" >&2
                dx sh -c "ls -t '$CRON_PLUS_DIR' 2>/dev/null | head -20" >&2
                exit 1
            fi
            echo "==> $latest" >&2
            exec dx tail -100 "$latest"
        fi
        # No filter — list recent runs
        dx sh -c "ls -lt '$CRON_PLUS_DIR' 2>/dev/null | head -20"
        ;;

    errors)
        if ! dx test -f "$AGENT_LOG"; then
            echo "ERROR: $AGENT_LOG not found in gateway $CONTAINER — nothing to scan yet." >&2
            exit 1
        fi
        # WARNING+ from all cron-plus loggers across the gateway
        dx sh -c "grep -E '(WARNING|ERROR|CRITICAL).*(cron-?plus|cron_plus)' '$AGENT_LOG' 2>/dev/null | tail -30"
        ;;

    *)
        echo "Usage: $0 [tail|runs [name|id]|errors]" >&2
        exit 1
        ;;
esac
