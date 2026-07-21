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
# Resolve from env -> pack .env pin -> tree owner (okengine#185): writing jobs.json as the wrong
# uid stalls the cron fleet, so never silently fall through to 10000 when the pack pins otherwise.
# shellcheck source=lib/hermes_uid.sh
. "$REPO_ROOT/scripts/lib/hermes_uid.sh"
HERMES_UID="$(resolve_hermes_uid "$PACK_DIR")"
# cron-plus runs INSIDE the gateway and reads /opt/data/cron-plus/jobs.json (the
# mounted pack .hermes-data) — NOT host ~/.hermes. Deploy into the container as
# the `hermes` user so ownership is correct (#18).
DEST_IN="/opt/data/cron-plus/jobs.json"

# Two-repo split (slice 2): cron-plus-jobs.json is a per-pack GENERATED artifact (gitignored, never
# committed) produced from the engine half (config/engine-crons.json) + the domain pack
# ($PACK_DIR/crons/, optional — regen tolerates its absence for an engine-only pack). ALWAYS
# regenerate for THIS pack so the deploy never ships a stale leftover from the last regen — which,
# on a multi-pack host, could be a DIFFERENT pack's job set (invariant-audit #12). The old guard
# skipped regen when a pack lacked crons/ and deployed whatever was lying around.
if [ -f "$REPO_ROOT/config/engine-crons.json" ]; then
    CRON_PACK_DIR="$PACK_DIR" python3 "$REPO_ROOT/scripts/cron_pack_split.py" regen
else
    # No engine-crons.json = a broken/partial engine checkout, NOT a valid DR source. The artifact is
    # gitignored, so any $SRC present is a stale leftover — refuse rather than deploy it blind.
    echo "ERROR: $REPO_ROOT/config/engine-crons.json missing — cannot regenerate the cron fleet." >&2
    echo "       (cron-plus-jobs.json is a generated artifact, never committed; a leftover copy is" >&2
    echo "        a stale/wrong-pack snapshot and will NOT be deployed.)  Restore the engine checkout." >&2
    exit 1
fi

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found.  Are you running from the repo root?" >&2
    exit 1
fi

# The regenerated artifact must belong to THIS pack: every domain lane carries a `pack:` provenance
# marker (cron_pack_split), and none may name another pack — a mismatch means a wrong-pack artifact
# slipped through (invariant-audit #12). Cheap earliest-gate check before touching the live store.
TARGET_PACK="$(grep -oE '^name:[[:space:]]*[A-Za-z0-9._-]+' "$PACK_DIR/pack.yaml" 2>/dev/null | head -1 | awk '{print $2}' || true)"
if [ -n "$TARGET_PACK" ]; then
    FOREIGN="$(python3 -c '
import json, sys
target = sys.argv[2]
print("\n".join(sorted({j.get("pack") for j in json.load(open(sys.argv[1]))["jobs"]
                        if j.get("pack") and j.get("pack") != target})))
' "$SRC" "$TARGET_PACK")"
    if [ -n "$FOREIGN" ]; then
        echo "ERROR: cron-plus-jobs.json carries domain lane(s) for OTHER pack(s):" >&2
        echo "$FOREIGN" | sed 's/^/  FOREIGN-PACK  /' >&2
        echo "  target pack is '$TARGET_PACK' — a stale/wrong-pack artifact. Regenerate for this pack." >&2
        exit 1
    fi
fi

# Expand any @jitter:* sentinels into concrete schedules for the DEPLOY copy. Engine crons
# ship sentinels for per-install jitter (pack crons were expanded at `framework pull`); cron-plus
# can't parse a raw sentinel, so an unexpanded one errors every tick and never runs (okengine#107).
# Expand a temp copy, NOT $SRC, so the generated cron-plus-jobs.json stays round-trippable.
# Also resolve any `@<profile>` model references against the pack's model-profiles.yaml
# (okengine#151) — same deploy-only transform as @jitter (on the temp copy, never $SRC), so a
# lane can switch ollama host / ctx. Fail-loud on an undefined profile (a broken fleet must not
# deploy).
DEPLOY_JOBS="$(mktemp)"
trap 'rm -f "$DEPLOY_JOBS"' EXIT
# Morning-brief hour: the deployment's single "when do my daily briefs run" knob
# (OKENGINE_BRIEF_HOUR in .env, gateway-local TZ). Brief lanes ship `@morning[:MM]`
# sentinels; they expand to this hour at deploy so every deployment picks its own
# morning without forking any schedule (okengine#177). Default 7.
BRIEF_HOUR="$(_okengine_env_file_val "$PACK_DIR" OKENGINE_BRIEF_HOUR || true)"
BRIEF_HOUR="${BRIEF_HOUR:-7}"
case "$BRIEF_HOUR" in
    *[!0-9]*|"") echo "ERROR: OKENGINE_BRIEF_HOUR must be an integer from 0 to 23 (got '$BRIEF_HOUR')" >&2; exit 1 ;;
esac
if [ "$BRIEF_HOUR" -gt 23 ]; then
    echo "ERROR: OKENGINE_BRIEF_HOUR must be from 0 to 23 (got '$BRIEF_HOUR')" >&2
    exit 1
fi
PYTHONPATH="$REPO_ROOT/scripts" python3 - "$SRC" "$DEPLOY_JOBS" "$PACK_DIR" "$BRIEF_HOUR" <<'PY'
import sys, json, hashlib, random, cron_jitter, model_profiles
src, out, pack_dir, brief_hour = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
d = json.load(open(src, encoding="utf-8"))
jobs = d.get("jobs", [])
bn = cron_jitter.expand_brief_jobs(jobs, brief_hour)
# ENGINE crons ship @jitter sentinels and are re-expanded on EVERY deploy; with an unseeded
# random.Random() each redeploy re-rolls every jittered lane's minute, and because the deploy strips
# runtime fields (next_run_at recomputed from the new expr) a lane can silently skip or double-run
# that cycle (invariant-audit #47). Seed the RNG from the pack identity so re-expansion is STABLE
# across redeploys of the same install (idempotent) yet still differs between installs (the per-
# install spread @jitter exists to provide). Job order is fixed by the source file, so each lane
# keeps the same minute deploy-to-deploy.
_seed = int(hashlib.sha256(pack_dir.encode("utf-8")).hexdigest(), 16) % (2**32)
n = cron_jitter.expand_jobs(jobs, random.Random(_seed))
try:
    profiles = model_profiles.load_profiles(pack_dir)
except (ValueError, OSError) as e:
    print(f"ERROR: model-profiles.yaml: {e}", file=sys.stderr); sys.exit(1)
perr = model_profiles.validate_profiles(profiles)
if perr:
    print("ERROR: model-profiles.yaml:\n  " + "\n  ".join(perr), file=sys.stderr); sys.exit(1)
# Per-lane model overrides for non-extension lanes (engine/engine-template/domain), applied
# BEFORE @-profile expansion so an `@profile` value here resolves like any other ref.
try:
    lane_models = model_profiles.load_lane_models(pack_dir)
except (ValueError, OSError) as e:
    print(f"ERROR: cron-models.json: {e}", file=sys.stderr); sys.exit(1)
ln, lerr = model_profiles.apply_lane_models(jobs, lane_models)
if lerr:
    print("ERROR: cron-models.json (not deploying):\n  " + "\n  ".join(lerr), file=sys.stderr)
    sys.exit(1)
pn, jerr = model_profiles.expand_jobs(jobs, profiles)
if jerr:
    print("ERROR: model-profile references (not deploying):\n  " + "\n  ".join(jerr), file=sys.stderr)
    sys.exit(1)
json.dump(d, open(out, "w", encoding="utf-8"), indent=2)
print(f"  expanded {bn} @morning brief(s) @{brief_hour:02d}:MM + {n} @jitter sentinel(s) "
      f"+ {ln} lane override(s) + {pn} model-profile ref(s) for deploy")
PY

# Target THIS pack's gateway via its compose project — NOT the first gateway on the host,
# which is the wrong pack on a multi-pack host (okengine#108).
CONTAINER="$(docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway 2>/dev/null | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up?)." >&2
    exit 1
fi
TS="$(date +%Y%m%d-%H%M%S)"

# PREFLIGHT: the cron-plus plugin must exist in the gateway, or these jobs deploy into a silently
# DEAD scheduler (jobs.json lands fine, nothing ever fires — a live deployment shipped exactly this
# way). ensure-runtime.sh installs it (pinned) before compose; fail loud here as the backstop.
if ! docker exec "$CONTAINER" sh -c "test -f /opt/data/plugins/cron-plus/runner.py" 2>/dev/null; then
    echo "ERROR: cron-plus plugin missing in the gateway (/opt/data/plugins/cron-plus/runner.py)." >&2
    echo "       These jobs would deploy into a DEAD scheduler — nothing would ever fire." >&2
    echo "       Fix: bash \$ENGINE_DIR/scripts/ensure-runtime.sh (installs the pinned plugin)," >&2
    echo "       then docker compose restart gateway, then re-run this script." >&2
    exit 1
fi

# PRE-WRITE guard (okengine#162, invariant-audit #50): every deployed lane's `script` must be STAGED
# in the gateway, or the lane fails SILENTLY (cron-plus runs the agent with no wake-gate and writes
# nothing — how an enabled extension whose scripts were never deploy-cron-scripts'd goes dark).
# Validate the DEPLOY copy against /opt/data/scripts BEFORE overwriting the live store, so a broken
# set is rejected with the live jobs.json untouched (the old check ran AFTER the write and exited
# without restoring the snapshot — the invalid store was already live and being scheduled).
MISSING_SCRIPTS="$(docker exec -i -u "$HERMES_UID" "$CONTAINER" python3 -c '
import json, os, sys
for j in json.load(sys.stdin)["jobs"]:
    s = (j.get("script") or "").strip()
    if not s:
        continue
    p = s if s.startswith("/") else "/opt/data/scripts/" + s
    if not os.path.isfile(p):
        print((j.get("name") or "?") + " -> " + p)
' < "$DEPLOY_JOBS")"
if [ -n "$MISSING_SCRIPTS" ]; then
    echo "ERROR: lane(s) reference scripts NOT staged in the gateway (live jobs.json UNCHANGED):" >&2
    echo "$MISSING_SCRIPTS" | sed 's/^/  MISSING  /' >&2
    echo "  fix: CRON_PACK_DIR='$PACK_DIR' bash '$REPO_ROOT/scripts/deploy-cron-scripts.sh', then re-run." >&2
    exit 1
fi

# Create the runtime dir, snapshot any existing jobs.json, then stream the new one
# in as `hermes` (so the cron-plus subprocess, also hermes, can read it).
docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/cron-plus
docker exec -u "$HERMES_UID" "$CONTAINER" sh -c \
    "[ -f '$DEST_IN' ] && cp -p '$DEST_IN' '$DEST_IN.bak.$TS' && echo '  snapshot: $DEST_IN.bak.$TS' || true"
docker exec -i -u "$HERMES_UID" "$CONTAINER" sh -c "cat > '$DEST_IN' && chmod 600 '$DEST_IN'" < "$DEPLOY_JOBS"
echo "  deployed: $CONTAINER:$DEST_IN"

JOB_COUNT=$(python3 -c "import json; print(len(json.load(open('$SRC'))['jobs']))")
echo "  jobs: $JOB_COUNT (all lane scripts staged — validated pre-write)"

echo ""
echo "Done. cron-plus self-heals null next_run_at on the next tick (~60s)."
echo "Verify with: bash $REPO_ROOT/scripts/cron-plus.sh list"
