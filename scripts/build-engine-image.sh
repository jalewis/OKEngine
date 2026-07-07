#!/usr/bin/env bash
# Build the OKEngine gateway image = pinned Hermes + carried patches + engine overlay.
#
# This produces the `hermes-agent` image that a pack's docker-compose runs as its
# `gateway` service. OKEngine is an OVERLAY, not a Hermes fork, so the gateway
# image is assembled here: clone Hermes at the pin -> apply patches/ -> copy the
# engine layer into the tree -> build via Hermes' own Dockerfile (its `COPY . .`
# bakes everything to /opt/hermes, where config.yaml points the okengine-write
# MCP server: /opt/hermes/okengine-mcp/write_server.py).
#
# Usage:
#   bash scripts/build-engine-image.sh              # clone Hermes, build hermes-agent:okengine-<engine_release> + :latest
#   HERMES_SRC=/path/to/hermes bash scripts/build-engine-image.sh   # reuse a checkout (must be at the pin)
#   OKENGINE_IMAGE=myrepo/okengine OKENGINE_TAG=custom bash scripts/build-engine-image.sh
#   SKIP_BUILD=1 bash scripts/build-engine-image.sh # assemble the tree only (no docker build) — for inspection/CI
set -euo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN="${PIN:-$(awk -F': *' '/pinned_tag:/{print $2; exit}' "$ENGINE_DIR/engine-manifest.yaml" | tr -d ' ')}"
RELEASE="${RELEASE:-$(awk -F': *' '/^engine_release:/{print $2; exit}' "$ENGINE_DIR/engine-manifest.yaml" | awk '{print $1}')}"
# Fail LOUD rather than silently building against a STALE hardcoded pin or an "unknown" version stamp
# (okengine#193 shift-left, no-silent-omission): a mis-parsed / renamed engine-manifest.yaml must STOP
# the build, not guess a literal that ships a mismatched base — the old hardcoded pin fallback would
# clone a Hermes tag versions behind current. An explicit PIN/RELEASE env still wins (resolved
# above). The pinned_sha verification below is a backstop, but it's opt-out (empty pinned_sha skips it).
[ -n "$PIN" ]     || { echo "ERROR: could not read runtime.pinned_tag from $ENGINE_DIR/engine-manifest.yaml — refusing to build against a guessed pin" >&2; exit 1; }
[ -n "$RELEASE" ] || { echo "ERROR: could not read engine_release from $ENGINE_DIR/engine-manifest.yaml — refusing to bake an 'unknown' version" >&2; exit 1; }
ENG_SHA="$(git -C "$ENGINE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
HERMES_REPO="${HERMES_REPO:-https://github.com/NousResearch/hermes-agent.git}"
IMAGE="${OKENGINE_IMAGE:-hermes-agent}"
# Default tag tracks the engine release from the manifest (okengine#101) — never a hardcoded
# literal, so a default build stamps the image with the version of the source it was built from.
TAG="${OKENGINE_TAG:-okengine-$RELEASE}"

echo "==> OKEngine gateway image build"
echo "    engine : $ENGINE_DIR"
echo "    Hermes : $HERMES_REPO @ $PIN"
echo "    image  : $IMAGE:$TAG (+ :latest)"

# 1. Hermes source at the pin.
CLEAN_WORK=0
if [ -n "${HERMES_SRC:-}" ]; then
  WORK="$HERMES_SRC"
  echo "==> using existing Hermes checkout: $WORK"
  [ -f "$WORK/Dockerfile" ] || { echo "ERROR: $WORK has no Dockerfile — not a Hermes checkout"; exit 1; }
else
  WORK="$(mktemp -d)/hermes"
  CLEAN_WORK=1
  # Clean the temp clone on ANY exit — success, error, OR signal (okengine#139). The
  # old inline rm's only fired on the happy path + one error path, leaking ~160M per
  # failed/interrupted build. Guarded by CLEAN_WORK so a reused HERMES_SRC checkout is
  # never deleted (that branch never sets this trap anyway).
  trap '[ "${CLEAN_WORK:-0}" = 1 ] && rm -rf "$(dirname "$WORK")"' EXIT
  echo "==> cloning Hermes @ $PIN"
  git clone --depth 1 --branch "$PIN" "$HERMES_REPO" "$WORK"
fi

# 1b. Supply-chain integrity: verify the source is at the pinned commit, so a
#     moved/retagged upstream or a stale reused checkout can't slip in. The pin
#     lives in engine-manifest.yaml; clear pinned_sha there only to opt out.
PINNED_SHA="$(awk '/pinned_sha:/{print $2; exit}' "$ENGINE_DIR/engine-manifest.yaml")"
if [ -n "$PINNED_SHA" ]; then
  GOT="$(git -C "$WORK" rev-parse HEAD 2>/dev/null || true)"
  if [ "$GOT" != "$PINNED_SHA" ]; then
    echo "ERROR: Hermes source is at ${GOT:-unknown}, expected pinned commit $PINNED_SHA" >&2
    echo "       (tag $PIN must resolve to $PINNED_SHA — refusing to build a mismatched base)" >&2
    exit 1                                  # the EXIT trap removes the temp clone
  fi
  echo "==> verified Hermes @ $PINNED_SHA"
else
  echo "WARNING: engine-manifest.yaml has no pinned_sha — skipping commit verification" >&2
fi

# 2. Carried patches (idempotent).
echo "==> applying carried patches"
bash "$ENGINE_DIR/patches/apply.sh" "$WORK"

# 3. Overlay the engine layer into the Hermes tree (merge — Hermes' COPY . . bakes
#    it into /opt/hermes). Keep in sync with engine-manifest.yaml engine_layer.
echo "==> overlaying engine layer"
install -m 0644 "$ENGINE_DIR/tools/schema_validator.py" "$WORK/tools/schema_validator.py"
rm -rf "$WORK/okengine-mcp" "$WORK/okengine-reader"
cp -r "$ENGINE_DIR/okengine-mcp"    "$WORK/okengine-mcp"
cp -r "$ENGINE_DIR/okengine-reader" "$WORK/okengine-reader"
mkdir -p "$WORK/scripts" "$WORK/config" "$WORK/plugins/model-providers"
cp -r "$ENGINE_DIR/scripts/." "$WORK/scripts/"
cp -r "$ENGINE_DIR/config/."  "$WORK/config/"
cp -r "$ENGINE_DIR/plugins/model-providers/custom"     "$WORK/plugins/model-providers/"
cp -r "$ENGINE_DIR/plugins/model-providers/openrouter" "$WORK/plugins/model-providers/"
# web-search provider overlay: Serper (okengine#190) — a backend Hermes doesn't ship, added as a
# plugin (addition, not a fork). Auto-loads via kind: backend alongside the bundled web providers.
mkdir -p "$WORK/plugins/web"
cp -r "$ENGINE_DIR/plugins/web/serper" "$WORK/plugins/web/serper"
# Bake the RUNNING engine version into the image (okengine#192) so deployment_validate can compare
# it to the deployment's runtime stamp and self-heal an image-roll that skipped the re-stamp — the
# About panel then never reports a version the deployment isn't running.
printf '%s\n' "$RELEASE" > "$WORK/.okengine_release"
# drop any __pycache__ that hitched along
find "$WORK/okengine-mcp" "$WORK/okengine-reader" "$WORK/scripts" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "==> SKIP_BUILD=1 — assembled tree at $WORK (not building)"
  exit 0
fi

# 4. Build the gateway image via Hermes' own Dockerfile.
#    TAG_LATEST=1 (default) also tags :latest (what pack composes reference by
#    default). Set TAG_LATEST=0 to avoid moving an existing :latest in use.
LATEST_ARGS=()
[ "${TAG_LATEST:-1}" = "1" ] && LATEST_ARGS=(-t "$IMAGE:latest")
# Stamp provenance so `deploy.sh` can tell whether an existing :latest is stale
# (built from a different engine checkout) and so an operator can see what's running.
LABELS=(
  --label "org.okengine.release=$RELEASE"
  --label "org.okengine.git_sha=$ENG_SHA"
  --label "org.okengine.hermes_pin=$PIN"
)
echo "==> docker build $IMAGE:$TAG ${LATEST_ARGS[*]:-}  (release=$RELEASE sha=$ENG_SHA hermes=$PIN)"
docker build "${LABELS[@]}" -t "$IMAGE:$TAG" "${LATEST_ARGS[@]}" "$WORK"

# temp clone is removed by the EXIT trap (okengine#139)
echo "==> done: $IMAGE:$TAG${LATEST_ARGS:+ (and :latest)}. Pack docker-compose 'gateway' runs it."
