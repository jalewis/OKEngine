#!/usr/bin/env python3
"""Collapse malformed multi-date `updated:` frontmatter values to the newest date.

Agent-driven crons (entity-backfill, raw-backfill, concept-backfill) are
instructed to "bump `updated:` to today" but the agent's patch sometimes
PREPENDS the new date instead of replacing the old one, producing:
    updated: 2026-05-28 2026-05-26 2026-05-24
This breaks `yaml.safe_load` date parsing and any Dataview sort on the
field.

This script is deterministic (no agent) and idempotent. It scans the
vault for `updated:` lines carrying >1 ISO date and rewrites each to the
single newest (max) date. Touches ONLY the updated: line — no other
content. Runs as a script-only cron and can be re-run safely any time.

Scope: live wiki/ pages only — skips backup/restore artifacts
(*.bak*, _archived/, *.was-broken, *.restored*, *.corrupt.*) so we don't
churn deliberately-frozen snapshots.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"

# An `updated:` line whose value is two or more space-separated ISO dates.
# An ISO value: a date OR a `T`-separated ISO-8601 timestamp (the write path stamps the latter,
# okengine#... — so the multi-value separator stays an unambiguous space, the value has no space).
_TS = r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z?)?"
_MULTIDATE_RE = re.compile(
    rf"^(updated:[ \t]*)({_TS}(?:[ \t]+{_TS})+)[ \t]*(#.*)?$",
    re.MULTILINE,
)
_ISO_DATE = re.compile(_TS)

# Path fragments that mark a non-live / frozen artifact we must not rewrite.
_SKIP_SUBSTRINGS = (
    ".bak", "_archived/", ".was-broken", ".restored", ".corrupt",
    "/tmp_", "/_test_",
)


def _is_skippable(path: Path) -> bool:
    s = str(path)
    return any(frag in s for frag in _SKIP_SUBSTRINGS)


def _newest(dates: list[str]) -> str:
    """Lexicographic max == chronological max for ISO yyyy-mm-dd."""
    return max(dates)


def sanitize_text(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return (new_text, fixes) where fixes is a list of (before, after)
    for each rewritten updated: value."""
    fixes: list[tuple[str, str]] = []

    def repl(m: re.Match) -> str:
        prefix, dates_blob, comment = m.group(1), m.group(2), m.group(3)
        dates = _ISO_DATE.findall(dates_blob)
        newest = _newest(dates)
        fixes.append((dates_blob.strip(), newest))
        tail = f"  {comment}" if comment else ""
        return f"{prefix}{newest}{tail}"

    new_text = _MULTIDATE_RE.sub(repl, text)
    return new_text, fixes


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: {WIKI} does not exist", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    files_fixed = 0
    values_fixed = 0
    perm_skips = 0
    print("=== sanitize-frontmatter-updated ===")
    for path in sorted(WIKI.rglob("*.md")):
        if _is_skippable(path):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if "updated:" not in text:
            continue
        new_text, fixes = sanitize_text(text)
        if not fixes:
            continue
        try:
            path.write_text(new_text)
        except PermissionError:
            perm_skips += 1
            print(f"  ! {path.relative_to(VAULT)}: PERMISSION DENIED (host-owned) — skipped")
            continue
        files_fixed += 1
        values_fixed += len(fixes)
        for before, after in fixes:
            print(f"  + {path.relative_to(VAULT)}: '{before}' → '{after}'")

    print()
    print(f"Fixed {values_fixed} value(s) across {files_fixed} file(s).")
    if perm_skips:
        print(f"{perm_skips} file(s) skipped for permissions (chmod 646 + re-run).")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
