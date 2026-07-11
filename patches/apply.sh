#!/usr/bin/env bash
# Apply the okf-engine carried patches to a pinned Hermes checkout.
#
# These are OUR patches against core Hermes files, re-applied on each Hermes
# version bump (we do not upstream them). Pinned version below.
#
# Usage:  patches/apply.sh [HERMES_DIR]      (default: $HERMES_DIR, else cwd)
# Idempotent: already-applied patches are skipped. Exits non-zero on drift.
set -euo pipefail

PIN="v2026.7.7.2"   # Hermes v0.18.2 — the version these patches are cut against
HERMES="${1:-${HERMES_DIR:-$PWD}}"
PATCHDIR="$(cd "$(dirname "$0")" && pwd)"

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
expected="$(grep -oE '[0-9]{2}-[a-z0-9][a-z0-9-]*\.patch' "$PATCHDIR/README.md" 2>/dev/null | sort -u)"
[ -n "$expected" ] || { echo "ERROR: could not read the patch registry from $PATCHDIR/README.md" >&2; exit 3; }
for e in $expected; do
  [ -f "$PATCHDIR/$e" ] || { echo "ERROR: README registers '$e' but the file is MISSING — a carried patch was dropped (bad rebase/rename?). Refusing to build a partially-patched Hermes." >&2; exit 3; }
done

applied=0 skipped=0
for p in "$PATCHDIR"/[0-9]*.patch; do
  n="$(basename "$p")"
  if git apply --reverse --check "$p" >/dev/null 2>&1; then
    echo "  • already applied: $n"; skipped=$((skipped+1)); continue
  fi
  if git apply --check "$p" >/dev/null 2>&1; then
    git apply "$p"; echo "  ✓ applied: $n"; applied=$((applied+1))
  else
    echo "  ✗ does NOT apply: $n — Hermes drift from $PIN; rebase this patch." >&2; exit 2
  fi
done
echo "done: $applied applied, $skipped already-present"
