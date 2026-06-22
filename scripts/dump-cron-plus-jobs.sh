#!/usr/bin/env bash
# Re-sync the repo's cron-plus job source-of-truth from the live state.
# Reads /opt/data/cron-plus/jobs.json from inside the gateway container
# (since ~/.hermes/cron-plus/ is owned by container uid 10000 and not
# host-readable for uid 1003 — direct host reads fail with Permission
# denied), strips runtime fields, sorts jobs by name for diff-friendliness,
# and writes config/cron-plus-jobs.json on the host.
#
# Usage:
#   bash scripts/dump-cron-plus-jobs.sh
#
# Run after editing prompts/schedules/models live (via direct JSON edit
# or `bash scripts/cron-plus.sh ...`) to capture changes in git. The
# repo file is the canonical source; the live file is authoritative for
# runtime state (next_run_at, last_run_*, etc.).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_IN_CONTAINER="/opt/data/cron-plus/jobs.json"
OUT="$REPO_ROOT/config/cron-plus-jobs.json"
SERVICE="${HERMES_DOCKER_SERVICE:-gateway}"

cd "$REPO_ROOT"

# Verify docker compose can reach the gateway. `ps -q` returns the container
# id if it's running, empty otherwise — non-zero exit if compose itself is
# unreachable.
if ! container_id="$(docker compose ps -q "$SERVICE" 2>/dev/null)"; then
    echo "ERROR: 'docker compose ps' failed — is Docker running? Are you in the project dir?" >&2
    exit 1
fi
if [ -z "$container_id" ]; then
    echo "ERROR: service '$SERVICE' is not running. Bring it up with 'docker compose up -d'." >&2
    exit 1
fi

# Pull the live JSON out of the container as hermes (uid 10000) into a
# temp file. Using a file (not a pipe) decouples the docker exec stream
# from Python's stdin — `python3 -` reads its program from stdin, so we
# can't ALSO feed it the JSON via stdin in the same pipeline.
TMP_JSON="$(mktemp -t cron-plus-live.XXXXXX.json)"
trap 'rm -f "$TMP_JSON"' EXIT

if ! docker compose exec -T --user hermes "$SERVICE" cat "$LIVE_IN_CONTAINER" > "$TMP_JSON"; then
    echo "ERROR: failed to read $LIVE_IN_CONTAINER inside $SERVICE container." >&2
    exit 1
fi
if [ ! -s "$TMP_JSON" ]; then
    echo "ERROR: container returned empty JSON for $LIVE_IN_CONTAINER." >&2
    exit 1
fi

# Snapshot the existing repo file before overwriting.
if [ -f "$OUT" ]; then
    TS="$(date +%Y%m%d-%H%M%S)"
    cp -p "$OUT" "$OUT.bak.$TS"
    echo "  snapshot: $OUT.bak.$TS"
fi

# Slice 2 (two-repo split): capture the live state into the SOURCES — the engine
# half (config/engine-crons.json) + the domain pack (<pack>/crons/) — then
# regenerate the deployed artifact config/cron-plus-jobs.json from them.
# cron_pack_split sanitizes (strips runtime fields) and keys the split by
# config/cron-tiers.yaml. Override the pack location with CRON_PACK_DIR.
CRON_PACK_DIR="${CRON_PACK_DIR:-/path/to/pack}" \
    python3 "$REPO_ROOT/scripts/cron_pack_split.py" dump --live "$TMP_JSON"

echo ""
echo "Done. Sources updated: config/engine-crons.json + \$CRON_PACK_DIR/crons/."
echo "Review with 'git diff $OUT config/engine-crons.json' (and the pack repo)."
