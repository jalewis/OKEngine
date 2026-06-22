#!/usr/bin/env bash
# Deploy cron-plus jobs from the repo's source-of-truth
# (config/cron-plus-jobs.json) into the running pack gateway container at
# /opt/data/cron-plus/jobs.json (the mounted pack .hermes-data) — where cron-plus
# actually reads them. NOT host ~/.hermes (#18).
#
# Usage:
#   CRON_PACK_DIR=/path/to/pack bash scripts/deploy-cron-plus-jobs.sh
#
# Snapshots the existing jobs.json in-container before overwriting.
# As of cron-plus v0.1.2 the scheduler self-heals null next_run_at on
# enabled jobs as part of claim_due_jobs() — no external seed step needed.
# A sanitized source-of-truth (runtime fields stripped) deploys cleanly:
# the next scheduler tick (~60s) computes next_run_at from each job's
# schedule under the same exclusive lock as the claim itself.
#
# Verify with: bash scripts/cron-plus.sh list (jobs should show real
# NEXT RUN times within ~60s of deploy).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_ROOT/config/cron-plus-jobs.json"
PACK_DIR="${CRON_PACK_DIR:-/path/to/pack}"
# cron-plus runs INSIDE the gateway and reads /opt/data/cron-plus/jobs.json (the
# mounted pack .hermes-data) — NOT host ~/.hermes. Deploy into the container as
# the `hermes` user so ownership is correct (#18).
DEST_IN="/opt/data/cron-plus/jobs.json"

# Two-repo split (slice 2): cron-plus-jobs.json is GENERATED from the engine half
# (config/engine-crons.json) + the domain pack ($PACK_DIR/crons/). If both sources
# are present, regenerate first so the deploy reflects them. If the pack is absent
# (e.g. disaster recovery from the engine repo alone), fall back to the committed
# cron-plus-jobs.json as-is.
if [ -f "$REPO_ROOT/config/engine-crons.json" ] && [ -d "$PACK_DIR/crons" ]; then
    CRON_PACK_DIR="$PACK_DIR" python3 "$REPO_ROOT/scripts/cron_pack_split.py" regen
else
    echo "  (pack not found at $PACK_DIR — deploying committed cron-plus-jobs.json as-is)"
fi

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found.  Are you running from the repo root?" >&2
    exit 1
fi

# Target the running pack gateway container (compose service == gateway).
CONTAINER="$(docker ps --filter 'label=com.docker.compose.service=gateway' \
                       --filter 'status=running' --format '{{.Names}}' | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up?)." >&2
    exit 1
fi
TS="$(date +%Y%m%d-%H%M%S)"

# Create the runtime dir, snapshot any existing jobs.json, then stream the new one
# in as `hermes` (so the cron-plus subprocess, also hermes, can read it).
docker exec -u hermes "$CONTAINER" mkdir -p /opt/data/cron-plus
docker exec -u hermes "$CONTAINER" sh -c \
    "[ -f '$DEST_IN' ] && cp -p '$DEST_IN' '$DEST_IN.bak.$TS' && echo '  snapshot: $DEST_IN.bak.$TS' || true"
docker exec -i -u hermes "$CONTAINER" sh -c "cat > '$DEST_IN' && chmod 600 '$DEST_IN'" < "$SRC"
echo "  deployed: $CONTAINER:$DEST_IN"

JOB_COUNT=$(python3 -c "import json; print(len(json.load(open('$SRC'))['jobs']))")
echo "  jobs: $JOB_COUNT"

echo ""
echo "Done. cron-plus self-heals null next_run_at on the next tick (~60s)."
echo "Verify with: bash $REPO_ROOT/scripts/cron-plus.sh list"
