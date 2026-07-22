#!/usr/bin/env python3
"""Remove replay-created duplicate rows from the operational review queue.

The first row for each bold canonical path is retained.  Every flag reason is
already preserved in wiki/log.md; this command only restores the queue's
one-outstanding-item-per-page invariant.  It is dry-run unless --write is set.
"""

import argparse
import re
from pathlib import Path


ROW = re.compile(r"^- \d{4}-\d{2}-\d{2} \*\*(?P<path>[^*]+)\*\* — ")


def dedupe(text: str) -> tuple[str, list[str]]:
    seen: set[str] = set()
    removed: list[str] = []
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        match = ROW.match(line)
        if match and match.group("path") in seen:
            removed.append(match.group("path"))
            continue
        if match:
            seen.add(match.group("path"))
        kept.append(line)
    return "".join(kept), removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    queue = args.pack.expanduser().resolve() / "wiki" / "_review-queue.md"
    if not queue.is_file():
        parser.error(f"review queue not found: {queue}")
    original = queue.read_text(encoding="utf-8")
    cleaned, removed = dedupe(original)
    print(f"duplicate review rows: {len(removed)}")
    for path in removed:
        print(f"- {path}")
    if args.write and removed:
        backup = queue.with_suffix(".md.bak-okengine-397")
        if not backup.exists():
            backup.write_text(original, encoding="utf-8")
        queue.write_text(cleaned, encoding="utf-8")
        print(f"updated: {queue}")
        print(f"backup: {backup}")
    elif removed:
        print("dry run; pass --write to update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
