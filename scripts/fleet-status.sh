#!/usr/bin/env bash
# Fleet health view (okengine#64) — per-lane run outcomes + silent-failure signals.
# Runs fleet_status.py INSIDE the gateway (where /opt/data lives). Run from the deployment
# dir (where docker-compose.yml is), the same place as post_deploy_verify.sh:
#
#     bash <engine>/scripts/fleet-status.sh [window_hours]
#
# Exit 0 = no critical signals; exit 1 = a vault-write-denial / provider-payment error fired.
set -uo pipefail

GW=${OKENGINE_GATEWAY_SVC:-gateway}
WINDOW=${1:-24}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# locate the gateway container (compose service here, else by name)
CID="$(docker compose ps -q "$GW" 2>/dev/null | head -1)"
[ -z "$CID" ] && CID="$(docker ps --filter "name=gateway" --format '{{.ID}}' 2>/dev/null | head -1)"
if [ -z "$CID" ]; then
    echo "fleet-status: no gateway container found (run from the deployment dir)." >&2
    exit 2
fi

# pipe the analyzer in on stdin; argv[1] = the in-container data dir.
docker exec -i "$CID" python3 - /opt/data "$WINDOW" < "$HERE/fleet_status.py"
