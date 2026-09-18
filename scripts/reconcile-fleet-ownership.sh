#!/usr/bin/env bash
# Host-side, non-model ownership reconciliation for deployed OKEngine packs.
#
# The gateway cron runs as the lane uid and cannot repair files accidentally
# created by root. This wrapper intentionally stays on the host, serializes
# itself, and delegates to the narrowly scoped fix-vault-ownership.sh repair.
#
# Usage: reconcile-fleet-ownership.sh <deployment-dir> [...]
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$#" -gt 0 ] || {
  echo "usage: reconcile-fleet-ownership.sh <deployment-dir> [...]" >&2
  exit 2
}

exec 9>"/tmp/okengine-ownership-reconcile.lock"
flock -n 9 || exit 0

failed=0
for deployment in "$@"; do
  if [ ! -f "$deployment/docker-compose.yml" ]; then
    echo "ownership-reconcile: skip invalid deployment: $deployment" >&2
    failed=1
    continue
  fi
  echo "ownership-reconcile: $(date -u +%Y-%m-%dT%H:%M:%SZ) $deployment"
  if ! bash "$ENGINE_DIR/scripts/fix-vault-ownership.sh" "$deployment"; then
    failed=1
  fi
done
exit "$failed"
