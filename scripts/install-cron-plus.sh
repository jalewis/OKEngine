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
    # The plugin dir is an engine-MANAGED clone. Before hermes-cron-plus v0.2.0 the engine patched it
    # in place and copied overlay modules in, so a host's clone still carries those edits and untracked
    # files, which the new pin now tracks. Always restore the exact pin: surface what is discarded,
    # clean untracked content FIRST (a checkout refuses to overwrite an untracked file the target
    # commit tracks), then force the tracked tree to the pin.
    if [ -n "$(gitd status --porcelain 2>/dev/null)" ]; then
        echo "  ⚠ discarding LOCAL/generated modification(s) in the managed cron-plus clone before restoring the pin:" >&2
        gitd status --porcelain 2>/dev/null | sed 's/^/        /' >&2
    fi
    gitd clean -fdq
    gitd checkout --quiet --force "$SHA"
    gitd clean -fdq
    echo "  restored pinned cron-plus tree"
else
    mkdir -p "$(dirname "$DEST")"
    rm -rf "$DEST"
    git clone --quiet "$URL" "$DEST"
    gitd checkout --quiet "$SHA"
    echo "  cloned to $DEST"
fi

# The changes OKEngine once carried as patches live upstream since hermes-cron-plus v0.2.0, so the
# pinned tree is installed as-is. The engine keeps a mirror of the plugin's policy modules in
# patches/cron-plus/*.py, where they are unit- and mutation-tested; the pinned upstream file must be
# byte-identical to that mirror, or the engine has tested code that this deploy would not run. Fail
# loudly: a silent mismatch would put untested scheduler behavior in front of the whole cron fleet.
mirror_drift=0
for mirror in "$ENGINE_DIR"/patches/cron-plus/*.py; do
    name="$(basename "$mirror")"
    if ! cmp -s "$mirror" "$DEST/$name"; then
        echo "ERROR: cron-plus pin ${SHA:0:12} $name differs from the engine's tested copy patches/cron-plus/$name" >&2
        mirror_drift=1
    fi
done
if [ "$mirror_drift" -ne 0 ]; then
    echo "       land the change in hermes-cron-plus and the engine mirror together, then move the pin" >&2
    exit 1
fi
# The deploy writes the live job store under the scheduler's lock (scripts/cron_plus_store.py
# LOCK_NAME); the pinned scheduler must take that same sidecar lock (okengine#850).
if ! grep -qF 'JOBS_LOCK_FILE = CRON_PLUS_HOME / "jobs.json.lock"' "$DEST/jobs.py"; then
    echo "ERROR: cron-plus pin ${SHA:0:12} does not lock the job store through jobs.json.lock" >&2
    exit 1
fi
echo "  verified the pinned policy modules match the engine's tested copies"

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
