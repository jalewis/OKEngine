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
# Exec into the container as the SAME uid the gateway runs as (compose
# `user: ${HERMES_UID:-10000}`), not the image's `hermes` name (10000): a pack
# that overrides HERMES_UID owns /opt/data with that uid, so `-u hermes` would
# mismatch and the tar extraction / mkdir hit permission-denied. Resolve from
# env -> pack .env pin -> tree owner (okengine#185) — never silently 10000.
# shellcheck source=lib/hermes_uid.sh
. "$REPO_ROOT/scripts/lib/hermes_uid.sh"
# shellcheck source=lib/pack_data.sh
. "$REPO_ROOT/scripts/lib/pack_data.sh"
HERMES_UID="$(resolve_hermes_uid "$PACK_DIR")"
PACK_SCRIPTS="$PACK_DIR/crons/scripts"
PACK_DATA="$PACK_DIR/data"
PACK_CONNECTORS="$PACK_DIR/connectors"
RUNTIME_COMPOSE_HELPERS=(extension_compose.py extension_discovery.py extension_manifest.py)

if [ ! -d "$SRC_DIR" ]; then
    echo "ERROR: $SRC_DIR not found.  Are you running from the repo root?" >&2
    exit 1
fi

# Locate THIS pack's gateway via its compose project — NOT the first gateway on the host,
# which is the wrong pack on a multi-pack host (okengine#108).
CONTAINER="$(docker compose -f "$PACK_DIR/docker-compose.yml" ps -q gateway 2>/dev/null | head -1)"
if [ -z "$CONTAINER" ]; then
    echo "ERROR: no running gateway container found (is the stack up?)." >&2
    exit 1
fi
echo "  gateway container: $CONTAINER"

# A freshly-seeded pack runtime has no /opt/data/{scripts,config,metrics} yet — create them AS THE
# CRON UID before any write. Else the first deploy fails "Cannot open" (#17), and — if the metrics
# dir/db is first created by a root-context run — usage-rollup later fails with "attempt to write a
# readonly database" because the 1003 cron can't write a root-owned usage.db.
docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/scripts /opt/data/config /opt/data/metrics

# --- engine/pack version pin check (warn-only; slice 4a) ---
# The pack pins an engine release in $PACK_DIR/engine.version; warn only if it's a
# DIFFERENT major.minor series than this engine checkout. A patch-newer engine (e.g.
# v0.3.2 vs a v0.3.0 pin) is compatible and must not warn (okengine#104). Non-fatal.
if [ -f "$PACK_DIR/engine.version" ]; then
    PINNED="$(sed -n 's/^version:[[:space:]]*//p' "$PACK_DIR/engine.version" | head -1)"
    ENGINE_TAG="$(git -C "$REPO_ROOT" describe --tags --match 'v*' --abbrev=0 2>/dev/null || echo '')"
    _series() { echo "${1#v}" | cut -d. -f1-2; }   # vX.Y.Z -> X.Y
    if [ -n "$PINNED" ] && [ -n "$ENGINE_TAG" ] && [ "$(_series "$PINNED")" != "$(_series "$ENGINE_TAG")" ]; then
        echo "  ⚠ engine/pack series mismatch: pack pins '$PINNED', engine is '$ENGINE_TAG'" >&2
    elif [ -n "$ENGINE_TAG" ]; then
        echo "  engine: $ENGINE_TAG (pack pin: ${PINNED:-none})"
    fi
fi

# --- cron scripts: engine scripts/cron/*.py + pack crons/scripts/*.py -> /opt/data/scripts/ ---
ecount="$(find "$SRC_DIR" -maxdepth 1 -name '*.py' | wc -l)"   # find: rc 0 on no match — a bare ls glob here dies silently under set -euo pipefail (4th instance of this class)
if [ "$ecount" -eq 0 ]; then
    echo "  (no engine scripts found in $SRC_DIR)"
else
    ( cd "$SRC_DIR" && tar -cf - ./*.py ) \
        | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/scripts/
    echo "  $ecount engine cron script(s) deployed to $CONTAINER:/opt/data/scripts/"
fi

# --- runtime schema-composition helpers + engine extension inputs (#277) ---
# deployment_validate runs inside the gateway. To reproduce the deploy-side
# composition there, it needs both the composer modules and the engine-tier
# extension manifests/schema fragments. The staged layout deliberately mirrors
# the repository layout expected by extension_discovery:
#   /opt/data/scripts/extension_discovery.py -> /opt/data/extensions/<id>/
for helper in "${RUNTIME_COMPOSE_HELPERS[@]}"; do
    if [ ! -f "$REPO_ROOT/scripts/$helper" ]; then
        echo "ERROR: runtime schema-composition helper missing: scripts/$helper" >&2
        exit 1
    fi
done
( cd "$REPO_ROOT/scripts" && tar -cf - "${RUNTIME_COMPOSE_HELPERS[@]}" ) \
    | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/scripts/

# Reconcile the whole generated engine-extension tier so removed/renamed
# extensions cannot linger discoverable in the runtime.
docker exec -u "$HERMES_UID" "$CONTAINER" sh -c \
    'rm -rf /opt/data/extensions && mkdir -p /opt/data/extensions'
if find "$REPO_ROOT/extensions" -mindepth 2 -maxdepth 2 -name extension.yaml -print -quit \
        | grep -q .; then
    ( cd "$REPO_ROOT" && tar --exclude='__pycache__' --exclude='*.pyc' -cf - extensions ) \
        | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/
fi
echo "  runtime schema composer + engine extension inputs deployed"

# --- engine base-schema -> /opt/data/config/ (okengine#90) ---
# The core (types/namespaces/optionals) lives in config/base-schema.yaml; the staged schema_lib
# resolves it at ../config relative to /opt/data/scripts (== /opt/data/config). Without it, cron
# lanes would see only the pack's domain types and miss the engine-owned core.
if [ -f "$REPO_ROOT/config/base-schema.yaml" ]; then
    ( cd "$REPO_ROOT/config" && tar -cf - base-schema.yaml ) \
        | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/config/
    echo "  engine base-schema deployed to $CONTAINER:/opt/data/config/"
fi

# --- tools/schema_validator.py REFERENCE -> /opt/data/config/ (okengine#326 [15]) ---
# The OKF conformance validator is BAKED at /opt/hermes/tools — the write-guard hook AND the staged
# importer_guard/schema_drift_lint crons import it from there. It is IMAGE-only, so a validator change
# staged without an image rebuild leaves the write path + those crons enforcing the OLD rules, with
# nothing catching the drift (the same trap as base-schema above). Stage a REFERENCE copy purely so
# deployment_validate can compare it against the baked copy and FAIL a stale image. Nothing imports
# this copy (the crons import `tools.schema_validator` from the baked /opt/hermes/tools).
if [ -f "$REPO_ROOT/tools/schema_validator.py" ]; then
    ( cd "$REPO_ROOT/tools" && tar -cf - schema_validator.py ) \
        | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/config/
    echo "  schema_validator reference deployed to $CONTAINER:/opt/data/config/"
fi
if [ -d "$PACK_SCRIPTS" ]; then
    pcount="$(find "$PACK_SCRIPTS" -maxdepth 1 -name '*.py' | wc -l)"
    if [ "$pcount" -gt 0 ]; then
        ( cd "$PACK_SCRIPTS" && tar -cf - ./*.py ) \
            | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/scripts/
        echo "  $pcount pack (domain) cron script(s) deployed from $PACK_SCRIPTS"
    fi
else
    echo "  (pack scripts not found at $PACK_SCRIPTS — engine-only deploy)"
fi

# --- reconcile: drop FLAT cron-script fossils no longer in source (invariant-audit #46). ---
# tar extraction only ADDS/overwrites, never removes, so a script deleted or RENAMED in scripts/cron/
# (e.g. moved into an extension, exactly d8657f7's select_prediction_candidates.py) lingers staged and
# importable forever: check_crons still finds the fossil and passes green, so an unregenerated lane
# silently keeps executing DELETED code. Remove any TOP-LEVEL *.py in /opt/data/scripts that isn't in
# the current engine+pack source set. Namespaced extension subdirs are untouched (they reconcile via
# their own stage plan below); the engine owns the flat *.py namespace here.
ALLOW="$( { find "$SRC_DIR" -maxdepth 1 -name '*.py' -printf '%f\n'; \
            printf '%s\n' "${RUNTIME_COMPOSE_HELPERS[@]}"; \
            if [ -d "$PACK_SCRIPTS" ]; then find "$PACK_SCRIPTS" -maxdepth 1 -name '*.py' -printf '%f\n'; fi; } | sort -u )"
REMOVED="$(printf '%s\n' "$ALLOW" | docker exec -i -u "$HERMES_UID" "$CONTAINER" python3 -c '
import os, sys
allow = {l.strip() for l in sys.stdin if l.strip()}
d = "/opt/data/scripts"
out = []
for name in sorted(os.listdir(d)):
    p = os.path.join(d, name)
    if name.endswith(".py") and os.path.isfile(p) and name not in allow:
        os.unlink(p); out.append(name)
print("\n".join(out))
')"
if [ -n "$REMOVED" ]; then
    echo "  reconciled: removed stale staged script(s) no longer in source:"
    printf '%s\n' "$REMOVED" | sed 's/^/    - /'
fi

# --- enabled extension scripts -> /opt/data/scripts/<id>/ (okengine#128) ---
# Each enabled in-gateway operation extension stages its *.py into a NAMESPACED
# subdir; the synthesized cron job's `script:` is /opt/data/scripts/<id>/<file>
# (extension_compose.SCRIPTS_ROOT). The plan comes from the composer, so a broken
# enabled set (dup id, missing dep) fails the deploy BEFORE staging — fail-loud.
if ! EXT_PLAN="$(python3 "$REPO_ROOT/scripts/framework.py" extensions stage-plan "$PACK_DIR")"; then
    echo "ERROR: extension staging plan failed (see errors above)." >&2
    exit 1
fi
if [ -n "$EXT_PLAN" ]; then
    estaged=0
    while IFS=$'\t' read -r ext_id ext_dir; do
        [ -z "$ext_id" ] && continue
        pyn="$(find "$ext_dir" -maxdepth 1 -name '*.py' | wc -l)"
        if [ "$pyn" -eq 0 ]; then
            echo "  ⚠ extension '$ext_id' has no *.py to stage in $ext_dir" >&2
            continue
        fi
        docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p "/opt/data/scripts/$ext_id"
        ( cd "$ext_dir" && tar -cf - ./*.py ) \
            | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C "/opt/data/scripts/$ext_id/"
        estaged=$((estaged + 1))
        echo "  extension '$ext_id': $pyn script(s) -> /opt/data/scripts/$ext_id/"
    done <<< "$EXT_PLAN"
    echo "  $estaged enabled extension(s) staged"
else
    echo "  (no enabled extensions to stage)"
fi

# --- reader extension panels (okengine#160) -> <pack>/.okengine/reader-panels.json ---
# Type-bound panel bindings for the reader (it reads them from the vault it mounts). Host-side
# (a vault file, not a /opt/data script). Self-declared panels (e.g. viz's map) don't need this.
# stage-panels writes `{}` and exits 0 even with ZERO panels, so a non-zero exit is ALWAYS a real
# error — a broken extension config or a panel-TYPE COLLISION (collect_reader_panels fails loud when
# two enabled extensions bind the same page type). Do NOT swallow it as "skipped": that shipped the
# deploy green with an ambiguous/broken reader panel map. Fail the deploy, like the stage-plan above
# (invariant-audit B6.4).
if ! python3 "$REPO_ROOT/scripts/framework.py" extensions stage-panels "$PACK_DIR"; then
    echo "ERROR: reader-panels staging failed — broken extension config or a panel-type collision" \
         "(see the FAIL lines above)." >&2
    exit 1
fi

# --- domain data -> /opt/data/config/ ---
# Domain data tables consumed at runtime (cron-plus mounts only /opt/data/, so these must sit
# alongside the scripts). The pack contract is `data/*` (docs/deploy-a-new-domain.md) — stage the
# WHOLE data/ tree, not a curated allowlist, else a table a pack adds is silently dropped and its
# cron FileNotFoundErrors at the tick (okengine invariant-audit #9). Enumeration lives in
# scripts/lib/pack_data.sh so it's unit-testable without Docker.
# (publishers.canonical.json is NOT shipped in a pack's data/: the publisher-canonical-drain cron
#  maintains it IN-PLACE in the vault at config/publishers.canonical.json — that's the live source
#  of truth, also read by the scripts/normalize_publishers.py dev tool.)
if [ -d "$PACK_DATA" ]; then
    cfgs=()
    while IFS= read -r cfg; do
        [ -n "$cfg" ] && cfgs+=("$cfg")
    done < <(enumerate_pack_data_files "$PACK_DATA")
    if [ "${#cfgs[@]}" -gt 0 ]; then
        docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/config
        ( cd "$PACK_DATA" && tar -cf - "${cfgs[@]}" ) \
            | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/config/
        echo "  ${#cfgs[@]} pack data file(s) deployed to $CONTAINER:/opt/data/config/"
    else
        echo "  (no pack data files to deploy under $PACK_DATA)"
    fi
else
    echo "  (pack data not found at $PACK_DATA — skipping domain data deploy)"
fi

# --- pack feed lists (*.opml) -> /opt/data/config/ ---
# Read by the generic feed_fetch.py at runtime (feeds = pure config).
PACK_FEEDS="$PACK_DIR/feeds"
if [ -d "$PACK_FEEDS" ]; then
    ocount="$(find "$PACK_FEEDS" -maxdepth 1 -name '*.opml' | wc -l)"
    if [ "$ocount" -gt 0 ]; then
        docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/config
        ( cd "$PACK_FEEDS" && tar -cf - ./*.opml ) \
            | docker exec -i -u "$HERMES_UID" "$CONTAINER" tar -xf - -C /opt/data/config/
        echo "  $ocount pack feed list(s) deployed to $CONTAINER:/opt/data/config/"
    fi
fi

# --- declarative source connectors -> /opt/data/config/connectors/ (okengine#273) ---
# framework validate has already enforced the connector contract. Stage manifests only;
# authoring fixtures remain in the source tree and cannot accidentally become live inputs.
if [ -d "$PACK_CONNECTORS" ]; then
    mapfile -t connector_files < <(
        find "$PACK_CONNECTORS" -maxdepth 1 -type f \( -name '*.yaml' -o -name '*.yml' \) \
            -printf '%f\n' | sort
    )
    if [ "${#connector_files[@]}" -gt 0 ]; then
        docker exec -u "$HERMES_UID" "$CONTAINER" mkdir -p /opt/data/config/connectors
        ( cd "$PACK_CONNECTORS" && tar -cf - "${connector_files[@]}" ) \
            | docker exec -i -u "$HERMES_UID" "$CONTAINER" \
                tar -xf - -C /opt/data/config/connectors/
        echo "  ${#connector_files[@]} source connector manifest(s) deployed to /opt/data/config/connectors/"
    else
        echo "  (no source connector manifests found under $PACK_CONNECTORS)"
    fi
fi

echo "  done."
