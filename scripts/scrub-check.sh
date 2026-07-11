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

GLOBS=('*.py' '*.md' '*.yaml' '*.yml' '*.json' '*.sh' '*.toml')
found=0

# git grep: exit 0 (prints matches) when a leak IS present, exit 1 when clean. So a taken if-branch
# == a leak. `|| true` keeps the no-match exit 1 from tripping anything.
if git grep -inE "192\.168\." -- "${GLOBS[@]}"; then found=1; fi

if [ -f .scrub-patterns ]; then
  if git grep -inE -f .scrub-patterns -- "${GLOBS[@]}" \
        ':!scripts/publish-snapshot.sh' ':!tests/test_publish_snapshot.py'; then
    found=1
  fi
fi

if [ "$found" = 1 ]; then
  echo "scrub: ✗ domain leak(s) found above — do NOT commit (fix, or move a false positive out of tracked files)." >&2
  exit 1
fi
echo "scrub: ✓ clean"
exit 0
