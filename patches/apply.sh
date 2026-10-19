#!/usr/bin/env bash
# Apply the okf-engine carried patches to a pinned Hermes checkout.
#
# These are OUR patches against core Hermes files, re-applied on each Hermes
# version bump (we do not upstream them). Pinned version below.
#
# Usage:  patches/apply.sh [HERMES_DIR]      (default: $HERMES_DIR, else cwd)
# Idempotent: an already-applied leading run of the series is skipped (a re-run on a fully or
# partially patched checkout resumes).
# Exit codes: 0 = series applied or already present; 1 = the checkout or git environment is
# unusable (not a git checkout, not the TOP LEVEL of its own work tree, or the checkout could not
# be snapshotted); 2 = Hermes drift (the checkout is not at the manifest's pinned_sha, a patch does
# not apply cleanly, or -- legacy series only -- applies with a relocated hunk); 3 = the engine's
# own patch set is broken (unknown pin, malformed pinned_sha, unreadable registry, missing patch).
#
# Placement proof (okengine#1042 [35]): `git apply` never fuzzes; it applies exact context, at an
# offset when the context has moved. The active target series is sequential, so offsets are
# expected there and are NOT rejected. What proves a hunk landed where it was cut is the commit:
# the series is applied only on the exact manifest pinned_sha (CI's contract lane runs the shipped
# series and the declared Hermes-side tests on that commit). A Hermes bump that must rebase the
# series on another commit sets OKENGINE_ALLOW_UNPINNED_HERMES=1 for that work, and is told so.
set -euo pipefail

HERMES="${1:-${HERMES_DIR:-$PWD}}"
PATCHROOT="$(cd "$(dirname "$0")" && pwd)"
# Read the Hermes pin from engine-manifest.yaml (the single source of truth), NOT a hardcoded literal
# that silently drifts from the manifest on a bump — the exact class the cron-plus pin already binds
# by test (invariant-audit #6). The advisory check below just reports the pin these patches target.
PIN="$(grep -E '^[[:space:]]*pinned_tag:' "$PATCHROOT/../engine-manifest.yaml" 2>/dev/null | awk '{print $2}' | head -1)"
PIN="${PIN:-unknown}"
# The FIRST pinned_sha in the manifest is runtime's (the same read build-engine-image.sh does). An
# empty value (or a bare comment) is the documented opt-out; anything else must be a full sha.
PIN_SHA="$(awk '/pinned_sha:/{print $2; exit}' "$PATCHROOT/../engine-manifest.yaml" 2>/dev/null)"
case "$PIN_SHA" in \#*) PIN_SHA="" ;; esac
if [ -n "$PIN_SHA" ] && ! printf '%s' "$PIN_SHA" | grep -qE '^[0-9a-f]{40}$'; then
  echo "ERROR: engine-manifest.yaml pinned_sha '$PIN_SHA' is not a 40-character commit sha" >&2; exit 3
fi

case "$PIN" in
  v2026.7.7.2) PATCHDIR="$PATCHROOT"; REGISTRY="$PATCHROOT/README.md"; STRICT_OFFSET=1 ;;
  v2026.9.14) PATCHDIR="$PATCHROOT/target-v2026.9.14"; REGISTRY="$PATCHDIR/inventory.json"; STRICT_OFFSET=0 ;;
  *) echo "ERROR: no governed Hermes patch set for manifest pin $PIN" >&2; exit 3 ;;
esac

cd "$HERMES"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  echo "ERROR: $HERMES is not a git checkout of Hermes" >&2; exit 1; }
# A Hermes tree nested inside ANOTHER repository passes the check above, but every patch path then
# resolves against the outer root: nothing applies, the reverse check "succeeds" on nothing, and
# an unpatched tree was reported fully patched with exit 0 (okengine#1042 [34]).
top="$(git rev-parse --show-toplevel 2>/dev/null)" || top=""
if [ -z "$top" ] || [ "$(cd "$top" && pwd -P)" != "$(pwd -P)" ]; then
  echo "ERROR: $HERMES is not the top level of its own git checkout (enclosing work tree: ${top:-unknown}); clone Hermes at the pin instead of patching a copy inside another repository" >&2
  exit 1
fi

cur="$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)"
echo "Hermes checkout: $cur   (patches cut against pin: $PIN)"
[ "$cur" = "$PIN" ] || echo "  ⚠ not at the pinned tag name — the pinned commit check below decides."
head_sha="$(git rev-parse HEAD)"
if [ -z "$PIN_SHA" ]; then
  echo "  ⚠ engine-manifest.yaml has no pinned_sha — hunk placement is NOT proven by commit" >&2
elif [ "$head_sha" != "$PIN_SHA" ]; then
  if [ "${OKENGINE_ALLOW_UNPINNED_HERMES:-0}" = 1 ]; then
    echo "  ⚠ OKENGINE_ALLOW_UNPINNED_HERMES=1: applying on $head_sha, not the pinned commit $PIN_SHA — a relocated hunk will NOT be caught here" >&2
  else
    echo "ERROR: Hermes checkout is not at the pinned commit: HEAD $head_sha, engine-manifest.yaml pinned_sha $PIN_SHA. Check out the pinned commit (set OKENGINE_ALLOW_UNPINNED_HERMES=1 only while rebasing the series for a Hermes bump)." >&2
    exit 2
  fi
fi

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

# Idempotency is decided for the SERIES, not per patch. The target series is sequential (several
# artifacts rewrite the same scheduler hunks), so once a later patch has landed an earlier one no
# longer reverses on its own, and a per-patch reverse check called a fully patched tree "drift"
# (okengine#991 audit 1 [16]). Instead find the longest applied PREFIX: the checkout holds pin +
# patches 1..k exactly when reversing k, k-1, ..., 1 IN SEQUENCE succeeds. The reversal runs in a
# throwaway index seeded from the working tree, so the checkout itself is never touched by it.
# Then k+1..n are applied one at a time with the strict checks below.
patches=("$PATCHDIR"/[0-9]*.patch)
[ -e "${patches[0]}" ] || { echo "ERROR: no patches found in $PATCHDIR" >&2; exit 3; }
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
in_scratch_index() { GIT_INDEX_FILE="$scratch/index" "$@"; }
# The tree of HEAD plus the working-tree state of every path the series touches.
snapshot_worktree() {
  in_scratch_index git read-tree HEAD || return 1
  sed -n -e 's#^--- a/##p' -e 's#^+++ b/##p' "${patches[@]}" | sort -u | while IFS= read -r f; do
    if [ -e "$f" ]; then in_scratch_index git update-index --add -- "$f" || exit 1
    else in_scratch_index git update-index --force-remove -- "$f" || exit 1; fi
  done || return 1
  in_scratch_index git write-tree
}
worktree_tree="$(snapshot_worktree)" || {
  echo "ERROR: could not snapshot the checkout at $HERMES (git read-tree/update-index/write-tree failed); no patch was applied" >&2
  exit 1; }
done_count=0 reverse_output=""
for ((k=${#patches[@]}; k>0; k--)); do
  in_scratch_index git read-tree "$worktree_tree"
  out="" ok=1
  for ((i=k-1; i>=0; i--)); do
    if ! step="$(in_scratch_index git apply --cached --reverse --verbose "${patches[i]}" 2>&1)"; then
      ok=0; break
    fi
    out+="$step"$'\n'
  done
  if [ "$ok" -eq 1 ]; then done_count=$k reverse_output="$out"; break; fi
done
if [ "$done_count" -gt 0 ]; then
  last="$(basename "${patches[done_count-1]}")"
  if [ "$STRICT_OFFSET" -eq 1 ] && printf '%s\n' "$reverse_output" | grep -qi 'offset'; then
    echo "  ✗ reverse check relocates a hunk: $last — already-applied placement is not exact; rebase or repair the checkout." >&2
    exit 2
  fi
fi

# Name the patch a drift really sits in (QA on !1250). The forward check only knows the first patch
# that will not apply after the applied prefix, which on a fully patched tree with one drifted hunk
# is an early, innocent patch. So: (1) replay the series on HEAD in the scratch index; a patch that
# fails there is upstream drift and is named. (2) Otherwise pick the series state pin + 1..m the
# checkout is nearest to (fewest differing lines; ties keep the earlier state), reverse m, m-1, ...
# from the checkout, and name the first patch, from the top of the series, whose hunks no longer
# match. That can be a later patch whose context overlaps the drifted line. (3) If every one of
# them still reverses, the drift sits ahead of the prefix, in patch m+1.
# Prints "<patch>\t<upstream|reverse|ahead>\t<differing paths>" (paths may be empty). Advisory only: the caller treats a
# failed or empty diagnosis as "name the patch that failed" and keeps the exit-2 drift contract.
diagnose_drift() {
  local -a states
  local i m d best=0 best_d="" current
  differing() {  # up to three paths that differ from the chosen state, then "+N more"
    git diff --name-only "${states[best]}" "$current" \
      | awk 'NR <= 3 {printf "%s%s", (NR > 1 ? ", " : ""), $0} END {if (NR > 3) printf " +%d more", NR - 3}'
  }
  # Every step returns 1 on failure: this runs in a command substitution, which does not inherit
  # `set -e`, and a step that carried on with empty data would name a confident wrong patch.
  current="$(snapshot_worktree)" || return 1
  in_scratch_index git read-tree HEAD || return 1
  states[0]="$(in_scratch_index git write-tree)" || return 1
  for ((i=0; i<${#patches[@]}; i++)); do
    if ! in_scratch_index git apply --cached "${patches[i]}" >/dev/null 2>&1; then
      printf '%s\tupstream\t\n' "$(basename "${patches[i]}")"; return
    fi
    states[i+1]="$(in_scratch_index git write-tree)" || return 1
  done
  for ((m=0; m<=${#patches[@]}; m++)); do
    d="$(git diff --numstat "${states[m]}" "$current" | awk '{s += $1 + $2} END {print s + 0}')" \
      || return 1
    if [ -z "$best_d" ] || [ "$d" -lt "$best_d" ]; then best=$m best_d=$d; fi
  done
  in_scratch_index git read-tree "$current" || return 1
  for ((i=best-1; i>=0; i--)); do
    if ! in_scratch_index git apply --cached --reverse "${patches[i]}" >/dev/null 2>&1; then
      printf '%s\treverse\t%s\n' "$(basename "${patches[i]}")" "$(differing)"
      return
    fi
  done
  i=$(( best < ${#patches[@]} ? best : ${#patches[@]} - 1 ))
  printf '%s\tahead\t%s\n' "$(basename "${patches[i]}")" "$(differing)"
}

applied=0 skipped=0
for ((i=0; i<${#patches[@]}; i++)); do
  p="${patches[i]}"
  n="$(basename "$p")"
  if [ "$i" -lt "$done_count" ]; then
    echo "  • already applied: $n"; skipped=$((skipped+1)); continue
  fi
  check_rc=0
  check_output="$(git apply --check --verbose "$p" 2>&1)" || check_rc=$?
  # The v0.21.3 artifacts are a governed sequential series. Earlier artifacts
  # intentionally move later line coordinates, so offsets are expected there and
  # placement is proven by the pinned commit above. The legacy independent set
  # retains exact offsets. (`git apply` has no fuzz mode, so there is no fuzz check.)
  if [ "$check_rc" -eq 0 ] && [ "$STRICT_OFFSET" -eq 1 ] && printf '%s\n' "$check_output" | grep -qi 'offset'; then
    echo "  ✗ relocates a hunk: $n — exact patch placement is not proven; rebase this patch." >&2
    exit 2
  fi
  if [ "$check_rc" -eq 0 ]; then
    git apply "$p"; echo "  ✓ applied: $n"; applied=$((applied+1))
  else
    diag="$(diagnose_drift 2>/dev/null)" || diag=""
    culprit="" kind="" drift_path=""
    IFS=$'\t' read -r culprit kind drift_path <<<"$diag" || true
    case "$kind" in
      upstream) why="the first patch that fails on a clean replay of the series on HEAD (upstream drift)" ;;
      reverse) why="the first patch, from the top of the series, whose hunks no longer match" ;;
      ahead) why="the next patch after the part of the series the checkout matches" ;;
      *) culprit="$n" drift_path=""; why="the patch the series stopped at (drift diagnosis unavailable)" ;;
    esac
    where="${drift_path:+ (differing: $drift_path)}"
    echo "  ✗ does NOT apply: $culprit — $why. Hermes drift from $PIN$where; rebase this patch or start from a pristine $PIN checkout. (The series stopped at $n after $((skipped + applied)) applied/present patch(es).)" >&2; exit 2
  fi
done
echo "done: $applied applied, $skipped already-present"
