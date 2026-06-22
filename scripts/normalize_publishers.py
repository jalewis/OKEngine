#!/usr/bin/env python3
"""Normalize publisher: frontmatter values across wiki/sources/*.md against config/publishers.canonical.json.

Default mode is dry-run: prints proposed changes and per-canonical tallies, writes nothing.
Pass --apply to actually rewrite files.

Composite / joint-attribution forms are not in the mapping and are left untouched.
Files are git-tracked, so git is the snapshot — no .bak files written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# publishers.canonical.json is DOMAIN data — it lives in the VAULT, not this engine
# repo (removed engine-side in ec763cd4f, "engine free of domain data"). The live
# source of truth is the vault's `config/publishers.canonical.json`, maintained
# in-place by the `publisher-canonical-drain` cron. Point the dev tool there so it
# normalizes against the SAME mapping the cron grows (not a stale pack snapshot).
DEFAULT_WIKI = Path(os.environ.get("WIKI_PATH_HOST", "/path/to/vault"))
DEFAULT_MAPPING = Path(os.environ.get("PUBLISHERS_CANONICAL")
                       or DEFAULT_WIKI / "config" / "publishers.canonical.json")

PUBLISHER_LINE_RE = re.compile(r'^publisher:[ \t]*(?P<value>.*?)[ \t]*$')


def load_inverse_map(path: Path) -> dict[str, str]:
    """Load canonical→variants and invert to variant→canonical."""
    data = json.loads(path.read_text())
    inverse: dict[str, str] = {}
    for canonical, variants in data.items():
        if canonical.startswith("_"):
            continue
        for v in variants:
            if v in inverse and inverse[v] != canonical:
                raise ValueError(
                    f"Variant {v!r} maps to both {inverse[v]!r} and {canonical!r}"
                )
            inverse[v] = canonical
    return inverse


def parse_publisher_value(raw: str) -> str | None:
    """Strip surrounding quotes from a YAML scalar; return None if empty."""
    raw = raw.strip()
    if not raw:
        return None
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return raw


def find_frontmatter_bounds(text: str) -> tuple[int, int] | None:
    """Return (start_line_idx, end_line_idx) of the YAML frontmatter, exclusive of fences. None if absent."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return (1, i)
    return None


def normalize_file(path: Path, inverse: dict[str, str]) -> tuple[str, str] | None:
    """Return (old_value, new_value) if a rewrite would occur; None if not. Side effect: rewrites file when apply=True is passed via the caller closure (handled in main).

    This function is read-only — caller decides whether to write.
    """
    text = path.read_text()
    bounds = find_frontmatter_bounds(text)
    if bounds is None:
        return None
    start, end = bounds
    lines = text.splitlines(keepends=True)
    for idx in range(start, end):
        m = PUBLISHER_LINE_RE.match(lines[idx].rstrip("\n"))
        if not m:
            continue
        old_raw = m.group("value")
        old_value = parse_publisher_value(old_raw)
        if old_value is None:
            return None
        new_value = inverse.get(old_value)
        if new_value is None or new_value == old_value:
            return None
        return (old_value, new_value)
    return None


def rewrite_file(path: Path, new_value: str) -> None:
    """Replace the publisher: line in leading frontmatter with double-quoted canonical form."""
    text = path.read_text()
    bounds = find_frontmatter_bounds(text)
    if bounds is None:
        return
    start, end = bounds
    lines = text.splitlines(keepends=True)
    for idx in range(start, end):
        if PUBLISHER_LINE_RE.match(lines[idx].rstrip("\n")):
            newline = "\n" if lines[idx].endswith("\n") else ""
            escaped = new_value.replace('\\', '\\\\').replace('"', '\\"')
            lines[idx] = f'publisher: "{escaped}"{newline}'
            break
    path.write_text("".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING, help=f"Canonical mapping JSON (default: {DEFAULT_MAPPING})")
    ap.add_argument("--wiki", type=Path, default=DEFAULT_WIKI, help=f"Wiki root path (default: {DEFAULT_WIKI})")
    ap.add_argument("--apply", action="store_true", help="Actually rewrite files (default: dry-run)")
    args = ap.parse_args()

    sources_dir = args.wiki / "wiki" / "sources"
    if not sources_dir.is_dir():
        print(f"ERROR: sources dir not found: {sources_dir}", file=sys.stderr)
        return 2

    inverse = load_inverse_map(args.mapping)
    print(f"Loaded {len(inverse)} variant mappings from {args.mapping}", file=sys.stderr)
    print(f"Scanning {sources_dir}", file=sys.stderr)

    changes: list[tuple[Path, str, str]] = []
    by_canonical: dict[str, Counter] = defaultdict(Counter)
    files_scanned = 0

    for path in sorted(sources_dir.rglob("*.md")):   # rglob: sources may be sharded (sources/<year>/<month>/)
        if path.name == "INDEX.md" or path.name.startswith(("_", "INDEX-")):
            continue
        files_scanned += 1
        result = normalize_file(path, inverse)
        if result is None:
            continue
        old, new = result
        changes.append((path, old, new))
        by_canonical[new][old] += 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n=== {mode} — {len(changes)} files would change (of {files_scanned} scanned) ===\n")

    for canonical in sorted(by_canonical):
        total = sum(by_canonical[canonical].values())
        print(f"\n→ {canonical}  ({total} files)")
        for variant, count in by_canonical[canonical].most_common():
            print(f"     {count:4d}  {variant!r}")

    if args.apply:
        print(f"\nWriting {len(changes)} files...", file=sys.stderr)
        for path, _old, new in changes:
            rewrite_file(path, new)
        print("Done.", file=sys.stderr)
    else:
        print(f"\n(dry-run — no files written; pass --apply to rewrite)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
