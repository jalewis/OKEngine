#!/usr/bin/env bash
# One-command bring-up for an OKEngine pack — the single entry point so the
# seed-before-compose step can never be skipped (a git-cloned library pack has no
# .hermes-data; Docker would auto-create it as root at compose-up otherwise).
#
# Runs, in order, from the pack dir (the vault):
#   1. framework validate   (fail fast on a broken pack)
#   2. ensure-runtime.sh     (seed host-owned .hermes-data/config.yaml + qmd/)
#   3. build-engine-image.sh (only if the gateway image is missing)
#   4. docker compose up -d  (gateway + reader + mcp)
#   5. deploy-cron-scripts.sh + deploy-cron-plus-jobs.sh
#   6. post_deploy_verify.sh (live end-to-end checks: reader/MCP/write-path/crons/index)
#
# Usage (from the pack dir):
#   bash $ENGINE_DIR/scripts/deploy.sh
#   bash $ENGINE_DIR/scripts/deploy.sh /path/to/pack
# Flags:
#   --rebuild        force-rebuild the gateway image (default: build only if absent)
#   --skip-build     never build the image (use an existing hermes-agent:latest)
#   --skip-validate  skip the pre-deploy validate gate
#   --no-crons       bring up containers only; don't deploy crons
#   --fix-perms      make the pack tree writable by HERMES_UID (local convenience;
#                    otherwise a non-writable tree fails before compose with remediation)
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACK="$PWD"
REBUILD=0; SKIP_BUILD=0; SKIP_VALIDATE=0; NO_CRONS=0; FIX_PERMS=0
for a in "$@"; do
    case "$a" in
        --rebuild)       REBUILD=1 ;;
        --skip-build)    SKIP_BUILD=1 ;;
        --skip-validate) SKIP_VALIDATE=1 ;;
        --no-crons)      NO_CRONS=1 ;;
        --fix-perms)     FIX_PERMS=1 ;;
        -*)              echo "unknown flag: $a" >&2; exit 2 ;;
        *)               PACK="$a" ;;
    esac
done
PACK="$(cd "$PACK" && pwd)"

if [ ! -f "$PACK/docker-compose.yml" ]; then
    echo "ERROR: $PACK has no docker-compose.yml — run from a pack dir (or pass one)." >&2
    exit 1
fi

# Default to the invoking user's uid/gid so a clone-as-yourself pack tree is writable out of
# the box (okengine#102): you own the vault, and the gateway remaps to it. Pin a FIXED uid
# (+ chown the tree to it) instead for a vault you'll move between hosts or share across
# operators — see docs/deploy-a-new-domain.md §2.
export HERMES_UID="${HERMES_UID:-$(id -u)}" HERMES_GID="${HERMES_GID:-$(id -g)}"
PYTHON="${PYTHON:-python3}"

echo "==> OKEngine deploy: $PACK"
echo "    engine $ENGINE_DIR · uid:gid $HERMES_UID:$HERMES_GID"

# 1. validate — don't deploy a broken pack.
if [ "$SKIP_VALIDATE" = 0 ]; then
    echo "==> [1/6] validate"
    if ! "$PYTHON" "$ENGINE_DIR/scripts/framework.py" validate "$PACK" --quiet; then
        echo "ERROR: validation failed — fix the FAILs above, or re-run with --skip-validate." >&2
        exit 1
    fi
fi

# 2. seed the runtime dir + ensure it's writable by HERMES_UID BEFORE compose binds it,
#    and install the cron-plus scheduler plugin into the runtime (the seeded config
#    enables it, so it must be present before the gateway starts).
echo "==> [2/6] seed runtime (.hermes-data) + cron-plus plugin"
FIX_PERMS="$FIX_PERMS" bash "$ENGINE_DIR/scripts/ensure-runtime.sh" "$PACK"
bash "$ENGINE_DIR/scripts/install-cron-plus.sh" "$PACK"

# 3. gateway image — build if missing OR stale (label != current checkout), or forced.
cur_sha="$(git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
img_sha="$(docker image inspect -f '{{ index .Config.Labels "org.okengine.git_sha" }}' hermes-agent:latest 2>/dev/null || echo)"
if [ "$SKIP_BUILD" = 0 ]; then
    if [ "$REBUILD" = 1 ]; then
        echo "==> [3/6] build gateway image (--rebuild)"
        bash "$ENGINE_DIR/scripts/build-engine-image.sh"
    elif ! docker image inspect hermes-agent:latest >/dev/null 2>&1; then
        echo "==> [3/6] build gateway image (none present)"
        bash "$ENGINE_DIR/scripts/build-engine-image.sh"
    elif [ "$img_sha" != "$cur_sha" ]; then
        echo "==> [3/6] gateway image is STALE (image sha='${img_sha:-none/unlabeled}' != engine sha '$cur_sha') — rebuilding"
        bash "$ENGINE_DIR/scripts/build-engine-image.sh"
    else
        echo "==> [3/6] gateway image up to date (sha $cur_sha) — skipping build"
    fi
else
    # Even when skipping, surface the existing image's provenance so a stale one is visible.
    if [ "$img_sha" != "$cur_sha" ]; then
        echo "==> [3/6] build skipped (--skip-build) — WARNING: image sha '${img_sha:-none/unlabeled}' != engine sha '$cur_sha' (image may be stale; --rebuild to refresh)"
    else
        echo "==> [3/6] build skipped (--skip-build); image sha $cur_sha matches engine"
    fi
fi

# 4. bring up the containers (from the pack dir, where docker-compose.yml lives).
echo "==> [4/6] docker compose up -d"
( cd "$PACK" && ENGINE_DIR="$ENGINE_DIR" docker compose up -d )

# 5. deploy cron scripts + jobs.
if [ "$NO_CRONS" = 0 ]; then
    echo "==> [5/6] deploy crons"
    CRON_PACK_DIR="$PACK" bash "$ENGINE_DIR/scripts/deploy-cron-scripts.sh"
    CRON_PACK_DIR="$PACK" bash "$ENGINE_DIR/scripts/deploy-cron-plus-jobs.sh"
else
    echo "==> [5/6] crons skipped (--no-crons)"
fi

# 6. post-deploy verification — actually exercise the live stack (#67): reader/MCP reachability +
#    auth, the enforced write path, cron-plus registration, and search-index readiness, with
#    operator remediation for anything down. Non-fatal: the stack is already up, so a reported
#    issue is diagnostic, not a reason to abort (and the gateway may still be booting).
echo "==> [6/6] verify deployment"
sleep 5   # give the reader/MCP a moment to bind before probing
if ( cd "$PACK" && bash "$ENGINE_DIR/scripts/post_deploy_verify.sh" ); then
    echo "==> done — deployment verified healthy."
else
    echo "==> done — bring-up complete, but post-deploy checks reported issues (see remediation above)."
    echo "    re-verify any time:  ( cd $PACK && bash $ENGINE_DIR/scripts/post_deploy_verify.sh )"
fi
echo "    LLM cron output + delivery appear once a model key (and a delivery channel, if used) are set in .env."
