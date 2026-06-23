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

# Run as the SAME uid the gateway runs as (compose `user: ${HERMES_UID:-10000}`),
# NOT the image's `hermes` name (10000): a deployment that overrides HERMES_UID
# (e.g. to the host operator's uid) owns /opt/data with that uid, so `-u hermes`
# would mismatch and hit permission-denied on jobs.json / the .tick.lock.
HERMES_UID="${HERMES_UID:-10000}"

# Target the running pack gateway container by compose-service label, so this
# works regardless of which pack dir the stack was brought up from (#19) — the
# deployed compose lives in the pack, not the engine repo.
CONTAINER="$(docker ps --filter 'label=com.docker.compose.service=gateway' \
                       --filter 'status=running' --format '{{.Names}}' | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up?)." >&2
    exit 1
fi

exec docker exec -i -u "$HERMES_UID" "$CONTAINER" \
    /opt/hermes/.venv/bin/python /opt/data/plugins/cron-plus/cli.py "$@"
