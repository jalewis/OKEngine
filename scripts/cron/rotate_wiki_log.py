#!/usr/bin/env python3
"""Rotate `wiki/log.md` into dated archives so the live ledger stays a readable page.

`log.md` is the vault's audit trail: the enforced write path appends one line per successful
write, and sixteen lanes append their own. That single-file design is deliberate and stays —
every writer keeps appending to `wiki/log.md`, and every reserved-file guard keeps matching it by
name. What it lacks is an ending.

Measured 2026-08-18 on a live vault: 40,575 lines over 39 days, 5.0 MB, growing ~1,000 lines
(~128 KB) a day — roughly 47 MB a year, per deployment. The reader surfaces log.md for READING,
so this is a page a human is expected to open, and at that size it stops being one.

So: lines dated before today move to `wiki/_logs/<YYYY-MM-DD>.md`, and today's stay put. The
archive lives under a `_`-prefixed directory, which the projection scanner, index builders,
corpus indexer, field-loss detector and write path all already skip by convention — so an archive
never becomes a corpus page. Chronology is preserved, which is why this rotates by DATE rather
than by lane: one trail you can read in order beats sixteen you have to merge.

Two line shapes carry a date, and both are recognised:

    ## [2026-07-10] source-quality-backfill | 20 sources scored ...
    - 2026-07-10 mcp-write create sources/2026/07/some-page.md v1

A line with no date of its own belongs to the entry above it and travels with it, so a wrapped
summary is never split from its heading.

**Applies by default; `--dry-run` previews.** That is deliberately the opposite of
`dedup_partition_collisions`, and the reason is mechanical rather than stylistic: cron-plus builds
a job from a fixed key set with no `args`, so a lane needing a flag to do its work would never do
it. The repo's apply-lanes solve this with a dedicated `apply_*.py` script; rotation has only one
behaviour, so the default is that behaviour.

The risk is also different in kind. dedup merges pages and can lose content, so it earns a manual
gate. Rotation moves whole lines between files without editing one, appends rather than replaces,
and rewrites through an atomic rename — and a property test asserts no line is lost or duplicated.

Env: WIKI_PATH (default /opt/vault).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

ARCHIVE_DIR = "_logs"
_DATE = re.compile(r"^(?:##\s*\[(\d{4}-\d{2}-\d{2})\]|[-*]\s+(\d{4}-\d{2}-\d{2})\b)")


def line_date(line: str) -> str | None:
    """The ISO date a log line declares, or None when it carries no date of its own."""
    match = _DATE.match(line)
    if not match:
        return None
    return match.group(1) or match.group(2)


def partition(lines: list[str], today: str) -> tuple[dict[str, list[str]], list[str]]:
    """Split lines into {date: archived lines} and the lines that stay in log.md.

    An undated line inherits the date of the entry above it, so a wrapped continuation is never
    separated from the heading it belongs to. Undated lines before ANY dated line have nothing to
    inherit and stay put — moving them would guess at a date the file never claimed.
    """
    archived: dict[str, list[str]] = {}
    kept: list[str] = []
    current: str | None = None
    for line in lines:
        stamped = line_date(line)
        if stamped is not None:
            current = stamped
        if current is None or current >= today:
            kept.append(line)
        else:
            archived.setdefault(current, []).append(line)
    return archived, kept


def rotate(vault: Path, today: str, apply: bool = False) -> dict:
    """Move every entry older than `today` out of log.md and into wiki/_logs/<date>.md."""
    log = vault / "wiki" / "log.md"
    if not log.is_file():
        return {"rotated": 0, "dates": [], "kept": 0, "applied": False, "reason": "no log.md"}
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    archived, kept = partition(lines, today)
    report = {
        "rotated": sum(len(rows) for rows in archived.values()),
        "dates": sorted(archived),
        "kept": len(kept),
        "applied": bool(apply and archived),
    }
    if not apply or not archived:
        return report
    target = vault / "wiki" / ARCHIVE_DIR
    target.mkdir(parents=True, exist_ok=True)
    for stamped, rows in sorted(archived.items()):
        # Append rather than replace: a date that somehow rotates twice adds to its archive
        # instead of silently discarding whatever was already filed under it.
        with (target / f"{stamped}.md").open("a", encoding="utf-8") as handle:
            handle.writelines(rows)
    # Rewrite through a temp file in the same directory so a crash cannot leave a truncated
    # ledger: the rename is atomic, and until it happens the original is intact.
    scratch = log.with_suffix(".md.rotating")
    scratch.write_text("".join(kept), encoding="utf-8")
    scratch.replace(log)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would move without touching anything")
    parser.add_argument("--root", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    parser.add_argument("--today", default=None,
                        help="ISO date treated as 'today'; entries before it rotate")
    args = parser.parse_args(argv)
    today = args.today or date.today().isoformat()
    report = rotate(Path(args.root), today, apply=not args.dry_run)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
