#!/usr/bin/env bash
# cron-plus log tailer — surface daemon + per-job activity.
#
# Hermes' gateway only sends WARNING+ to stderr (so `docker compose logs
# gateway` doesn't show cron-plus INFO). The full log stream lives in
# ~/.hermes/logs/. This helper makes it easy to see what cron-plus is
# actually doing.
#
# Usage:
#   bash scripts/cron-plus-logs.sh                  # follow daemon ticker activity
#   bash scripts/cron-plus-logs.sh runs             # list recent per-job log files
#   bash scripts/cron-plus-logs.sh runs <name|id>   # tail latest run for a specific job
#   bash scripts/cron-plus-logs.sh errors           # WARNING+ across all cron-plus loggers
#
# Log locations:
#   ~/.hermes/logs/agent.log              — gateway centralized log (daemon ticker)
#   ~/.hermes/logs/cron-plus/*.log        — per-spawn subprocess logs

set -euo pipefail

LOGS="${HERMES_HOME:-$HOME/.hermes}/logs"
AGENT_LOG="$LOGS/agent.log"
CRON_PLUS_DIR="$LOGS/cron-plus"

case "${1:-tail}" in
    tail|"")
        # Daemon ticker activity — tick claims, spawns, completions
        exec tail -F "$AGENT_LOG" 2>/dev/null | grep --line-buffered -E "cron-?plus|cron_plus"
        ;;

    runs)
        if [ -n "${2:-}" ]; then
            # Find latest log for a specific job (matches name or id)
            latest="$(ls -t "$CRON_PLUS_DIR"/*"$2"* 2>/dev/null | head -1)"
            if [ -z "$latest" ]; then
                echo "No log found matching: $2" >&2
                echo "Recent runs:" >&2
                ls -t "$CRON_PLUS_DIR" 2>/dev/null | head -20 >&2
                exit 1
            fi
            echo "==> $latest" >&2
            exec tail -100 "$latest"
        fi
        # No filter — list recent runs
        ls -lt "$CRON_PLUS_DIR" 2>/dev/null | head -20
        ;;

    errors)
        # WARNING+ from all cron-plus loggers across the gateway
        grep -E "(WARNING|ERROR|CRITICAL).*(cron-?plus|cron_plus)" "$AGENT_LOG" 2>/dev/null | tail -30
        ;;

    *)
        echo "Usage: $0 [tail|runs [name|id]|errors]" >&2
        exit 1
        ;;
esac
