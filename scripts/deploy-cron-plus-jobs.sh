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
# Write into the container as the SAME uid the gateway runs as (compose
# `user: ${HERMES_UID:-10000}`), not the image's `hermes` name (10000): a pack
# that overrides HERMES_UID owns /opt/data with that uid, and `-u hermes` would
# mismatch -> permission-denied writing jobs.json (#18 follow-up).
HERMES_UID="${HERMES_UID:-10000}"
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

# Expand any @jitter:* sentinels into concrete schedules for the DEPLOY copy. Engine crons
# ship sentinels for per-install jitter (pack crons were expanded at `framework pull`); cron-plus
# can't parse a raw sentinel, so an unexpanded one errors every tick and never runs (okengine#107).
# Expand a temp copy, NOT $SRC, so the generated cron-plus-jobs.json stays round-trippable.
DEPLOY_JOBS="$(mktemp)"
trap 'rm -f "$DEPLOY_JOBS"' EXIT
PYTHONPATH="$REPO_ROOT/scripts" python3 - "$SRC" "$DEPLOY_JOBS" <<'PY'
import sys, json, cron_jitter
src, out = sys.argv[1], sys.argv[2]
d = json.load(open(src, encoding="utf-8"))
n = cron_jitter.expand_jobs(d.get("jobs", []))
json.dump(d, open(out, "w", encoding="utf-8"), indent=2)
print(f"  expanded {n} @jitter sentinel(s) for deploy")
PY

# Target THIS pack's gateway via its compose project — NOT the first gateway on the host,
# which is the wrong pack on a multi-pack host (okengine#108).
CONTAINER="$(docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway 2>/dev/null | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up?)." >&2
    exit 1
fi
TS="$(date +%Y%m%d-%H%M%S)"

# Create the runtime dir, snapshot any existing jobs.json, then stream the new one
# in as `hermes` (so the cron-plus subprocess, also hermes, can read it).
docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/cron-plus
docker exec -u "$HERMES_UID" "$CONTAINER" sh -c \
    "[ -f '$DEST_IN' ] && cp -p '$DEST_IN' '$DEST_IN.bak.$TS' && echo '  snapshot: $DEST_IN.bak.$TS' || true"
docker exec -i -u "$HERMES_UID" "$CONTAINER" sh -c "cat > '$DEST_IN' && chmod 600 '$DEST_IN'" < "$DEPLOY_JOBS"
echo "  deployed: $CONTAINER:$DEST_IN"

JOB_COUNT=$(python3 -c "import json; print(len(json.load(open('$SRC'))['jobs']))")
echo "  jobs: $JOB_COUNT"

echo ""
echo "Done. cron-plus self-heals null next_run_at on the next tick (~60s)."
echo "Verify with: bash $REPO_ROOT/scripts/cron-plus.sh list"
