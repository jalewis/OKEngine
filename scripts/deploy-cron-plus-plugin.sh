#!/usr/bin/env bash
# Deploy cron-plus plugin from this repo into ~/.hermes/plugins/cron-plus/.
# Hermes auto-discovers plugins from ~/.hermes/plugins/, gated by the
# `plugins.enabled` allow-list in ~/.hermes/config.yaml.
#
# Usage:  bash scripts/deploy-cron-plus-plugin.sh
#         (run after editing plugins/cron-plus/*.py and committing)
#
# Idempotent. Snapshots existing files before overwriting.
# Restart the gateway (`docker compose restart gateway`) for changes
# to take effect — Python module imports are cached for the lifetime
# of the gateway process.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/plugins/cron-plus"
DEST_DIR="${HOME}/.hermes/plugins/cron-plus"

if [ ! -d "$SRC_DIR" ]; then
    echo "ERROR: $SRC_DIR not found. Are you running from the repo root?" >&2
    exit 1
fi

mkdir -p "$DEST_DIR"
TS="$(date +%Y%m%d-%H%M%S)"

shopt -s nullglob
deployed=0
for src in "$SRC_DIR"/*.py "$SRC_DIR"/*.yaml; do
    name="$(basename "$src")"
    dest="$DEST_DIR/$name"
    if [ -f "$dest" ]; then
        cp -p "$dest" "$dest.bak.$TS"
    fi
    cp -p "$src" "$dest"
    echo "  deployed: $name"
    deployed=$((deployed + 1))
done

if [ "$deployed" -eq 0 ]; then
    echo "  (no files found in $SRC_DIR)"
else
    echo "  $deployed file(s) deployed to $DEST_DIR"
    echo
    echo "  Restart gateway to load the changes:"
    echo "    HERMES_UID=\$(id -u) HERMES_GID=\$(id -g) docker compose restart gateway"
fi
