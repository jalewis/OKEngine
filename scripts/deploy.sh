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
#   --skip-build     never build the immutable image selected for this deployment
#   --release-digests <tested-digests.json>
#                    deploy the images the release pipeline TESTED instead of building any
#                    (okengine#1048; implies --skip-build). Loads each by digest through the
#                    pinned containerized loader (scripts/release_load.py), refuses a list for
#                    another commit or a dirty checkout, verifies every config ID before tagging,
#                    and makes post-deploy verify FAIL on a missing or untested image.
#   --skip-validate  skip the pre-deploy validate gate
#   --no-upgrade     skip the pre-validate engine-pin reconcile (framework upgrade)
#   --no-crons       bring up containers only; don't deploy crons
#   --fix-perms      make the pack tree writable by HERMES_UID (local convenience;
#                    otherwise a non-writable tree fails before compose with remediation)
#   --kickstart      after deploy, populate the vault NOW (ingest -> compile -> dashboards
#                    -> brief) instead of waiting for the schedule. Opt-in: the compile +
#                    brief stages spend on the model. See scripts/kickstart.sh.
#
# Exit codes (okengine#597) — the third state used to be reported as success:
#   0   up and verified
#   3   up, but post_deploy_verify.sh reported issues (the stack IS running; see remediation)
#   1   bring-up failed (no compose file, validate/recompose/policy failure)
#   2   usage error (unknown flag)
# A human sees no change: the stack still comes up and the remediation still prints. A scripted
# caller — a fleet roll, a CI job, `deploy.sh && <next>` — can finally tell the states apart.
#
# Env: OKENGINE_VERIFY_DELAY (default 5) — seconds to let the reader/MCP bind before verifying.
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# okengine#1043: host-side python (validate, the image override, post_deploy_verify's checks)
# imports the engine being deployed, never another okengine or `tools` in the deployer's
# site-packages: a venv's stale validator failed the v0.15.12 roll on code this checkout lacks.
export PYTHONPATH="$ENGINE_DIR/src:$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
PACK="$PWD"
REBUILD=0; SKIP_BUILD=0; SKIP_VALIDATE=0; NO_CRONS=0; FIX_PERMS=0; KICKSTART=0; NO_UPGRADE=0
RELEASE_DIGESTS=""
while [ "$#" -gt 0 ]; do
    a="$1"; shift
    case "$a" in
        --release-digests=*) RELEASE_DIGESTS="${a#*=}"; [ -n "$RELEASE_DIGESTS" ] || { echo "--release-digests needs a file" >&2; exit 2; } ;;
        --release-digests)
            [ "$#" -gt 0 ] && [ -n "$1" ] || { echo "--release-digests needs a file" >&2; exit 2; }
            RELEASE_DIGESTS="$1"; shift ;;
        --rebuild)       REBUILD=1 ;;
        --skip-build)    SKIP_BUILD=1 ;;
        --skip-validate) SKIP_VALIDATE=1 ;;
        --no-upgrade)    NO_UPGRADE=1 ;;
        --no-crons)      NO_CRONS=1 ;;
        --fix-perms)     FIX_PERMS=1 ;;
        --kickstart)     KICKSTART=1 ;;
        -*)              echo "unknown flag: $a" >&2; exit 2 ;;
        *)               PACK="$a" ;;
    esac
done
PACK="$(cd "$PACK" && pwd)"
if [ -n "$RELEASE_DIGESTS" ]; then
    # Resolved before anything changes directory. A missing list is a FAIL here: the operator asked
    # for tested digests, and there is no evidence to deploy from.
    if [ ! -f "$RELEASE_DIGESTS" ]; then
        echo "ERROR: --release-digests: $RELEASE_DIGESTS does not exist" >&2
        exit 1
    fi
    RELEASE_DIGESTS="$(cd "$(dirname "$RELEASE_DIGESTS")" && pwd)/$(basename "$RELEASE_DIGESTS")"
    if [ "$REBUILD" = 1 ]; then
        echo "--release-digests deploys CI-tested images; it cannot be combined with --rebuild" >&2
        exit 2
    fi
    SKIP_BUILD=1
fi

if [ ! -f "$PACK/docker-compose.yml" ]; then
    echo "ERROR: $PACK has no docker-compose.yml — run from a pack dir (or pass one)." >&2
    exit 1
fi

# Resolve uid:gid. Precedence: an explicit shell export > a value already pinned in the pack's
# .env (an operator's deliberate choice / a prior deploy) > the invoking user's uid. Preferring
# the .env pin over $(id -u) means re-running deploy.sh as a different user can't silently retag
# the tree. Default to the invoking user's uid so a clone-as-yourself pack tree is writable out
# of the box (okengine#102): you own the vault, and the gateway remaps to it. Pin a FIXED uid
# (+ chown the tree to it) for a vault you'll move between hosts or share across operators —
# see docs/deploy-a-new-domain.md §2.
# The .env pin is read through scripts/lib/hermes_uid.sh, i.e. the ONE dotenv parser compose agrees
# with (last assignment wins; quotes, `export` and inline comments handled). A private first-wins
# `grep '^HERMES_UID=[0-9]+'` here once disagreed with compose on a duplicated, quoted or exported
# pin: this deploy exported its own reading, and the next bare `docker compose up` ran the gateway
# as a different uid than the tree owner (okengine#1044 [1], the okengine#994 A class). An unset
# or empty pin is a miss (`|| true`: under `set -euo pipefail` a bare miss would kill the deploy).
# shellcheck source=lib/hermes_uid.sh
. "$ENGINE_DIR/scripts/lib/hermes_uid.sh"
_env_uid="$(_okengine_env_file_val "$PACK" HERMES_UID || true)"
_env_gid="$(_okengine_env_file_val "$PACK" HERMES_GID || true)"
export HERMES_UID="${HERMES_UID:-${_env_uid:-$(id -u)}}" HERMES_GID="${HERMES_GID:-${_env_gid:-$(id -g)}}"
PYTHON="${PYTHON:-python3}"

echo "==> OKEngine deploy: $PACK"
echo "    engine $ENGINE_DIR · uid:gid $HERMES_UID:$HERMES_GID"

# PERSIST the resolved uid:gid to .env if not already pinned, so EVERY later op uses the same uid
# the runtime tree is owned by — a plain `docker compose up`, a `--force-recreate` (config reads
# need one), or a standalone `deploy-cron-*.sh`. Without this the uid is set only for THIS process;
# a later bare compose call falls back to the image default (10000), desyncs ownership from the
# mounted tree, and the cron-plus ticker dies on a `.tick.lock` PermissionError — nothing schedules
# (the exact trap that forced a full rebuild of a review instance). Idempotent; never overrides an
# existing pin. CREATE .env if the operator hasn't yet: ensure-runtime.sh (step 2) appends its own
# keys with `>>` and would otherwise mint a .env with NO uid pin on a clean deploy — pin FIRST here.
# .env holds model keys, OKENGINE_MCP_TOKEN and Postgres passwords and is read by docker compose
# on the HOST only, so it is always owner-only: created under umask 077 and tightened if an older
# deploy left it at the caller's umask (okengine#665 — INSTALL promised 600; nothing set it).
[ -f "$PACK/.env" ] || ( umask 077; : > "$PACK/.env" )
chmod 600 "$PACK/.env" 2>/dev/null || echo "WARN: could not chmod 600 $PACK/.env (not the owner?) — secrets may be readable by others" >&2
# "Pinned" means compose sees a non-empty HERMES_UID in .env (same parser as above): `export
# HERMES_UID=` is a pin, and an empty `HERMES_UID=` is not one (compose would default it to 10000).
if ! _okengine_env_file_val "$PACK" HERMES_UID >/dev/null; then
    printf '\n# uid:gid the gateway remaps to; pinned by deploy.sh so bare docker-compose ops match\n# the runtime tree owner (else compose defaults to 10000 and the scheduler dies on a perm error).\nHERMES_UID=%s\nHERMES_GID=%s\n' "$HERMES_UID" "$HERMES_GID" >> "$PACK/.env"
    echo "    pinned HERMES_UID:GID=$HERMES_UID:$HERMES_GID -> .env (matches the runtime tree owner)"
fi

# 0. reconcile the engine pin. A pack that pins an OLDER engine release than the one running this
#    deploy is brought CURRENT via `framework upgrade` (bumps engine.version + runs any registered
#    migrations in (pin, target] under a roll-forward gate that AUTO-ROLLS-BACK on failure) BEFORE the
#    validate gate — so bumping the engine doesn't dead-end every lagging pack's deploy at the
#    "incompatible release series" FAIL / --skip-validate (okengine#359). Exit codes:
#      0 = reconciled or already current;
#      2 = nothing attempted (an AHEAD pin — downgrade refused — or an unparseable pin): the pin is
#          unchanged and the validate gate below surfaces the real, actionable mismatch, so continue;
#      3 = a pin-bump-only upgrade (no migration ran) rolled back on a `framework validate` FAIL:
#          STOP, and name validate, not a migration (okengine#1042 [28]); --skip-validate reaches
#          this step as `--no-validate`;
#      anything else = a reconcile that SHOULD have happened FAILED (a migration rolled back, could
#          not load, or the migration state is unreadable). STOP: validate passes a same-series pin,
#          so continuing would ship a patch release's un-migrated pack as a success (okengine#991).
#      Precedence: an unreadable .okengine/migrations-state.json is exit 1 even when the pin alone
#      would be exit 2 (ahead or unparseable) — the state is read before the pin is compared.
#    Opt out with --no-upgrade.
if [ "$NO_UPGRADE" = 0 ] && [ -f "$PACK/engine.version" ]; then
    echo "==> [0/6] reconcile engine pin (framework upgrade)"
    _upgrade_rc=0
    # --skip-validate is the operator's explicit opt-out of `framework validate`; it must reach the
    # upgrade's roll-forward validate too, or a pre-existing FAIL aborts here before the flag is
    # honored (okengine#1042 [28]). It also skips that run's conformance-regression check.
    _upgrade_flags=(--apply)
    [ "$SKIP_VALIDATE" = 1 ] && _upgrade_flags+=(--no-validate)
    "$PYTHON" "$ENGINE_DIR/scripts/framework.py" upgrade "$PACK" "${_upgrade_flags[@]}" || _upgrade_rc=$?
    case "$_upgrade_rc" in
        0) ;;
        2) echo "    note: pin not reconciled (ahead or unparseable pin) — validate will report it" >&2 ;;
        3) echo "ERROR: framework validate FAILED after the pin bump (no migration ran); the pin was" >&2
           echo "       rolled back. Fix the FAILs \`framework validate\` reports above, or re-run with" >&2
           echo "       --skip-validate to deploy past them knowingly; deploy ABORTED." >&2
           exit 1 ;;
        *) echo "ERROR: engine pin reconcile FAILED (framework upgrade exit $_upgrade_rc) — a migration" >&2
           echo "       failed and was rolled back, or could not run. Fix the cause shown above and" >&2
           echo "       re-run; deploy ABORTED (use --no-upgrade only to deploy the old pin knowingly)." >&2
           exit 1 ;;
    esac
fi

# 1. validate — don't deploy a broken pack.
if [ "$SKIP_VALIDATE" = 0 ]; then
    echo "==> [1/6] validate"
    if ! "$PYTHON" "$ENGINE_DIR/scripts/framework.py" validate "$PACK" --quiet; then
        echo "ERROR: validation failed. A stale engine.version pin auto-reconciles above (see [0/6]);" >&2
        echo "       for other FAILs, fix them or re-run with --skip-validate." >&2
        exit 1
    fi
fi

# 1b. Recompose the schema artifact. The enforced write path prefers <pack>/.okengine/
#     composed-schema.yaml when present, but ONLY `framework extensions enable/disable` ever
#     regenerated it — so a plain schema.yaml edit was silently ignored on the write path until the
#     next extension toggle (okengine#178). write_composed_schema regenerates it from the CURRENT
#     schema.yaml + enabled extensions, or REMOVES a stale artifact when no schema-extensions remain
#     (a no-op when a pack has neither). FATAL on error (invariant-audit HIGH #4): on ANY recompose
#     error write_composed_schema writes NOTHING and leaves the STALE artifact, which the enforced
#     write path then keeps using UNCONDITIONALLY — so a broken/renamed extension fragment silently
#     freezes the governing schema and every future schema.yaml edit is ignored on the write path.
#     A WARN here (the old behavior) let that ship green. Fail the deploy so it's fixed, not frozen.
if ! "$PYTHON" -c "import sys, pathlib; sys.path[:0] = ['$ENGINE_DIR/scripts', '$ENGINE_DIR/src']; import extension_compose as c; errs = c.write_composed_schema(pathlib.Path('$PACK')); [print('    ERROR: recompose:', e, file=sys.stderr) for e in (errs or [])]; sys.exit(1 if errs else 0)"; then
    echo "==> [1b/6] FAILED: composed-schema recompose errored — the enforced write path is frozen on" >&2
    echo "    the stale .okengine/composed-schema.yaml (a broken/renamed extension schema fragment)." >&2
    echo "    Fix the fragment (or disable the extension) and re-run; deploy ABORTED." >&2
    exit 1
fi
echo "    schema artifact recomposed from current schema.yaml + extensions"

# Materialize the exact composed policy that write, audit, CI, and Cockpit consume.
# This is generated runtime state; a digest mismatch is a deploy failure, not a warning.
if ! OKENGINE_POLICY_CATALOG="$ENGINE_DIR/config/policy/catalog.yaml" \
     "$PYTHON" "$ENGINE_DIR/tools/policy_plane.py" materialize --vault "$PACK"; then
    echo "==> [1c/6] FAILED: policy composition/materialization failed" >&2
    exit 1
fi
echo "    policy artifact composed from engine + pack + enabled extension policy"

# Re-derive the scoped MCP token store from the CURRENT manifests (okengine#1032 [17]). It is a copy
# that only `extensions enable` refreshed, so a deploy shipped whatever scopes and write policy it
# last held: an identity_edit grant the manifest has since dropped stayed live. A stale record is
# rewritten in place (WARN, no token rotation); discovery failure aborts the deploy.
if ! "$PYTHON" "$ENGINE_DIR/scripts/framework.py" extensions reconcile-tokens "$PACK"; then
    echo "==> [1c/6] FAILED: the extension token store could not be reconciled" >&2
    exit 1
fi
echo "    extension token store reconciled from enabled extension manifests"

# Materialize enabled sidecar services before Compose reconciliation. A default backup deliberately
# omits their scoped tokens and token-bearing override; generation therefore fails loud after restore
# with the supported `extensions enable <id>` remediation instead of reporting a successful deploy
# whose enabled sidecar is silently absent. With no enabled sidecars, generation removes a stale
# override left by an earlier disable.
if ! "$PYTHON" "$ENGINE_DIR/scripts/framework.py" extensions sidecar-generate "$PACK"; then
    echo "==> [1d/6] FAILED: enabled sidecar deployment artifacts could not be generated" >&2
    echo "    If this is a secrets-excluded restore, re-run 'framework extensions enable <pack> <id>'" >&2
    echo "    for each enabled sidecar to re-mint its scoped token, then deploy again." >&2
    exit 1
fi
echo "    sidecar artifacts reconciled from enabled extensions"

# No fleet regen here (okengine#1029). ensure-runtime (step 2) builds THIS pack's fleet in memory
# to register each contracted lane's dedicated MCP server before the gateway first starts, and
# fails the deploy on a broken fleet; deploy-cron-plus-jobs.sh (step 5) regenerates the shared
# artifact it ships. A regen here only refreshed config/cron-plus-jobs.json, a checkout-wide file
# that a concurrent or later deploy of another pack overwrites, and no per-pack step may read it.

# PostgreSQL is the standard structured-query read path. Generate distinct per-deployment
# credentials and, for packs created before the native compose services shipped, materialize an
# engine-managed compatibility overlay. Run only after validation has accepted the deployment;
# the operation is idempotent and never prints secret values.
echo "==> [1p/6] ensure PostgreSQL projection configuration"
"$PYTHON" "$ENGINE_DIR/scripts/ensure_projection_config.py" "$PACK" --engine "$ENGINE_DIR"

# 2. seed the runtime dir + ensure it's writable by HERMES_UID BEFORE compose binds it,
#    and install the cron-plus scheduler plugin into the runtime (the seeded config
#    enables it, so it must be present before the gateway starts).
echo "==> [2/6] seed runtime (.hermes-data) + cron-plus plugin"
FIX_PERMS="$FIX_PERMS" bash "$ENGINE_DIR/scripts/ensure-runtime.sh" "$PACK"
bash "$ENGINE_DIR/scripts/install-cron-plus.sh" "$PACK"
# 2b. every write server must start with a pack principal (okengine#1027). Judged now, before the
#     gateway is recreated, so a nameless pack.yaml stops the deploy instead of taking every write
#     lane down after the recreate. Not skippable: --skip-validate skips judgment, not an outage.
echo "==> [2b/6] write servers have a pack principal"
"$PYTHON" "$ENGINE_DIR/scripts/check_write_principal.py" "$PACK" || exit 1

# 3. gateway image — build if missing OR stale (label != current checkout), or forced.
#
# First: is this tree fit to be built from AT ALL? The staleness gate below compares the ENGINE_DIR
# checkout against the image, which answers "is the image current" and says nothing about "is this
# checkout the code we mean to ship". On 2026-08-15 a deployment's ENGINE_DIR pointed at a working
# tree on a feature branch behind main; building there would have shipped a cockpit missing a fix
# that was live and serving, and the build, the container health check and the rendered page would
# all have reported success. Refuse rather than warn — every downstream signal is green either way,
# so a warning here is a warning nobody has any reason to read.
"$PYTHON" "$ENGINE_DIR/scripts/engine_source_guard.py" "$ENGINE_DIR" --pack "$PACK"

# Fail closed: an `unknown` revision would give every unattributable engine tree the same
# "immutable" gateway and engine image tags, so two different engines could share one.
if ! cur_sha="$(git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null)" || [ -z "$cur_sha" ]; then
    echo "ERROR: cannot resolve the engine revision (git rev-parse HEAD failed in $ENGINE_DIR);" \
         "immutable image tags need one. Deploy from a git checkout of a tagged release." >&2
    exit 1
fi
# build-engine-image.sh bakes the WORKING TREE (COPYs the tree, not HEAD), yet the staleness gate
# below compares HEAD short-shas — so an UNCOMMITTED edit at HEAD X against an image built at X reads
# "up to date" and the fix never ships, and a dirty --rebuild stamps a clean-X label that later reads
# as verified on any clean checkout at X (invariant-audit #9). Fold the dirty state into the compared
# sha: a dirty tree becomes "X-dirty", which can never equal a clean image label (so the gate always
# rebuilds) and matches the "-dirty" provenance label a dirty build stamps.
if [ -n "$(git -C "$ENGINE_DIR" status --porcelain 2>/dev/null)" ]; then
    ENGINE_DIRTY=1; cur_sha="${cur_sha}-dirty"
else
    ENGINE_DIRTY=0
fi
engine_release="$(awk -F': *' '/^engine_release:/{print $2; exit}' "$ENGINE_DIR/engine-manifest.yaml" | awk '{print $1}')"
[ -n "$engine_release" ] || { echo "ERROR: cannot resolve engine release for immutable image identity" >&2; exit 1; }
gateway_tag="okengine-${engine_release}-${cur_sha}"
gateway_repository="${OKENGINE_IMAGE:-hermes-agent}"
if [ -n "$RELEASE_DIGESTS" ] && [ -n "${OKENGINE_GATEWAY_IMAGE:-}" ]; then
    echo "ERROR: --release-digests selects the gateway image itself; unset OKENGINE_GATEWAY_IMAGE" >&2
    exit 1
fi
gateway_image="${OKENGINE_GATEWAY_IMAGE:-${gateway_repository}:${gateway_tag}}"
if [ -n "${OKENGINE_GATEWAY_IMAGE:-}" ] && [ "$SKIP_BUILD" = 0 ]; then
    echo "ERROR: OKENGINE_GATEWAY_IMAGE selects a prebuilt image; use --skip-build or set OKENGINE_IMAGE to select the build repository" >&2
    exit 1
fi

# The compose-built engine images (reader, mcp, review-write, operation-runner, cockpit,
# projection) get the same immutability, scoped to this deployment as well (okengine#1033): Compose
# stamps the project into every image it builds, so a tag shared across packs names a different
# image after each pack's build, and a shared tag moved by one pack's build recreates every other
# pack onto that code at its next `compose up`. scripts/engine_images.py explains the measurement.
if ! engine_image_tag="$("$PYTHON" "$ENGINE_DIR/scripts/engine_images.py" tag "$PACK" \
        --release "$engine_release" --sha "$cur_sha")" || [ -z "$engine_image_tag" ]; then
    echo "ERROR: cannot derive the immutable engine image tag for $PACK" >&2
    exit 1
fi
# A CI-tested gateway gets its own tag: the plain release+sha tag may name a host build that other
# deployments on this host run, and moving it would recreate them onto different bytes. It is scoped
# to this compose project as well, like the engine images (okengine#1033): two pipelines for one
# commit record different gateway IDs, and a shared `.tested` tag moved by one pack's roll would
# recreate every other pack onto that image at its next `compose up` (okengine#1057 [11]).
if [ -n "$RELEASE_DIGESTS" ]; then
    gateway_tag="${engine_image_tag}.tested"
    if [ "${#gateway_tag}" -gt 128 ]; then
        echo "ERROR: the tested gateway tag $gateway_tag is longer than Docker's 128 characters;" \
             "set a shorter COMPOSE_PROJECT_NAME" >&2
        exit 1
    fi
    gateway_image="${gateway_repository}:${gateway_tag}"
fi
# The compose project (and so that tag) is the directory basename unless COMPOSE_PROJECT_NAME says
# otherwise. Two deployments that share it recreate each other's containers onto whichever ran
# `compose up` last, and share one image tag (okengine#1042 [8]). Refuse before touching anything.
claim_rc=0
"$PYTHON" "$ENGINE_DIR/scripts/engine_images.py" claim "$PACK" || claim_rc=$?
case "$claim_rc" in
    0) ;;
    2) echo "WARNING: could not check whether another directory owns this compose project (undetectable, not a pass)" >&2 ;;
    *) echo "ERROR: refusing to deploy $PACK into a compose project another deployment owns" >&2; exit 1 ;;
esac

# okengine#1058: before ANY pin is written, prove the release can be loaded at all: the list, the
# checkout, the credentials, the registry name (resolved on this host and mapped into the loader)
# and a `crane digest` through the loader. A resolution or credential failure FAILs here with .env
# and the image override untouched, so even a deploy killed mid-load cannot strand them.
tested_list=""
if [ -n "$RELEASE_DIGESTS" ]; then
    echo "==> [3/6] preflight: CI-tested images and registry access ($RELEASE_DIGESTS)"
    if ! "$PYTHON" "$ENGINE_DIR/scripts/release_load.py" "$RELEASE_DIGESTS" --pack "$PACK" \
            --engine-dir "$ENGINE_DIR" --gateway-image "$gateway_image" \
            --engine-tag "$engine_image_tag" --preflight; then
        echo "ERROR: --release-digests: preflight failed; no pin was written and nothing was deployed" >&2
        exit 1
    fi
    # okengine#1057 [6]: the deployment keeps its own copy of the list it was rolled from, in the
    # git-ignored deploy-generated dir, and .env names THAT copy: the operator's file (a /tmp
    # download, a pruned deploy clone) may vanish, and post_deploy_verify would then FAIL a
    # deployment that runs exactly the tested images.
    tested_list="$PACK/.okengine/generated/tested-digests.json"
fi

# Persist the exact image and the engine-managed override in the deployment itself. This protects
# later bare `docker compose up` calls too; an environment export that exists only for this deploy
# would leave the old cross-deployment :latest hazard intact. Preserve unrelated COMPOSE_FILE
# entries while ensuring the override is present exactly once.
gateway_override="$PACK/docker-compose.okengine-image.yml"
# --release-digests persists these pins BEFORE release_load.py (step 3) loads the images they name.
# Snapshot both files first and restore them byte for byte on any exit before the load is verified:
# otherwise a failed load ("nothing was deployed") leaves the pack pinned to images that were never
# loaded, and the next bare `compose up` or gateway recreate fails, or builds untested code under
# the tested tag. The snapshot holds .env secrets: mktemp -d is owner-only, and it is always removed.
_pin_snapshot=""
_tested_stage=""
_restore_release_pins() {
    local rc=$?
    [ -z "$_tested_stage" ] || rm -f "$_tested_stage"
    if [ -n "$_pin_snapshot" ]; then
        cp -p "$_pin_snapshot/env" "$PACK/.env"
        if [ -e "$_pin_snapshot/override" ]; then
            cp -p "$_pin_snapshot/override" "$gateway_override"
        else
            rm -f "$gateway_override"
        fi
        rm -rf "$_pin_snapshot"
        echo "    restored .env and $(basename "$gateway_override") to their pre-deploy pins" >&2
    fi
    exit "$rc"
}
if [ -n "$RELEASE_DIGESTS" ]; then
    _pin_snapshot="$(mktemp -d)"
    cp -p "$PACK/.env" "$_pin_snapshot/env"
    if [ -e "$gateway_override" ]; then
        cp -p "$gateway_override" "$_pin_snapshot/override"
    fi
    trap _restore_release_pins EXIT
fi
cat >"$gateway_override" <<'EOF'
# Generated by OKEngine deploy.sh. Do not hand-edit; regenerated on every deploy.
services:
  gateway:
    image: ${OKENGINE_GATEWAY_IMAGE:?deploy.sh must pin an immutable gateway image}
    # Engine-owned availability contract. Keep this in the deploy override as well as the
    # scaffold: existing packs must gain new fail-loud health semantics on their next deploy.
    healthcheck:
      test: ["CMD-SHELL", "now=$$(date +%s); tick=$$(stat -c %Y /opt/data/cron-plus/.tick.lock 2>/dev/null) || exit 1; [ $$((now-tick)) -le 180 ] || exit 1; [ ! -s /opt/data/cron-plus/.scheduler-stalled ] || exit 1; owner=/opt/vault/.okengine/corpus/lock-owner.json; [ ! -e $$owner ] || { mtime=$$(stat -c %Y $$owner) || exit 1; [ $$((now-mtime)) -le 300 ] || exit 1; }; gen=$$(( $$(awk '/^btime/{print $$2}' /proc/stat) + $$(sed 's/.*) //' /proc/1/stat | awk '{print $$20}') / $$(getconf CLK_TCK) )); ! find /opt/data/cron-plus/runs -type f -name '*.json' -mmin +60 -newermt @$$gen -exec grep -lE '\"status\"[[:space:]]*:[[:space:]]*\"running\"' {} + 2>/dev/null | grep -q . || exit 1; for stat in /proc/[0-9]*/stat; do grep -q ') D ' $$stat 2>/dev/null && exit 1; done; exit 0"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 180s
EOF
python3 - "$PACK/.env" "$gateway_image" "$engine_image_tag" "$tested_list" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
image = sys.argv[2]
engine_tag = sys.argv[3]
# okengine#1048: the tested-digest list this deployment was rolled from, so every later
# post_deploy_verify checks against it. A deploy without --release-digests builds on the host and
# removes it: those images are not the tested ones, and the stale list would only fail them.
tested_list = sys.argv[4]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
values = {}
order = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0]
        values[key] = line
        order.append(key)
values["OKENGINE_GATEWAY_IMAGE"] = f"OKENGINE_GATEWAY_IMAGE={image}"
values["OKENGINE_ENGINE_IMAGE_TAG"] = f"OKENGINE_ENGINE_IMAGE_TAG={engine_tag}"
if tested_list:
    values["OKENGINE_TESTED_DIGESTS"] = f"OKENGINE_TESTED_DIGESTS={tested_list}"
else:
    values.pop("OKENGINE_TESTED_DIGESTS", None)
parts = [p for p in values.get("COMPOSE_FILE", "COMPOSE_FILE=").split("=", 1)[1].split(":") if p]
sidecars = ".okengine/generated/sidecars.compose.yml"
# Sidecars are one-shot scheduled services. They must never be part of the default project used by
# `docker compose up`, or a deploy launches untrusted extension code outside budget/schedule gates.
# Remove relative and absolute legacy spellings; generated trigger wrappers attach the override
# explicitly only for `docker compose run --rm`.
sidecar_path = (path.parent / sidecars).resolve()
def compose_path(part):
    candidate = Path(part).expanduser()
    return candidate.resolve() if candidate.is_absolute() else (path.parent / candidate).resolve()
parts = [
    part for part in parts
    if compose_path(part) != sidecar_path
]
for required in ("docker-compose.yml", "docker-compose.okengine-image.yml"):
    if required not in parts:
        parts.append(required)
values["COMPOSE_FILE"] = "COMPOSE_FILE=" + ":".join(parts)
out = []
seen = set()
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0]
        if key == "OKENGINE_TESTED_DIGESTS" and not tested_list:
            continue
        if key in values:
            if key not in seen:
                out.append(values[key]); seen.add(key)
            continue
    out.append(line)
for key in ("OKENGINE_GATEWAY_IMAGE", "OKENGINE_ENGINE_IMAGE_TAG", "COMPOSE_FILE",
            "OKENGINE_TESTED_DIGESTS"):
    if key in values and key not in seen:
        out.append(values[key])
path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY
export OKENGINE_GATEWAY_IMAGE="$gateway_image"
echo "    gateway image: $gateway_image (pinned in .env + docker-compose.okengine-image.yml)"
# Pin every engine-built service this deployment defines (from every file in its COMPOSE_FILE) to
# the scoped tag. Existing packs keep `image: okengine-reader` in their own docker-compose.yml; this
# override is what moves them, on their next deploy, with no edit to the pack's file.
if ! engine_override="$("$PYTHON" "$ENGINE_DIR/scripts/engine_images.py" override "$PACK")"; then
    echo "ERROR: cannot read this deployment's compose files to pin its engine images" >&2
    exit 1
fi
printf '%s' "$engine_override" >>"$gateway_override"
export OKENGINE_ENGINE_IMAGE_TAG="$engine_image_tag"
echo "    engine images: <repo>:$engine_image_tag ($(printf '%s' "$engine_override" | grep -c 'image:') service(s) pinned)"

if [ -n "$RELEASE_DIGESTS" ]; then
    # okengine#1048: load the CI-tested images by digest and verify every config ID BEFORE any local
    # tag moves; a FAIL stops here, before compose touches a container.
    # The loader verifies a staged copy beside the durable path, so the bytes persisted are the
    # bytes verified; the rename below is atomic, and a failure removes the stage (the trap).
    echo "==> [3/6] load CI-tested images ($RELEASE_DIGESTS)"
    mkdir -p "$(dirname "$tested_list")"
    _tested_stage="$tested_list.staged.$$"
    cp "$RELEASE_DIGESTS" "$_tested_stage"
    if ! "$PYTHON" "$ENGINE_DIR/scripts/release_load.py" "$_tested_stage" --pack "$PACK" \
            --engine-dir "$ENGINE_DIR" --gateway-image "$gateway_image" --engine-tag "$engine_image_tag"; then
        echo "ERROR: --release-digests: the tested images could not be loaded and verified; nothing was deployed" >&2
        exit 1
    fi
    mv -f "$_tested_stage" "$tested_list"
    _tested_stage=""
    # Loaded, verified and tagged: the new pins now name images that exist. Keep them.
    trap - EXIT
    rm -rf "$_pin_snapshot"
    _pin_snapshot=""
    export OKENGINE_TESTED_DIGESTS="$tested_list" OKENGINE_REQUIRE_TESTED_DIGESTS=1
fi
img_sha="$(docker image inspect -f '{{ index .Config.Labels "org.okengine.git_sha" }}' "$gateway_image" 2>/dev/null || echo)"
if [ -n "$RELEASE_DIGESTS" ]; then
    :   # step 3 above: nothing is built
elif [ "$SKIP_BUILD" = 0 ]; then
    if [ "$REBUILD" = 1 ]; then
        echo "==> [3/6] build gateway image (--rebuild)"
        OKENGINE_IMAGE="$gateway_repository" OKENGINE_TAG="$gateway_tag" TAG_LATEST=0 bash "$ENGINE_DIR/scripts/build-engine-image.sh"
    elif ! docker image inspect "$gateway_image" >/dev/null 2>&1; then
        echo "==> [3/6] build gateway image (none present)"
        OKENGINE_IMAGE="$gateway_repository" OKENGINE_TAG="$gateway_tag" TAG_LATEST=0 bash "$ENGINE_DIR/scripts/build-engine-image.sh"
    elif [ "$ENGINE_DIRTY" = 1 ]; then
        echo "==> [3/6] engine tree has UNCOMMITTED changes — rebuilding so the image bakes current source (a HEAD-sha match would otherwise run stale code)"
        OKENGINE_IMAGE="$gateway_repository" OKENGINE_TAG="$gateway_tag" TAG_LATEST=0 bash "$ENGINE_DIR/scripts/build-engine-image.sh"
    elif [ "$img_sha" != "$cur_sha" ]; then
        echo "==> [3/6] gateway image is STALE (image sha='${img_sha:-none/unlabeled}' != engine sha '$cur_sha') — rebuilding"
        OKENGINE_IMAGE="$gateway_repository" OKENGINE_TAG="$gateway_tag" TAG_LATEST=0 bash "$ENGINE_DIR/scripts/build-engine-image.sh"
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

# 4. bring up the containers (from the pack dir, where docker-compose.yml lives). Use --build so
#    the SIBLING images (reader/mcp/cockpit/review-write/operation-runner/projection — the services
#    with a compose `build:`) are rebuilt when their COPY'd source changed. Plain `up -d` builds an
#    image only when ABSENT, so after the first deploy those froze and shipped stale baked code
#    silently — the gateway (built separately by build-engine-image.sh, step 3) has no `build:`
#    here, so --build never touches it (okengine#178). --build uses the layer cache, so an
#    unchanged image is a fast no-op. It writes only this deployment's scoped tag
#    (OKENGINE_ENGINE_IMAGE_TAG above), never a tag another pack runs (okengine#1033).
# okengine#993 R2: the reader takes OKENGINE_EDITING by compose INTERPOLATION
# (`OKENGINE_EDITING=${OKENGINE_EDITING:-}`), where this process's environment beats .env, while
# the gateway takes it from env_file (.env wins) and ensure-runtime (step 2) follows .env. A shell
# OKENGINE_EDITING=1 over an .env =0 showed the reader editable on a read-only deployment. Pin the
# compose environment to the .env value, so every later `compose up` here interpolates what the
# gateway got. When .env does not set it, ensure-runtime follows the process value and so does the
# reader: leave it. A bare `docker compose up` run by hand outside deploy.sh is not covered.
# --- reader editing pin: begin
if editing_env="$(ENGINE_DIR="$ENGINE_DIR" ENVF="$PACK/.env" python3 -c '
import os, sys
sys.path.insert(0, os.path.join(os.environ["ENGINE_DIR"], "scripts", "cron"))
from dotenv_lib import read_compose_env
value = read_compose_env(os.environ["ENVF"], os.environ).get("OKENGINE_EDITING")
print("" if value is None else "=" + value, end="")')"; then
    if [ -n "$editing_env" ]; then
        if [ "${OKENGINE_EDITING+set}" = set ] && [ "$OKENGINE_EDITING" != "${editing_env#=}" ]; then
            echo "==> OKENGINE_EDITING='$OKENGINE_EDITING' in this shell is overridden by .env ('${editing_env#=}') for compose, so the reader agrees with the gateway"
        fi
        export OKENGINE_EDITING="${editing_env#=}"
    fi
else
    echo "WARNING: could not resolve OKENGINE_EDITING from $PACK/.env; the reader's read-only indicator follows this shell's value" >&2
fi
# --- reader editing pin: end
# Host ports (okengine#1044 [25]): every pack defaults to port_offset 0 and the cockpit is reader+1,
# so two packs (or another stack on the base ports) can want one host port. Each validates clean
# alone; `compose up` would then fail part-way with `port is already allocated`, after recreating
# the gateway. Refuse before anything is recreated; an unanswerable question warns and continues.
# engine_images.py runs `compose config` with the pack as its cwd, like every compose call here,
# so the check holds whatever directory deploy.sh was started from.
ports_rc=0
"$PYTHON" "$ENGINE_DIR/scripts/engine_images.py" ports "$PACK" || ports_rc=$?
case "$ports_rc" in
    0) ;;
    2) echo "WARNING: could not check this deployment's host ports against other deployments (undetectable, not a pass)" >&2 ;;
    *) echo "ERROR: refusing to bring up $PACK on host ports another deployment publishes" >&2; exit 1 ;;
esac
if [ -n "$RELEASE_DIGESTS" ]; then echo "==> [4/6] docker compose up -d --no-build"; else echo "==> [4/6] docker compose up -d --build"; fi
# OKENGINE_ENGINE_BUILD_TAG is exported for this build only, never written to .env: it stamps the
# provenance label the override declares, so an image a later bare `compose up` rebuilds from
# whatever ENGINE_DIR holds then is labeled `unattributed` and `engine_images.py drift` names it
# (okengine#1042 [14]).
if [ -n "$RELEASE_DIGESTS" ]; then
    # --no-build: the loaded, verified images are the deployment; a missing one fails rather than
    # being rebuilt from the host tree.
    ( cd "$PACK" && ENGINE_DIR="$ENGINE_DIR" docker compose up -d --no-build )
else
    ( cd "$PACK" && ENGINE_DIR="$ENGINE_DIR" OKENGINE_ENGINE_BUILD_TAG="$engine_image_tag" \
        docker compose up -d --build )
fi

# 4b. config.yaml is read ONCE at gateway start and lives on a BIND MOUNT, so a content edit is
#     invisible to `docker compose up` (compose recreates on compose/env-var changes, never on a
#     bind-mounted file's content) — the old config keeps running until an unrelated recreate weeks
#     later, when the change appears spontaneously (invariant-audit #10). ensure-runtime (step 2)
#     re-seds config.yaml on every run, so this is the common case. Force-recreate the gateway when
#     config.yaml is newer than the running container's start time — the cheap discriminator. A fresh
#     deploy just started the container (StartedAt > mtime), so this is a no-op there.
CFG_FILE="$PACK/.hermes-data/config.yaml"
if [ -f "$CFG_FILE" ]; then
    gw_cid="$(cd "$PACK" && docker compose ps -q gateway 2>/dev/null | head -1)"
    gw_started="$(docker inspect "$gw_cid" --format '{{.State.StartedAt}}' 2>/dev/null || echo)"
    gw_epoch=0; [ -n "$gw_started" ] && gw_epoch="$(date -d "$gw_started" +%s 2>/dev/null || echo 0)"
    cfg_epoch="$(stat -c %Y "$CFG_FILE" 2>/dev/null || echo 0)"
    if [ "$gw_epoch" -gt 0 ] 2>/dev/null && [ "$cfg_epoch" -gt "$gw_epoch" ] 2>/dev/null; then
        echo "==> [4b/6] config.yaml is newer than the running gateway ($(( cfg_epoch - gw_epoch ))s) — force-recreating so the new config actually loads"
        ( cd "$PACK" && ENGINE_DIR="$ENGINE_DIR" docker compose up -d --force-recreate --no-deps gateway )
    fi
fi

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
# A journal-fed projection can legitimately predate files restored or moved with
# preserved mtimes. Reconcile once at deploy so the health gate compares the live
# corpus with a complete, current projection rather than waiting for its cadence.
# Best effort (okengine#853): a full scan of a vault that lanes are writing to can lose the race
# with them ("canonical corpus changed during projection reconciliation") and exit non-zero. Under
# `set -e` that aborted the deploy with 1 -- "bring-up failed" -- on a stack that was up and
# healthy. The projector refreshes incrementally on every corpus change (#851), and
# post_deploy_verify judges projection health itself, so a real projector fault still surfaces as
# 3 rather than being hidden here.
if ( cd "$PACK" && docker compose config --services 2>/dev/null | grep -Fxq okengine-projection ); then
    echo "    reconciling PostgreSQL projection against the complete live corpus"
    if ! ( cd "$PACK" && docker compose exec -T okengine-projection python /app/service.py --reconcile ); then
        echo "    WARN: projection reconcile did not complete (corpus busy or projector unavailable) —" \
             "the projector's incremental refresh catches up; verification below judges its health"
    fi
fi
# Give the reader/MCP a moment to bind before probing. Overridable so the behavioural tests in
# tests/test_deploy_exit_contract.py do not pay it on every run (twelve runs x 5s dominated the
# whole file); the default is unchanged for every real deploy.
sleep "${OKENGINE_VERIFY_DELAY:-5}"
VERIFY_RC=0
if ( cd "$PACK" && bash "$ENGINE_DIR/scripts/post_deploy_verify.sh" ); then
    echo "==> done — deployment verified healthy."
else
    # okengine#597: report the third state instead of collapsing it into success. Aborting the
    # bring-up here would still be wrong — the stack is already up, the gateway may only be
    # binding, and the remediation above is what the operator needs. But exit status is ALL a
    # scripted caller sees, and `exit 0` told a fleet roll, a CI job, or an agent chaining
    # `deploy.sh && <next>` that a deployment with failing checks was a working one. Both paths
    # print `==> done` and differ only in the sentence after it, which no script reads.
    #
    # 0 = up and verified · 3 = up, checks reported issues · other non-zero = bring-up failed.
    VERIFY_RC=3
    echo "==> done — bring-up complete, but post-deploy checks reported issues (see remediation above)."
    echo "    re-verify any time:  ( cd $PACK && bash $ENGINE_DIR/scripts/post_deploy_verify.sh )"
fi
echo "    LLM cron output + delivery appear once a model key (and a delivery channel, if used) are set in .env."

# --kickstart: populate the vault now instead of waiting for the schedule (opt-in; the compile
# + brief stages spend on the model). Needs the crons deployed, so it's skipped with --no-crons.
if [ "$KICKSTART" = 1 ]; then
    if [ "$NO_CRONS" = 1 ]; then
        echo "==> --kickstart skipped: no crons were deployed (--no-crons)." >&2
    else
        CRON_PACK_DIR="$PACK" HERMES_UID="$HERMES_UID" bash "$ENGINE_DIR/scripts/kickstart.sh" "$PACK"
    fi
fi

# Carry the verification verdict out as the script's status. Without an explicit exit the status is
# whatever ran last — an `echo`, or the kickstart block — which is how the verdict was lost.
exit "$VERIFY_RC"
