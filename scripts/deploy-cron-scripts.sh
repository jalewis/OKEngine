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
# Usage:  bash scripts/deploy-cron-scripts.sh [--prune-stale-extensions]
#         (run after editing scripts/cron/*.py and committing)
#
#   --prune-stale-extensions  remove staged extension dirs (/opt/data/scripts/<id>/) that the
#                             current stage plan no longer stages (disabled, removed or no longer
#                             discovered). Without it they are only REPORTED (okengine#994 [27]).
#
# Idempotent.

set -euo pipefail

PRUNE_STALE_EXTENSIONS=0
for arg in "$@"; do
    case "$arg" in
        --prune-stale-extensions) PRUNE_STALE_EXTENSIONS=1 ;;
        *) echo "ERROR: unknown argument '$arg' (usage: deploy-cron-scripts.sh [--prune-stale-extensions])" >&2
           exit 2 ;;
    esac
done

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
# shellcheck source=lib/baked_validator_preflight.sh
. "$REPO_ROOT/scripts/lib/baked_validator_preflight.sh"
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

# Refuse to stage onto a gateway whose BAKED engine is older than these scripts: a staged script
# importing a name the image lacks would make every cron that imports it raise ImportError
# (okengine#997, #1017). The probe is derived from exactly what is about to be staged, the pack's
# scripts and connectors included (#1016). Before ANY write, so a refused stage leaves the running
# scripts untouched.
# The enabled extensions' staging plan comes from the composer: a broken enabled set (dup id,
# missing dep) fails the deploy here, before staging. Its dirs (engine, pack and operator tier)
# are probed with everything else (!1305 QA N2).
if ! EXT_PLAN="$(python3 "$REPO_ROOT/scripts/framework.py" extensions stage-plan "$PACK_DIR")"; then
    echo "ERROR: extension staging plan failed (see errors above)." >&2
    exit 1
fi
EXT_DIRS=()
while IFS=$'\t' read -r _ext_id ext_dir; do
    [ -n "$ext_dir" ] && EXT_DIRS+=("$ext_dir")
done <<< "$EXT_PLAN"
require_baked_validator "$CONTAINER" "$HERMES_UID" "$SRC_DIR" \
    "${RUNTIME_COMPOSE_HELPERS[@]/#/$REPO_ROOT/scripts/}" "$REPO_ROOT/extensions" \
    "$PACK_SCRIPTS" "$PACK_CONNECTORS" "${EXT_DIRS[@]}" || exit 1

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

# Expected cron-plus revision for the continuous live-drift check. A stage-only engine update does
# not reinstall the scheduler, so publish the CURRENT manifest pin beside the other staged
# references; deployment_validate compares it to the managed clone's actual HEAD every day.
CRON_PLUS_PIN="$(awk '/^  cron-plus:/{f=1} f&&/pinned_sha:/{print $2; exit}' "$REPO_ROOT/engine-manifest.yaml")"
if [ -z "$CRON_PLUS_PIN" ]; then
    echo "ERROR: could not read dependencies.cron-plus.pinned_sha" >&2
    exit 1
fi
printf '%s\n' "$CRON_PLUS_PIN" \
    | docker exec -i -u "$HERMES_UID" "$CONTAINER" sh -c \
        'tmp=/opt/data/config/cron-plus.pin.tmp; cat > "$tmp" && chmod 600 "$tmp" && mv "$tmp" /opt/data/config/cron-plus.pin'
echo "  cron-plus expected pin deployed to $CONTAINER:/opt/data/config/cron-plus.pin"

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
        # Pack scripts are staged AFTER the engine's into the same directory, so a shared basename
        # means the pack copy silently wins on every deploy. That is how okcti-test ran a
        # pre-#267 fork of nvd_import.py for three weeks: deploy exits 0, file present, cron runs
        # "successfully", and the engine's version simply never arrives. Refuse rather than
        # overwrite -- an intended override gets a different basename and a matching cron `script:`.
        shadowed=""
        for pf in "$PACK_SCRIPTS"/*.py; do
            [ -e "$pf" ] || continue
            if [ -f "$SRC_DIR/$(basename "$pf")" ]; then
                shadowed="$shadowed $(basename "$pf")"
            fi
        done
        if [ -n "$shadowed" ]; then
            echo "ERROR: pack script(s) would overwrite an engine cron script of the same name:" >&2
            for s in $shadowed; do echo "  - $s  ($PACK_SCRIPTS/$s shadows $SRC_DIR/$s)" >&2; done
            echo "The pack copy is staged last and would win on every deploy. Rename the pack" >&2
            echo "script (and its cron 'script:') to override deliberately, or delete it to use" >&2
            echo "the engine's. Refusing to stage a silent shadow." >&2
            exit 1
        fi
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

# --- prove the stage LANDED: compare the target against source, not this script's exit code. ---
# A deploy exiting 0 means it ran, not that the right bytes arrived. okcti-test's pre-#267 fork of
# nvd_import.py overwrote the engine's copy on every run for three weeks while this script printed
# "done." each time; it surfaced only when someone hashed the file inside the container. So hash it
# here, every time. Note the ordering: this runs AFTER the fossil reconcile, so `extra` means the
# reconcile itself did not do its job rather than a file it was about to remove.
STAGED_LISTING="$(docker exec -u "$HERMES_UID" "$CONTAINER" \
    sh -c 'cd /opt/data/scripts 2>/dev/null && sha256sum ./*.py 2>/dev/null' || true)"
DRIFT_ARGS=(--source-dir "$SRC_DIR" --label "$(basename "$PACK_DIR")")
if [ -d "$PACK_SCRIPTS" ]; then
    DRIFT_ARGS+=(--source-dir "$PACK_SCRIPTS")
fi
for helper in "${RUNTIME_COMPOSE_HELPERS[@]}"; do
    DRIFT_ARGS+=(--source-file "$REPO_ROOT/scripts/$helper")
done
if ! printf '%s\n' "$STAGED_LISTING" \
        | python3 "$REPO_ROOT/scripts/staged_drift.py" "${DRIFT_ARGS[@]}" --staged-listing -; then
    exit 1
fi

# --- enabled extension scripts -> /opt/data/scripts/<id>/ (okengine#128) ---
# Each enabled in-gateway operation extension stages its *.py into a NAMESPACED
# subdir; the synthesized cron job's `script:` is /opt/data/scripts/<id>/<file>
# (extension_compose.SCRIPTS_ROOT). The plan comes from the composer, so a broken
# enabled set (dup id, missing dep) fails the deploy BEFORE staging — fail-loud.
# (EXT_PLAN was computed before the baked-engine preflight, which probes these dirs too.)
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

# --- stale extension dirs (okengine#994 [27]) ---
# tar extraction only ADDS, so a dir staged for an extension that is since disabled, removed or no
# longer discovered stays importable forever, and the validator's "staged scripts but has NO lane"
# WARN turns into permanent noise. Report each one; remove them only on the explicit flag (an
# operator choice, like `framework extensions purge`). The comparison is scripts/staged_extensions.py.
STAGED_EXT_DIRS="$(docker exec -u "$HERMES_UID" "$CONTAINER" \
    find /opt/data/scripts -mindepth 1 -maxdepth 1 -type d -printf '%f\n')"
STALE_EXT="$(printf '%s\n' "$STAGED_EXT_DIRS" \
    | python3 "$REPO_ROOT/scripts/staged_extensions.py" --staged-listing - \
        --plan <(printf '%s\n' "$EXT_PLAN"))"
if [ -n "$STALE_EXT" ]; then
    if [ "$PRUNE_STALE_EXTENSIONS" = 1 ]; then
        while IFS= read -r stale; do
            docker exec -u "$HERMES_UID" "$CONTAINER" rm -rf -- "/opt/data/scripts/$stale"
            echo "  reconciled: removed staged extension dir no longer in the stage plan: $stale"
        done <<< "$STALE_EXT"
    else
        echo "  ⚠ staged extension dir(s) no longer in the stage plan (disabled, removed or not" \
             "discovered); their code stays importable:" >&2
        printf '%s\n' "$STALE_EXT" | sed 's/^/    - /' >&2
        echo "    remove them: CRON_PACK_DIR='$PACK_DIR' bash '$REPO_ROOT/scripts/deploy-cron-scripts.sh'" \
             "--prune-stale-extensions" >&2
    fi
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

# A standalone stage is a deploy operation, not a harmless copy. The read MCP bakes these search
# libraries into its own image while this script updates the gateway-mounted source. Refuse to
# report success when those two runtimes now disagree; otherwise search/graph silently serves old
# behavior until somebody happens to rebuild the MCP image later.
# The rebuild goes through deploy.sh: it builds onto this deployment's scoped engine image tag and
# stamps the provenance label. A compose build given only `-f` and the pack's own file drops the
# engine image override that .env's COMPOSE_FILE carries, so it rebuilt onto the shared `okengine-mcp:latest`
# that other packs resolve (okengine#1042 [13]/[31]).
mcp_rebuild_hint() {
    echo "       rebuild it with this deployment's pinned engine image: cd '$PACK_DIR' && bash '$REPO_ROOT/scripts/deploy.sh'" >&2
    echo "       (not a compose build with -f and the pack's compose file alone: that drops the engine image override)" >&2
}
for lib in kb_search.py tier_lib.py schema_lib.py; do
    STAGED_HASH="$(docker exec "$CONTAINER" sh -c "sha256sum /opt/data/scripts/$lib 2>/dev/null | cut -d' ' -f1")"
    MCP_HASH="$(docker compose -f "$PACK_DIR/docker-compose.yml" exec -T okengine-mcp \
        sh -c "sha256sum /app/scripts/$lib 2>/dev/null | cut -d' ' -f1" 2>/dev/null || true)"
    if [ -z "$STAGED_HASH" ] || [ -z "$MCP_HASH" ]; then
        echo "ERROR: cannot prove read-MCP $lib parity after staging (one or both copies missing)." >&2
        echo "       Rebuild/recreate okengine-mcp, then rerun this stage." >&2
        exit 1
    fi
    if [ "$STAGED_HASH" != "$MCP_HASH" ]; then
        echo "ERROR: read-MCP $lib is stale after staging; refusing a split-version deploy." >&2
        mcp_rebuild_hint
        exit 1
    fi
done
STAGED_HASH="$(docker exec "$CONTAINER" sh -c "sha256sum /opt/data/config/base-schema.yaml 2>/dev/null | cut -d' ' -f1")"
MCP_HASH="$(docker compose -f "$PACK_DIR/docker-compose.yml" exec -T okengine-mcp \
    sh -c "sha256sum /app/config/base-schema.yaml 2>/dev/null | cut -d' ' -f1" 2>/dev/null || true)"
if [ -z "$STAGED_HASH" ] || [ -z "$MCP_HASH" ]; then
    echo "ERROR: cannot prove read-MCP base-schema.yaml parity after staging (one or both copies missing)." >&2
    echo "       Rebuild/recreate okengine-mcp, then rerun this stage." >&2
    exit 1
fi
if [ "$STAGED_HASH" != "$MCP_HASH" ]; then
    echo "ERROR: read-MCP base-schema.yaml is stale after staging; refusing a split-version deploy." >&2
    mcp_rebuild_hint
    exit 1
fi
echo "  read-MCP baked search libraries match staged source"

echo "  done."
