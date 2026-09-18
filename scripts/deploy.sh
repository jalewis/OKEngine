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
PACK="$PWD"
REBUILD=0; SKIP_BUILD=0; SKIP_VALIDATE=0; NO_CRONS=0; FIX_PERMS=0; KICKSTART=0; NO_UPGRADE=0
for a in "$@"; do
    case "$a" in
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
_env_uid=""; _env_gid=""
if [ -f "$PACK/.env" ]; then
    # `|| true`: an .env with no pinned uid (the normal fresh-install shape — .env.example
    # doesn't pin one) makes grep exit 1, and under `set -euo pipefail` a bare no-match
    # substitution kills the whole deploy INSTANTLY and SILENTLY (5th instance of this class;
    # caught by the paste-block clean-host test — every prior deploy had HERMES_UID pinned).
    _env_uid="$(grep -oE '^HERMES_UID=[0-9]+' "$PACK/.env" | cut -d= -f2 | head -1 || true)"
    _env_gid="$(grep -oE '^HERMES_GID=[0-9]+' "$PACK/.env" | cut -d= -f2 | head -1 || true)"
fi
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
if ! grep -qE '^HERMES_UID=' "$PACK/.env"; then
    printf '\n# uid:gid the gateway remaps to; pinned by deploy.sh so bare docker-compose ops match\n# the runtime tree owner (else compose defaults to 10000 and the scheduler dies on a perm error).\nHERMES_UID=%s\nHERMES_GID=%s\n' "$HERMES_UID" "$HERMES_GID" >> "$PACK/.env"
    echo "    pinned HERMES_UID:GID=$HERMES_UID:$HERMES_GID -> .env (matches the runtime tree owner)"
fi

# 0. reconcile the engine pin. A pack that pins an OLDER engine release than the one running this
#    deploy is brought CURRENT via `framework upgrade` (bumps engine.version + runs any registered
#    migrations in (pin, target] under a roll-forward gate that AUTO-ROLLS-BACK on failure) BEFORE the
#    validate gate — so bumping the engine doesn't dead-end every lagging pack's deploy at the
#    "incompatible release series" FAIL / --skip-validate (okengine#359). Exit codes: 0 = reconciled
#    or already current; non-zero = an AHEAD pin (downgrade refused) or an upgrade that rolled back —
#    in both cases the pin is unchanged and the validate gate below surfaces the real, actionable
#    mismatch. Opt out with --no-upgrade.
if [ "$NO_UPGRADE" = 0 ] && [ -f "$PACK/engine.version" ]; then
    echo "==> [0/6] reconcile engine pin (framework upgrade)"
    "$PYTHON" "$ENGINE_DIR/scripts/framework.py" upgrade "$PACK" --apply \
        || echo "    note: pin not reconciled (ahead pin, or upgrade rolled back) — validate will report it" >&2
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

# Generate the authoritative fleet before runtime reconciliation so every contracted
# lane's dedicated MCP server exists when the gateway first starts.
CRON_PACK_DIR="$PACK" "$PYTHON" "$ENGINE_DIR/scripts/cron_pack_split.py" regen

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

# 3. gateway image — build if missing OR stale (label != current checkout), or forced.
#
# First: is this tree fit to be built from AT ALL? The staleness gate below compares the ENGINE_DIR
# checkout against the image, which answers "is the image current" and says nothing about "is this
# checkout the code we mean to ship". On 2026-08-15 a deployment's ENGINE_DIR pointed at a working
# tree on a feature branch behind main; building there would have shipped a cockpit missing a fix
# that was live and serving, and the build, the container health check and the rendered page would
# all have reported success. Refuse rather than warn — every downstream signal is green either way,
# so a warning here is a warning nobody has any reason to read.
"$PYTHON" "$ENGINE_DIR/scripts/engine_source_guard.py" "$ENGINE_DIR"

cur_sha="$(git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
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
gateway_image="${OKENGINE_GATEWAY_IMAGE:-${gateway_repository}:${gateway_tag}}"
if [ -n "${OKENGINE_GATEWAY_IMAGE:-}" ] && [ "$SKIP_BUILD" = 0 ]; then
    echo "ERROR: OKENGINE_GATEWAY_IMAGE selects a prebuilt image; use --skip-build or set OKENGINE_IMAGE to select the build repository" >&2
    exit 1
fi

# Persist the exact image and the engine-managed override in the deployment itself. This protects
# later bare `docker compose up` calls too; an environment export that exists only for this deploy
# would leave the old cross-deployment :latest hazard intact. Preserve unrelated COMPOSE_FILE
# entries while ensuring the override is present exactly once.
gateway_override="$PACK/docker-compose.okengine-image.yml"
cat >"$gateway_override" <<'EOF'
# Generated by OKEngine deploy.sh. Do not hand-edit; regenerated on every deploy.
services:
  gateway:
    image: ${OKENGINE_GATEWAY_IMAGE:?deploy.sh must pin an immutable gateway image}
    # Engine-owned availability contract. Keep this in the deploy override as well as the
    # scaffold: existing packs must gain new fail-loud health semantics on their next deploy.
    healthcheck:
      test: ["CMD-SHELL", "now=$$(date +%s); tick=$$(stat -c %Y /opt/data/cron-plus/.tick.lock 2>/dev/null) || exit 1; [ $$((now-tick)) -le 180 ] || exit 1; [ ! -s /opt/data/cron-plus/.scheduler-stalled ] || exit 1; owner=/opt/vault/.okengine/corpus/lock-owner.json; [ ! -e $$owner ] || { mtime=$$(stat -c %Y $$owner) || exit 1; [ $$((now-mtime)) -le 300 ] || exit 1; }; gen=$$(( $$(awk '/^btime/{print $$2}' /proc/stat) + $$(sed 's/.*) //' /proc/1/stat | awk '{print $$20}') / $$(getconf CLK_TCK) )); ! find /opt/data/cron-plus/runs -type f -mmin +60 -newermt @$$gen -exec grep -lE '\"status\"[[:space:]]*:[[:space:]]*\"running\"' {} + 2>/dev/null | grep -q . || exit 1; for stat in /proc/[0-9]*/stat; do grep -q ') D ' $$stat 2>/dev/null && exit 1; done; exit 0"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 180s
EOF
python3 - "$PACK/.env" "$gateway_image" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
image = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
values = {}
order = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0]
        values[key] = line
        order.append(key)
values["OKENGINE_GATEWAY_IMAGE"] = f"OKENGINE_GATEWAY_IMAGE={image}"
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
        if key in values:
            if key not in seen:
                out.append(values[key]); seen.add(key)
            continue
    out.append(line)
for key in ("OKENGINE_GATEWAY_IMAGE", "COMPOSE_FILE"):
    if key not in seen:
        out.append(values[key])
path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY
export OKENGINE_GATEWAY_IMAGE="$gateway_image"
echo "    gateway image: $gateway_image (pinned in .env + docker-compose.okengine-image.yml)"

img_sha="$(docker image inspect -f '{{ index .Config.Labels "org.okengine.git_sha" }}' "$gateway_image" 2>/dev/null || echo)"
if [ "$SKIP_BUILD" = 0 ]; then
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
#    the SIBLING images (okengine-reader/-mcp/-cockpit — the only services with a compose `build:`)
#    are rebuilt when their COPY'd source changed. Plain `up -d` builds an image only when ABSENT,
#    so after the first deploy those three froze and shipped stale baked code silently — the gateway
#    (built separately by build-engine-image.sh, step 3) has no `build:` here, so --build never
#    touches it (okengine#178). --build uses the layer cache, so an unchanged image is a fast no-op.
echo "==> [4/6] docker compose up -d --build"
( cd "$PACK" && ENGINE_DIR="$ENGINE_DIR" docker compose up -d --build )

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
if ( cd "$PACK" && docker compose config --services 2>/dev/null | grep -Fxq okengine-projection ); then
    echo "    reconciling PostgreSQL projection against the complete live corpus"
    ( cd "$PACK" && docker compose exec -T okengine-projection python /app/service.py --reconcile )
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
