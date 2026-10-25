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

# Verify the pin BEFORE the managed clone moves to it (okengine#1069): build the candidate tree in a
# scratch directory, run the mirror and lock checks there, and switch the managed clone only when both
# pass. A refused pin then leaves the clone at its previous HEAD with its tree untouched. Fetching the
# pin's objects into an existing clone changes neither its HEAD nor its files.
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
CANDIDATE="$SCRATCH/tree"
if [ -d "$DEST/.git" ]; then
    if [ "$(gitd rev-parse HEAD 2>/dev/null)" != "$SHA" ]; then
        gitd fetch --quiet --depth 1 origin "$SHA" 2>/dev/null || gitd fetch --quiet origin
    fi
    mkdir "$CANDIDATE"
    gitd archive "$SHA" | tar -x -C "$CANDIDATE"
else
    git clone --quiet "$URL" "$CANDIDATE"
    git -C "$CANDIDATE" checkout --quiet "$SHA"
fi

# The changes OKEngine once carried as patches live upstream since hermes-cron-plus v0.2.0, so the
# pinned tree is installed as-is. The engine keeps a mirror of the plugin's policy modules in
# patches/cron-plus/*.py, where they are unit- and mutation-tested; the pinned upstream file must be
# byte-identical to that mirror, or the engine has tested code that this deploy would not run. Fail
# loudly: a silent mismatch would put untested scheduler behavior in front of the whole cron fleet.
mirror_drift=0
for mirror in "$ENGINE_DIR"/patches/cron-plus/*.py; do
    name="$(basename "$mirror")"
    if ! cmp -s "$mirror" "$CANDIDATE/$name"; then
        echo "ERROR: cron-plus pin ${SHA:0:12} $name differs from the engine's tested copy patches/cron-plus/$name" >&2
        mirror_drift=1
    fi
done
if [ "$mirror_drift" -ne 0 ]; then
    echo "       land the change in hermes-cron-plus and the engine mirror together, then move the pin" >&2
    exit 1
fi
# The deploy writes the live job store under the scheduler's lock (scripts/cron_plus_store.py
# LOCK_NAME, next to jobs.json); the pinned scheduler must take that same sidecar lock (okengine#850).
# Import the pinned jobs.py and read the paths it resolves, rather than matching its source text: a
# pin that keeps the expected line and rebinds JOBS_LOCK_FILE later would pass a text check
# (okengine#1068). HERMES_HOME points at a throwaway directory and -B writes no __pycache__, so the
# import touches no real state.
lock_home="$SCRATCH/hermes-home"
mkdir "$lock_home"
lock_rc=0
HERMES_HOME="$lock_home" python3 -I -B - "$CANDIDATE" "$ENGINE_DIR/scripts/cron_plus_store.py" <<'PY' || lock_rc=$?
import importlib.util
import sys

dest, store_path = sys.argv[1], sys.argv[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = load("cron_plus_store", store_path)
sys.path.insert(0, dest)
try:
    jobs = load("jobs", dest + "/jobs.py")
    lock, jobs_file = jobs.JOBS_LOCK_FILE, jobs.JOBS_FILE
except Exception as exc:  # any failure means the lock cannot be confirmed
    print(f"cannot import the pinned jobs.py to read its lock: {type(exc).__name__}: {exc}",
          file=sys.stderr)
    sys.exit(1)
expected = jobs_file.parent / store.LOCK_NAME
if lock != expected:
    print(f"the pinned jobs.py locks {lock}, not {expected}", file=sys.stderr)
    sys.exit(1)
PY
if [ "$lock_rc" -ne 0 ]; then
    echo "ERROR: cron-plus pin ${SHA:0:12} does not lock the job store through jobs.json.lock" >&2
    exit 1
fi
echo "  verified the pinned policy modules match the engine's tested copies"

if [ -d "$DEST/.git" ]; then
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
    mv "$CANDIDATE" "$DEST"
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
