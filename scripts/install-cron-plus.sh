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

# The managed clone lives under the pack's .hermes-data, which deploy.sh chowns to HERMES_UID (so
# the container's uid-1003 scheduler can write it). When that uid != the operator running the deploy,
# every `git -C "$DEST"` here aborts with "detected dubious ownership in repository" and bricks the
# deploy at step 2 — and git's own hint (chown the tree, or add a --global safe.directory) is a trap:
# chowning it away from HERMES_UID breaks the container. Scope safe.directory to THIS repo instead —
# no chown, container ownership preserved (invariant-audit B7.1).
gitd() { git -C "$DEST" -c safe.directory="$DEST" "$@"; }

if [ -d "$DEST/.git" ]; then
    if [ "$(gitd rev-parse HEAD 2>/dev/null)" = "$SHA" ]; then
        echo "  already at the pinned commit ($DEST) — skipping"
    else
        gitd fetch --quiet --depth 1 origin "$SHA" 2>/dev/null || gitd fetch --quiet origin
        # The plugin dir is an engine-MANAGED clone — the pin is authoritative, not a place for
        # local edits. A plain `checkout` ABORTS on any local modification, which blocks the WHOLE
        # deploy (this bit a live redeploy: an old-pin jobs.py carried a hand-ported TZ-aware patch,
        # so `checkout` refused and deploy.sh died at step 2). FORCE the tree to the pin instead,
        # but SURFACE any discarded change so a real hand-edit isn't lost silently (okengine#178).
        if [ -n "$(gitd status --porcelain 2>/dev/null)" ]; then
            echo "  ⚠ discarding LOCAL modification(s) in the managed cron-plus clone (the pin is authoritative):" >&2
            gitd status --porcelain 2>/dev/null | sed 's/^/        /' >&2
        fi
        gitd checkout --quiet --force "$SHA"
        echo "  updated to the pinned commit"
    fi
else
    mkdir -p "$(dirname "$DEST")"
    rm -rf "$DEST"
    git clone --quiet "$URL" "$DEST"
    gitd checkout --quiet "$SHA"
    echo "  cloned to $DEST"
fi

# Apply OKEngine's small dependency-boundary capabilities in a fixed order.
# Fail loudly when a future pin changes their context; silently losing runtime
# configuration or `after:` enforcement would be worse than stopping deploy.
apply_carried_patch() {
    local patch="$1" label="$2"
    if [ ! -f "$patch" ]; then
        echo "ERROR: required cron-plus patch not found: $patch" >&2
        exit 1
    fi
    if gitd apply --check "$patch" >/dev/null 2>&1; then
        gitd apply "$patch"
        echo "  applied $label"
    elif gitd apply --reverse --check "$patch" >/dev/null 2>&1; then
        echo "  $label already applied"
    else
        echo "ERROR: $patch does not apply to cron-plus pin ${SHA:0:12}" >&2
        echo "       refresh the carried patch when updating dependencies.cron-plus.pinned_sha" >&2
        exit 1
    fi
}

apply_carried_patch "$ENGINE_DIR/patches/cron-plus/job-env.patch" \
    "OKEngine per-job environment support"
apply_carried_patch "$ENGINE_DIR/patches/cron-plus/after-ordering.patch" \
    "OKEngine after: runtime ordering"
cp "$ENGINE_DIR/patches/cron-plus/after_ordering.py" "$DEST/after_ordering.py"
echo "  installed OKEngine after: policy overlay"

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
