#!/usr/bin/env bash
# Pre-commit domain-leak gate with CONVENTIONAL exit semantics: exit 0 = clean, exit 1 = a leak was
# found (the offending lines are printed). The raw `git grep` returns 1 on NO match (i.e. on SUCCESS),
# so the documented inline commands abort a `set -e` script exactly when they pass — this wraps them so
# scripts, hooks, and CI can gate on the result normally (issue okengine#204, gap 6).
#
# Scans exactly the TRACKED files (the leak surface) across the code/doc globs, using the git-ignored
# .scrub-patterns (private hostnames/product names, one extended regex per line) plus the generic
# private-IP pattern. publish-snapshot.sh + its test are excluded (they legitimately contain the
# patterns). See CLAUDE.md "Before you commit".
set -uo pipefail   # NOT -e: a clean `git grep` returns 1 and must not abort the gate

cd "$(git rev-parse --show-toplevel)" || { echo "scrub: not in a git repo" >&2; exit 2; }

# Scan the WHOLE tracked tree (git grep skips binaries by default), matching the publish-time scrub
# which greps the entire staged snapshot — the old 7-glob subset never scanned tracked files that SHIP
# in the public snapshot (static/*.js|html|css, Dockerfiles, patches/*.patch, requirements*.txt,
# .env.example, feeds.opml, Makefile, LICENSE), so a hardcoded private URL in app.js passed the gate
# and only tripped at release (invariant-audit #55). EXCLUDE the paths publish-snapshot.sh also
# excludes: the publish scrubber + its test (they legitimately CONTAIN the patterns), plus the
# INTERNAL-only docs that never ship (they reference the dev remote / internal issues by design) —
# keep this list in sync with publish-snapshot.sh's EXCLUDE array (invariant-audit #55/#56).
EXCL=(
  ':!scripts/publish-snapshot.sh' ':!tests/test_publish_snapshot.py'
  ':!docs/release-checklist.md' ':!CLAUDE.md' ':!scripts/audit'
  ':!docs/testing-and-audit.md' ':!docs/hermes-upgrades'
  ':!docs/design/sec-threat-hunting-prd.md' ':!docs/design/sec-threat-hunting-technical-spec.md'
  ':!.gitlab-ci.yml'   # legitimately names the internal group runner (org-abbrev pattern); publish-excluded, never ships
)
found=0

# git grep: exit 0 (prints matches) when a leak IS present, exit 1 when clean. So a taken if-branch
# == a leak.
if git grep -inE "192\.168\." -- "${EXCL[@]}"; then found=1; fi

if [ -f .scrub-patterns ]; then
  if git grep -inE -f .scrub-patterns -- "${EXCL[@]}"; then
    found=1
  fi
else
  # .scrub-patterns is git-ignored (the patterns ARE the secrets) — absent on a fresh clone. Silently
  # skipping the private-token half read as "clean" when it was UNDETECTABLE; WARN loudly instead, the
  # same posture the CI takes when its SCRUB_PATTERNS variable is missing (okengine#326 [17]).
  echo "scrub: ⚠ .scrub-patterns absent — the private-token half is UNDETECTABLE (this is NOT a pass; the generic 192.168 check still ran)." >&2
fi

if [ "$found" = 1 ]; then
  echo "scrub: ✗ domain leak(s) found above — do NOT commit (fix, or move a false positive out of tracked files)." >&2
  exit 1
fi
echo "scrub: ✓ clean"
exit 0
