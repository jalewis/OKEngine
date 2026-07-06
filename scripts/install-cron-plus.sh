#!/usr/bin/env bash
# Install the cron-plus scheduler plugin into a pack's runtime so the gateway can
# run the cron fleet. cron-plus is a REQUIRED EXTERNAL dependency (pinned in
# engine-manifest.yaml `dependencies.cron-plus`), NOT vendored in this repo — this
# clones it at the pinned commit into <pack>/.hermes-data/plugins/cron-plus, which
# the gateway sees at /opt/data/plugins/cron-plus. The seeded config.yaml already
# lists `cron-plus` under plugins.enabled.
#
# Run BEFORE `docker compose up` (so the plugin is present when the gateway starts
# with cron-plus enabled). `deploy.sh` calls this automatically. Idempotent.
#
# Usage:
#   bash $ENGINE_DIR/scripts/install-cron-plus.sh [pack-dir]
#   CRON_PACK_DIR=/path/to/pack bash .../install-cron-plus.sh
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACK="$(cd "${1:-${CRON_PACK_DIR:-$PWD}}" && pwd)"
MANIFEST="$ENGINE_DIR/engine-manifest.yaml"

# Pull the upstream URL + pinned SHA from the manifest's dependencies.cron-plus.
URL="$(awk '/^  cron-plus:/{f=1} f&&/upstream:/{print $2; exit}' "$MANIFEST")"
SHA="$(awk '/^  cron-plus:/{f=1} f&&/pinned_sha:/{print $2; exit}' "$MANIFEST")"
URL="${URL:-https://github.com/jalewis/hermes-cron-plus.git}"
if [ -z "$SHA" ]; then
    echo "ERROR: could not read dependencies.cron-plus.pinned_sha from $MANIFEST" >&2
    exit 1
fi

DEST="$PACK/.hermes-data/plugins/cron-plus"
echo "  cron-plus: $URL @ ${SHA:0:12}"

if [ -d "$DEST/.git" ]; then
    if [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null)" = "$SHA" ]; then
        echo "  already at the pinned commit ($DEST) — skipping"
    else
        git -C "$DEST" fetch --quiet --depth 1 origin "$SHA" 2>/dev/null || git -C "$DEST" fetch --quiet origin
        # The plugin dir is an engine-MANAGED clone — the pin is authoritative, not a place for
        # local edits. A plain `checkout` ABORTS on any local modification, which blocks the WHOLE
        # deploy (this bit a live redeploy: an old-pin jobs.py carried a hand-ported TZ-aware patch,
        # so `checkout` refused and deploy.sh died at step 2). FORCE the tree to the pin instead,
        # but SURFACE any discarded change so a real hand-edit isn't lost silently (okengine#178).
        if [ -n "$(git -C "$DEST" status --porcelain 2>/dev/null)" ]; then
            echo "  ⚠ discarding LOCAL modification(s) in the managed cron-plus clone (the pin is authoritative):" >&2
            git -C "$DEST" status --porcelain 2>/dev/null | sed 's/^/        /' >&2
        fi
        git -C "$DEST" checkout --quiet --force "$SHA"
        echo "  updated to the pinned commit"
    fi
else
    mkdir -p "$(dirname "$DEST")"
    rm -rf "$DEST"
    git clone --quiet "$URL" "$DEST"
    git -C "$DEST" checkout --quiet "$SHA"
    echo "  cloned to $DEST"
fi

# Sanity: the CLI the cron helpers invoke must be present.
if [ ! -f "$DEST/cli.py" ]; then
    echo "  ⚠ $DEST/cli.py not found — the pinned cron-plus layout may have changed" >&2
fi

# Confirm the seeded config enables it (the template does; warn if a hand-edited
# config dropped it).
CFG="$PACK/.hermes-data/config.yaml"
if [ -f "$CFG" ] && ! grep -qE 'cron-plus' "$CFG"; then
    echo "  ⚠ $CFG does not list cron-plus under plugins.enabled — add it, else the fleet won't schedule" >&2
fi
echo "  done."
