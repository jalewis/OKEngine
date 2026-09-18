#!/usr/bin/env bash
# Apply the okf-engine carried patches to a pinned Hermes checkout.
#
# These are OUR patches against core Hermes files, re-applied on each Hermes
# version bump (we do not upstream them). Pinned version below.
#
# Usage:  patches/apply.sh [HERMES_DIR]      (default: $HERMES_DIR, else cwd)
# Idempotent: already-applied patches are skipped. Exits non-zero on drift.
set -euo pipefail

HERMES="${1:-${HERMES_DIR:-$PWD}}"
PATCHROOT="$(cd "$(dirname "$0")" && pwd)"
# Read the Hermes pin from engine-manifest.yaml (the single source of truth), NOT a hardcoded literal
# that silently drifts from the manifest on a bump — the exact class the cron-plus pin already binds
# by test (invariant-audit #6). The advisory check below just reports the pin these patches target.
PIN="$(grep -E '^[[:space:]]*pinned_tag:' "$PATCHROOT/../engine-manifest.yaml" 2>/dev/null | awk '{print $2}' | head -1)"
PIN="${PIN:-unknown}"

case "$PIN" in
  v2026.7.7.2) PATCHDIR="$PATCHROOT"; REGISTRY="$PATCHROOT/README.md"; STRICT_OFFSET=1 ;;
  v2026.9.14) PATCHDIR="$PATCHROOT/target-v2026.9.14"; REGISTRY="$PATCHDIR/inventory.json"; STRICT_OFFSET=0 ;;
  *) echo "ERROR: no governed Hermes patch set for manifest pin $PIN" >&2; exit 3 ;;
esac

cd "$HERMES"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "ERROR: $HERMES is not a git checkout of Hermes" >&2; exit 1; }

cur="$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)"
echo "Hermes checkout: $cur   (patches cut against pin: $PIN)"
[ "$cur" = "$PIN" ] || echo "  ⚠ not at the pinned version — patches may need a rebase if they fail."

# GUARD (okengine#178): the loop below globs whatever [0-9]*.patch files EXIST and exits 0 with a
# cosmetic count — so a patch dropped by a bad rebase / a digit-dropping rename would silently
# reduce the applied set and bake a Hermes image MISSING a carried guard (e.g. the OKF write-guard),
# with no failure. `git apply` can't catch a MISSING patch (there's nothing to reject). So assert
# every patch the README registers is present on disk before we apply anything.
expected="$(grep -oE '[0-9]{2}-[a-z0-9][a-z0-9-]*\.patch' "$REGISTRY" 2>/dev/null | sort -u)"
[ -n "$expected" ] || { echo "ERROR: could not read the patch registry from $REGISTRY" >&2; exit 3; }
for e in $expected; do
  [ -f "$PATCHDIR/$e" ] || { echo "ERROR: README registers '$e' but the file is MISSING — a carried patch was dropped (bad rebase/rename?). Refusing to build a partially-patched Hermes." >&2; exit 3; }
done

applied=0 skipped=0
for p in "$PATCHDIR"/[0-9]*.patch; do
  n="$(basename "$p")"
  reverse_rc=0
  reverse_output="$(git apply --reverse --check --verbose "$p" 2>&1)" || reverse_rc=$?
  if [ "$reverse_rc" -eq 0 ] && printf '%s\n' "$reverse_output" | grep -qi 'fuzz'; then
    echo "  ✗ reverse check uses fuzz: $n — patch placement is ambiguous; rebase or repair the checkout." >&2
    exit 2
  fi
  if [ "$reverse_rc" -eq 0 ] && [ "$STRICT_OFFSET" -eq 1 ] && printf '%s\n' "$reverse_output" | grep -qi 'offset'; then
    echo "  ✗ reverse check relocates a hunk: $n — already-applied placement is not exact; rebase or repair the checkout." >&2
    exit 2
  fi
  if [ "$reverse_rc" -eq 0 ]; then
    echo "  • already applied: $n"; skipped=$((skipped+1)); continue
  fi
  check_rc=0
  check_output="$(git apply --check --verbose "$p" 2>&1)" || check_rc=$?
  if [ "$check_rc" -eq 0 ] && printf '%s\n' "$check_output" | grep -qi 'fuzz'; then
    echo "  ✗ uses fuzz: $n — patch placement is ambiguous; rebase this patch." >&2
    exit 2
  fi
  # The v0.21.3 artifacts are a governed sequential series. Earlier artifacts
  # intentionally move later line coordinates, so offsets are expected there;
  # fuzz remains forbidden. The legacy independent set retains exact offsets.
  if [ "$check_rc" -eq 0 ] && [ "$STRICT_OFFSET" -eq 1 ] && printf '%s\n' "$check_output" | grep -qi 'offset'; then
    echo "  ✗ relocates a hunk: $n — exact patch placement is not proven; rebase this patch." >&2
    exit 2
  fi
  if [ "$check_rc" -eq 0 ]; then
    git apply "$p"; echo "  ✓ applied: $n"; applied=$((applied+1))
  else
    echo "  ✗ does NOT apply: $n — Hermes drift from $PIN; rebase this patch." >&2; exit 2
  fi
done
echo "done: $applied applied, $skipped already-present"
