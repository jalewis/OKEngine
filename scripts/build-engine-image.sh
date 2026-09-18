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
#   bash scripts/build-engine-image.sh              # clone Hermes, build immutable release+sha tag
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
# The engine overlay COPYs the WORKING TREE (below), so a dirty tree bakes uncommitted edits. Reflect
# that in the provenance label ("X-dirty") so deploy.sh's staleness gate never trusts a dirty-built
# image as clean-commit-X on a later clean checkout (invariant-audit #9 label poisoning). Mirrors the
# "X-dirty" sha deploy.sh computes, so a dirty build + clean re-deploy at X correctly rebuilds.
[ -n "$(git -C "$ENGINE_DIR" status --porcelain 2>/dev/null)" ] && ENG_SHA="${ENG_SHA}-dirty"
HERMES_REPO="${HERMES_REPO:-https://github.com/NousResearch/hermes-agent.git}"
IMAGE="${OKENGINE_IMAGE:-hermes-agent}"
# Image identity is immutable by default (#627). A release-only tag still aliases every commit
# made between releases, which recreates the same cross-deployment hazard as :latest. Include the
# exact engine revision so two packs on one host can remain pinned to different engine builds.
TAG="${OKENGINE_TAG:-okengine-$RELEASE-$ENG_SHA}"

echo "==> OKEngine gateway image build"
echo "    engine : $ENGINE_DIR"
echo "    Hermes : $HERMES_REPO @ $PIN"
echo "    image  : $IMAGE:$TAG"

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
  # HEAD-sha alone is not integrity: a reused HERMES_SRC checkout can sit AT the pinned commit yet
  # carry uncommitted local edits (Docker's `COPY . .` bakes the working tree, not HEAD), so a dirty
  # sibling checkout would ship as a "verified" image. Refuse a dirty tree (invariant-audit B7.4).
  # A fresh clone is clean by construction, so this only ever bites a reused checkout.
  # NB: intentionally NOT `--ignored` — that would flag every normal gitignored artifact (__pycache__,
  # *.pyc) and false-fail routine builds. Residual (B7.4 re-verify, low): a gitignored file in a
  # Hermes-only path OUTSIDE the overlay-clobbered trees can still be COPY'd unless .dockerignore
  # excludes it; ordinary modified/untracked edits — the real risk — are caught here.
  if [ -n "$(git -C "$WORK" status --porcelain 2>/dev/null)" ]; then
    echo "ERROR: Hermes source at $WORK is at the pinned commit but has UNCOMMITTED changes —" >&2
    echo "       building would bake local edits into a supposedly-verified image. Clean the tree" >&2
    echo "       (git -C '$WORK' stash) or unset HERMES_SRC to build from a fresh clone." >&2
    exit 1
  fi
  echo "==> verified Hermes @ $PINNED_SHA (working tree clean)"
else
  echo "WARNING: engine-manifest.yaml has no pinned_sha — skipping commit verification" >&2
fi

# 2. Carried patches (idempotent).
echo "==> applying carried patches"
bash "$ENGINE_DIR/patches/apply.sh" "$WORK"

# Hermes installs its already-synced project editable with `--no-deps`, but uv
# still creates an isolated build environment and resolves setuptools from
# PyPI.  That makes an otherwise self-contained image rebuild depend on live
# DNS/PyPI.  Reuse the build backend already installed by the preceding
# dependency-sync layer.  Fail loudly if upstream changes the instruction so
# this supply-chain hardening cannot silently disappear.
EDITABLE_INSTALL='uv pip install --no-cache-dir --no-deps -e "."'
if ! grep -Fq "$EDITABLE_INSTALL" "$WORK/Dockerfile"; then
  echo "ERROR: Hermes Dockerfile editable-install instruction changed; cannot enforce offline build isolation" >&2
  exit 1
fi
SETUPTOOLS_WHEEL="$ENGINE_DIR/vendor/python-build/setuptools-82.0.1-py3-none-any.whl"
SETUPTOOLS_SHA256="a59e362652f08dcd477c78bb6e7bd9d80a7995bc73ce773050228a348ce2e5bb"
printf '%s  %s\n' "$SETUPTOOLS_SHA256" "$SETUPTOOLS_WHEEL" | sha256sum -c - >/dev/null
mkdir -p "$WORK/.okengine-build"
install -m 0644 "$SETUPTOOLS_WHEEL" "$WORK/.okengine-build/"
sed -i 's@uv pip install --no-cache-dir --no-deps -e "\."@uv pip install --no-cache-dir --no-deps ./.okengine-build/setuptools-82.0.1-py3-none-any.whl \&\& uv pip install --no-cache-dir --no-deps --no-build-isolation -e "."@' "$WORK/Dockerfile"

# Build the engine package once for this gateway revision, bake that exact wheel
# into the immutable image context, and install it after Hermes. The gateway is
# therefore a wheel consumer just like every engine-owned sidecar without
# performing a mutable install at container startup.
python3 "$ENGINE_DIR/scripts/build_engine_wheel.py" --out "$WORK/.okengine-build"
OKENGINE_WHEEL="$(find "$WORK/.okengine-build" -maxdepth 1 -name 'okengine-*.whl' -print -quit)"
[ -n "$OKENGINE_WHEEL" ] || { echo "ERROR: OKEngine wheel build produced no artifact" >&2; exit 1; }
printf '\nRUN uv pip install --no-cache-dir --no-deps /opt/hermes/.okengine-build/%s\n' \
  "$(basename "$OKENGINE_WHEEL")" >> "$WORK/Dockerfile"

# Governed MCP writes run from the vault and therefore import policy_plane from
# the installed wheel, not from the source overlay. Give both locations the
# same explicit catalog path so the wheel cannot derive a nonexistent
# site-packages/config/policy/catalog.yaml path.
printf '\nENV OKENGINE_POLICY_CATALOG=/opt/hermes/config/policy/catalog.yaml\n' >> "$WORK/Dockerfile"

# 3. Overlay the engine layer into the Hermes tree (merge — Hermes' COPY . . bakes
#    it into /opt/hermes). Keep in sync with engine-manifest.yaml engine_layer.
echo "==> overlaying engine layer"
install -m 0644 "$ENGINE_DIR/tools/schema_validator.py" "$WORK/tools/schema_validator.py"
install -m 0644 "$ENGINE_DIR/tools/policy_plane.py" "$WORK/tools/policy_plane.py"
rm -rf "$WORK/okengine-mcp" "$WORK/okengine-reader"
cp -r "$ENGINE_DIR/okengine-mcp"    "$WORK/okengine-mcp"
cp -r "$ENGINE_DIR/okengine-reader" "$WORK/okengine-reader"
# These write-path libraries are installed from the revision wheel above. Do
# not leave service-local copies that shadow the wheel according to script-dir
# import precedence and recreate baked-vs-staged resolution ambiguity.
rm -f "$WORK/okengine-mcp/scope.py" "$WORK/okengine-mcp/projection.py" \
      "$WORK/okengine-mcp/output_contract_enforce.py" "$WORK/okengine-mcp/converge.py"
mkdir -p "$WORK/scripts" "$WORK/config" "$WORK/plugins/model-providers"
cp -r "$ENGINE_DIR/scripts/." "$WORK/scripts/"
cp -r "$ENGINE_DIR/config/."  "$WORK/config/"
install -m 0644 "$ENGINE_DIR/engine-manifest.yaml" "$WORK/engine-manifest.yaml"
# Exact-pin overlay set: target v0.21.3 must not bake the old custom/OpenRouter
# profiles, which would replace upstream routing and reasoning safeguards.
PLUGIN_OVERLAY_ROOT="$(bash "$ENGINE_DIR/scripts/select_hermes_plugin_overlays.sh" "$ENGINE_DIR" "$PIN")"
cp -r "$PLUGIN_OVERLAY_ROOT/model-providers/custom"     "$WORK/plugins/model-providers/"
if [ "$PIN" = "v2026.9.14" ]; then
  # The target's native OpenRouter profile already contains every carried
  # behavior plus new affinity/reasoning/speed-tier safeguards. Keep it intact.
  bash "$ENGINE_DIR/scripts/verify_native_openrouter.sh" "$WORK"
else
  cp -r "$PLUGIN_OVERLAY_ROOT/model-providers/openrouter" "$WORK/plugins/model-providers/"
fi
# web-search provider overlay: Serper (okengine#190) — a backend Hermes doesn't ship, added as a
# plugin (addition, not a fork). Auto-loads via kind: backend alongside the bundled web providers.
mkdir -p "$WORK/plugins/web"
cp -r "$PLUGIN_OVERLAY_ROOT/web/serper" "$WORK/plugins/web/serper"
# Bake the RUNNING engine version into the image (okengine#192) so deployment_validate can compare
# it to the deployment's runtime stamp and self-heal an image-roll that skipped the re-stamp — the
# About panel then never reports a version the deployment isn't running.
printf '%s\n' "$RELEASE" > "$WORK/.okengine_release"
# Same for the HERMES pin (the #192 second half): without a baked marker the stamp's hermes_pin is
# unvalidatable, so a Hermes-bump canary roll left About claiming the OLD Hermes with nothing
# catching it (found live on the v0.18.2 canary). check_pins compares + self-heals from this.
printf '%s\n' "$PIN" > "$WORK/.hermes_pin"
# drop any __pycache__ that hitched along
find "$WORK/okengine-mcp" "$WORK/okengine-reader" "$WORK/scripts" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

# Never bake a GITIGNORED artifact (okengine#511). The overlay above copies the WORKING
# TREE, but ENG_SHA's dirty check is `git status --porcelain`, which does NOT report
# ignored files. So a checkout holding generated artifacts built as "clean at commit X"
# while shipping content that is in no commit at all: config/cron-plus-jobs.json (a
# cron_pack_split.py output, .gitignore:20) was baked carrying 132 jobs, 79 of them
# pack-specific, into a domain-agnostic engine image — and a build of the SAME commit from
# a different checkout baked a 53-job version of the same path. Two images, identical clean
# provenance, different contents.
#
# Tracked-but-modified files are still baked deliberately: that is the local iteration
# path, and ENG_SHA already labels it "X-dirty". Only ignored paths are dropped, and only
# under the trees this overlay actually copied — $WORK is a Hermes checkout, and blindly
# removing every engine-ignored path could delete an unrelated Hermes file that happens to
# share a name (its own .venv, for instance).
# --- BEGIN ignored-artifact sweep (okengine#511) --- extracted verbatim by
# tests/test_build_image_excludes_ignored.py; keep the sentinels.
overlaid_prefixes='okengine-mcp/ okengine-reader/ scripts/ config/ plugins/ tools/'
dropped_ignored=0
while IFS= read -r ignored; do
  [ -n "$ignored" ] || continue
  for prefix in $overlaid_prefixes; do
    case "$ignored" in
      "$prefix"*)
        if [ -e "$WORK/$ignored" ]; then
          rm -rf -- "${WORK:?}/$ignored"
          dropped_ignored=$((dropped_ignored + 1))
          echo "    dropped gitignored artifact: $ignored"
        fi
        break
        ;;
    esac
  done
done <<EOF
$(git -C "$ENGINE_DIR" ls-files --others --ignored --exclude-standard 2>/dev/null || true)
EOF
if [ "$dropped_ignored" -gt 0 ]; then
  echo "==> dropped $dropped_ignored gitignored artifact(s) from the overlay (okengine#511)"
fi
# --- END ignored-artifact sweep ---

if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "==> SKIP_BUILD=1 — assembled tree at $WORK (not building)"
  exit 0
fi

# 4. Build the gateway image via Hermes' own Dockerfile.
#    TAG_LATEST=1 is an explicit compatibility escape hatch only. Deployments never consume it;
#    moving :latest by default makes an unrelated pack change revision on its next recreate.
LATEST_ARGS=()
[ "${TAG_LATEST:-0}" = "1" ] && LATEST_ARGS=(-t "$IMAGE:latest")
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
