#!/usr/bin/env python3
"""One-time: put an existing `_review-queue.md` in newest-first order (okengine#521).

The write path now PREPENDS queue rows, so without this a vault ends up with new rows stacked
above a stale ascending block — worse than either order on its own. This reverses the rows that
are already there, once.

Only the `- ` rows move. Frontmatter, the heading and the explanatory sentence stay exactly
where they are; a row-shaped line is the only thing this recognises, so anything else in the
file is passed through untouched.

Idempotent by inspection, not by a state file: if the dated rows are already non-increasing
there is nothing to do and the file is left byte-identical. That means it is safe to re-run,
and safe to run on a vault that was created after the write-path change.

Dry-run is the default. `--apply` writes.

Usage:
  python3 scripts/reverse_review_queue.py --vault /path/to/pack
  python3 scripts/reverse_review_queue.py --vault /path/to/pack --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ROW = re.compile(r"^- (\d{4}-\d{2}-\d{2})\b")


def split_rows(text: str) -> tuple[list[str], list[str], list[str]]:
    """(preamble, rows, trailer). Rows are the contiguous run of `- ` lines; anything after
    the last row is a trailer and is preserved in place rather than being swept into the
    reordering."""
    lines = text.splitlines(keepends=True)
    first = last = None
    for i, line in enumerate(lines):
        if line.startswith("- "):
            if first is None:
                first = i
            last = i
    if first is None:
        return lines, [], []
    return lines[:first], lines[first:last + 1], lines[last + 1:]


def _dates(rows: list[str]) -> list[str]:
    out = []
    for row in rows:
        m = _ROW.match(row)
        out.append(m.group(1) if m else "")
    return out


def already_newest_first(rows: list[str]) -> bool:
    dated = [d for d in _dates(rows) if d]
    return all(a >= b for a, b in zip(dated, dated[1:]))


def reorder(text: str) -> tuple[str, int]:
    """Returns (new_text, rows_moved). rows_moved is 0 when nothing needed to change.

    Only DATED rows are permuted, and only among the positions they already occupy. Every
    other line keeps its exact index.

    That precision is not hypothetical. One live queue contains a stray ``[END LOG]`` marker
    and three lines of model prose (``- All 20 sources have: ...``) sitting between real rows —
    an agent wrote its summary into the worklist. Reversing the contiguous block would have
    dragged ``[END LOG]`` to the top and scattered the prose through the backlog. Those lines
    do not belong in the file, but silently relocating them is not this tool's call.
    """
    lines = text.splitlines(keepends=True)
    slots = [i for i, line in enumerate(lines) if _ROW.match(line)]
    if not slots:
        return text, 0
    dated = [lines[i] for i in slots]
    if already_newest_first(dated):
        return text, 0
    for slot, row in zip(slots, reversed(dated)):
        lines[slot] = row
    return "".join(lines), len(slots)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True, help="pack/vault root (contains wiki/)")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = ap.parse_args(argv)

    queue = Path(args.vault) / "wiki" / "_review-queue.md"
    if not queue.is_file():
        print(f"reverse-review-queue: no {queue}", file=sys.stderr)
        return 2

    text = queue.read_text(encoding="utf-8")
    new_text, moved = reorder(text)
    before = [ln for ln in text.splitlines() if _ROW.match(ln + "\n")]
    after = [ln for ln in new_text.splitlines() if _ROW.match(ln + "\n")]
    _pre, block, _post = split_rows(text)
    pinned = len(block) - len(before)

    print(f"=== reverse-review-queue ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    print(f"  file       : {queue}")
    print(f"  dated rows : {len(before)}")
    if pinned > 0:
        print(f"  pinned     : {pinned} non-row line(s) between rows — left at their index")
    if not moved:
        print("  order      : already newest-first — nothing to do")
        return 0
    print(f"  reordering : {moved} dated row(s) -> newest first")
    print(f"  top dated  : {before[0].strip()[:92]}")
    print(f"            -> {after[0].strip()[:92]}")
    if not args.apply:
        print("  (dry run — nothing written; pass --apply)")
        return 0
    tmp = queue.with_suffix(queue.suffix + ".reorder-tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(queue)                      # atomic within the same directory
    print("  written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
