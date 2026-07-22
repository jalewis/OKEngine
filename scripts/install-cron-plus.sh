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
    if [ "$(gitd rev-parse HEAD 2>/dev/null)" != "$SHA" ]; then
        gitd fetch --quiet --depth 1 origin "$SHA" 2>/dev/null || gitd fetch --quiet origin
    fi
    # The plugin dir is an engine-MANAGED clone and carried patches deliberately
    # leave tracked modifications behind. Always restore the exact pin before
    # applying the CURRENT patch set: otherwise an evolved patch cannot apply to
    # the prior patched form, and non-idempotent patches accumulate duplicate
    # definitions on every deploy. Surface what is discarded, including helper
    # overlays copied below, then clean tracked + untracked managed content.
    if [ -n "$(gitd status --porcelain 2>/dev/null)" ]; then
        echo "  ⚠ discarding LOCAL/generated modification(s) in the managed cron-plus clone before refreshing the patch set:" >&2
        gitd status --porcelain 2>/dev/null | sed 's/^/        /' >&2
    fi
    gitd checkout --quiet --force "$SHA"
    gitd clean -fdq
    echo "  restored pinned cron-plus tree before applying carried patches"
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
apply_carried_patch "$ENGINE_DIR/patches/cron-plus/run-receipts.patch" \
    "OKEngine verified model-run receipts"
apply_carried_patch "$ENGINE_DIR/patches/cron-plus/cli-null-next-run.patch" \
    "OKEngine null-safe manual run output"
cp "$ENGINE_DIR/patches/cron-plus/after_ordering.py" "$DEST/after_ordering.py"
cp "$ENGINE_DIR/patches/cron-plus/run_receipts.py" "$DEST/run_receipts.py"
cp "$ENGINE_DIR/patches/cron-plus/model_slots.py" "$DEST/model_slots.py"
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
