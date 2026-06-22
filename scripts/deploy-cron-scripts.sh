#!/usr/bin/env bash
# Deploy cron scripts from this repo into the gateway's /opt/data/scripts/
# (== host ~/.hermes/scripts/ via the docker-compose bind mount).
#
# Hermes' cron module only accepts --script paths under /opt/data/scripts/,
# so this is required to make the repo's source-of-truth versions runnable.
#
# NOTE: ~/.hermes is owned by the container user (uid 10000, mode 700) and is
# NOT host-writable. A host-side `cp` fails with EACCES. We therefore stream
# files THROUGH the running gateway container as the `hermes` user, so they
# land with correct ownership and the cron subprocess (also `hermes`) can read
# them. The repo (scripts/cron/) is the backup — no per-file .bak snapshots.
#
# Usage:  bash scripts/deploy-cron-scripts.sh
#         (run after editing scripts/cron/*.py and committing)
#
# Idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$REPO_ROOT/scripts/cron"
# Two-repo split (slice 3): domain cron scripts + domain data live in the pack.
# The deploy assembles /opt/data/{scripts,config}/ from BOTH the engine repo and
# the pack — co-location means imports resolve regardless of source repo.
PACK_DIR="${CRON_PACK_DIR:-/path/to/pack}"
PACK_SCRIPTS="$PACK_DIR/crons/scripts"
PACK_DATA="$PACK_DIR/data"

if [ ! -d "$SRC_DIR" ]; then
    echo "ERROR: $SRC_DIR not found.  Are you running from the repo root?" >&2
    exit 1
fi

# Locate the running gateway container (compose service == gateway).
CONTAINER="$(docker ps --filter 'label=com.docker.compose.service=gateway' \
                       --filter 'status=running' --format '{{.Names}}' | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up?)." >&2
    exit 1
fi
echo "  gateway container: $CONTAINER"

# A freshly-seeded pack runtime has no /opt/data/scripts (or /config) yet — create
# them before any tar extract, else the first deploy fails "Cannot open" (#17).
docker exec -u hermes "$CONTAINER" mkdir -p /opt/data/scripts /opt/data/config

# --- engine/pack version pin check (warn-only; slice 4a) ---
# The pack pins an engine release in $PACK_DIR/engine.version; warn if it doesn't
# match this engine checkout's latest v* release tag. Non-fatal by design.
if [ -f "$PACK_DIR/engine.version" ]; then
    PINNED="$(sed -n 's/^version:[[:space:]]*//p' "$PACK_DIR/engine.version" | head -1)"
    ENGINE_TAG="$(git -C "$REPO_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo '')"
    if [ -n "$PINNED" ] && [ -n "$ENGINE_TAG" ] && [ "$PINNED" != "$ENGINE_TAG" ]; then
        echo "  ⚠ engine/pack version mismatch: pack pins '$PINNED', engine is '$ENGINE_TAG'" >&2
    elif [ -n "$ENGINE_TAG" ]; then
        echo "  engine: $ENGINE_TAG (pack pin: ${PINNED:-none})"
    fi
fi

# --- cron scripts: engine scripts/cron/*.py + pack crons/scripts/*.py -> /opt/data/scripts/ ---
ecount="$(cd "$SRC_DIR" && ls -1 ./*.py 2>/dev/null | wc -l)"
if [ "$ecount" -eq 0 ]; then
    echo "  (no engine scripts found in $SRC_DIR)"
else
    ( cd "$SRC_DIR" && tar -cf - ./*.py ) \
        | docker exec -i -u hermes "$CONTAINER" tar -xf - -C /opt/data/scripts/
    echo "  $ecount engine cron script(s) deployed to $CONTAINER:/opt/data/scripts/"
fi
if [ -d "$PACK_SCRIPTS" ]; then
    pcount="$(cd "$PACK_SCRIPTS" && ls -1 ./*.py 2>/dev/null | wc -l)"
    if [ "$pcount" -gt 0 ]; then
        ( cd "$PACK_SCRIPTS" && tar -cf - ./*.py ) \
            | docker exec -i -u hermes "$CONTAINER" tar -xf - -C /opt/data/scripts/
        echo "  $pcount pack (domain) cron script(s) deployed from $PACK_SCRIPTS"
    fi
else
    echo "  (pack scripts not found at $PACK_SCRIPTS — engine-only deploy)"
fi

# --- domain data -> /opt/data/config/ ---
# Domain data tables consumed at runtime (cron-plus mounts only /opt/data/, so
# these must sit alongside the scripts). Sourced from the PACK now:
#   - sec-cyber-pubcos.yaml      — used by ingest_sec_filings.py (domain)
#   - curated-entity-fields.json — used by apply_curated_entity_fields.py (engine-template)
# (publishers.canonical.json is NOT deployed here: the publisher-canonical-drain cron
#  maintains it IN-PLACE in the vault at config/publishers.canonical.json — that's the
#  live source of truth, also read by the scripts/normalize_publishers.py dev tool.)
if [ -d "$PACK_DATA" ]; then
    cfgs=()
    for cfg in sec-cyber-pubcos.yaml curated-entity-fields.json; do
        [ -f "$PACK_DATA/$cfg" ] && cfgs+=("$cfg")
    done
    if [ "${#cfgs[@]}" -gt 0 ]; then
        docker exec -u hermes "$CONTAINER" mkdir -p /opt/data/config
        ( cd "$PACK_DATA" && tar -cf - "${cfgs[@]}" ) \
            | docker exec -i -u hermes "$CONTAINER" tar -xf - -C /opt/data/config/
        echo "  ${#cfgs[@]} pack data file(s) deployed to $CONTAINER:/opt/data/config/"
    fi
else
    echo "  (pack data not found at $PACK_DATA — skipping domain data deploy)"
fi

# --- pack feed lists (*.opml) -> /opt/data/config/ ---
# Read by the generic feed_fetch.py at runtime (feeds = pure config).
PACK_FEEDS="$PACK_DIR/feeds"
if [ -d "$PACK_FEEDS" ]; then
    ocount="$(cd "$PACK_FEEDS" && ls -1 ./*.opml 2>/dev/null | wc -l)"
    if [ "$ocount" -gt 0 ]; then
        docker exec -u hermes "$CONTAINER" mkdir -p /opt/data/config
        ( cd "$PACK_FEEDS" && tar -cf - ./*.opml ) \
            | docker exec -i -u hermes "$CONTAINER" tar -xf - -C /opt/data/config/
        echo "  $ocount pack feed list(s) deployed to $CONTAINER:/opt/data/config/"
    fi
fi

echo "  done."
