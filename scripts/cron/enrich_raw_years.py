#!/usr/bin/env python3
"""Extract per-file publication dates from raw/ HTML scrapes, write a year index.

Many `raw/<bulk-dir>/<sub>/<slug>/content.txt` files have mtimes set by a
bulk-import operation rather than the document's true publication date,
mis-classifying old material as fresh.

This script scans each bulk-archive HTML scrape for publication-date metadata
(OpenGraph, JSON-LD, HTML5 <time>) and writes the derived year to a single
index file `raw/.year_index.json`. `select_raw_batch.derive_year()` consults
the index before falling back to mtime, restoring accurate year ordering for
the bulk-archive backlog.

Idempotent: re-runs only process paths NOT already in the index (unless --force).
The index is keyed by VAULT-relative path (matching how select_raw_batch
normalizes paths). Values are integer years (no full dates — `derive_year`
only needs year granularity).

Output: stderr progress every N files, final stats grouped by year. The index
is the artifact; nothing under `wiki/` is written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
INDEX_PATH = VAULT / "raw" / ".year_index.json"

# Restrict to the bulk-archive subdir by default — that's where the bulk-import
# mtime problem is. Other subdirs (curated content) are individually saved and
# their mtimes are trustworthy. Override with --scan-root or BULK_DIR.
DEFAULT_SCAN_ROOTS = (os.environ.get("BULK_DIR", "bulk"),)
LEAF_EXTS = {".md", ".pdf", ".txt", ".html"}

# Date-metadata patterns. Each captures a YYYY in group 1.
# Order matters: most specific / most reliable first.
_DATE_PATTERNS = [
    # OpenGraph: <meta property="article:published_time" content="2024-02-28T..."
    re.compile(rb'article:published_time["\'\s]*content=["\'](\d{4})-\d{2}-\d{2}', re.IGNORECASE),
    # Schema.org JSON-LD: "datePublished":"2017-01-04T..."
    re.compile(rb'"datePublished"\s*:\s*"(\d{4})-\d{2}-\d{2}', re.IGNORECASE),
    # itemprop variant
    re.compile(rb'itemprop=["\']datePublished["\'][^>]*content=["\'](\d{4})-\d{2}-\d{2}', re.IGNORECASE),
    re.compile(rb'content=["\'](\d{4})-\d{2}-\d{2}[T"][^>]*itemprop=["\']datePublished["\']', re.IGNORECASE),
    # HTML5 <time datetime="..." pubdate>  (rare in modern HTML but cheap to check)
    re.compile(rb'<time[^>]+datetime=["\'](\d{4})-\d{2}-\d{2}[^"\']*["\'][^>]*pubdate', re.IGNORECASE),
    # dated reference pages: ">Created:&nbsp;</span>17 October 2018"
    re.compile(rb'Created.{0,100}?\b(20[12]\d)\b', re.IGNORECASE),
    # NOTE: a "Last Published:" HTML-comment pattern was deliberately NOT
    # included. On some site builders that stamp is a *site rebuild* timestamp,
    # not a publication date — it gives every page (even years-old ones) the
    # rebuild year as its "publication" year, producing fake-fresh entries. The
    # proper-metadata patterns above (OG, JSON-LD, itemprop, HTML5 pubdate)
    # catch sites with real publication dates; the dateModified fallback below
    # catches sites without. Rebuild stamps add only false positives.
    # JS-embedded data attribute (e.g. dataLayer): "'date': '2025/04/14'"
    re.compile(rb"['\"]date['\"]\s*:\s*['\"](\d{4})/\d{2}/\d{2}", re.IGNORECASE),
    # blog/diary entries: "<b>Published</b>: 2024-04-29."
    re.compile(rb'<b>\s*Published\s*</b>\s*:\s*(\d{4})-\d{2}-\d{2}', re.IGNORECASE),
    # HTML5 <time datetime="..."> WITHOUT pubdate — advisory-style pages.
    # Lower priority than the pubdate variant because it could be a modification date.
    re.compile(rb'<time[^>]+datetime=["\'](\d{4})-\d{2}-\d{2}', re.IGNORECASE),
    # Fallback: dateModified (article was updated, content is at least that old)
    re.compile(rb'"dateModified"\s*:\s*"(\d{4})-\d{2}-\d{2}', re.IGNORECASE),
    re.compile(rb'article:modified_time["\'\s]*content=["\'](\d{4})-\d{2}-\d{2}', re.IGNORECASE),
]


def extract_year(path: Path, head_bytes: int = 65536) -> int | None:
    """Read first head_bytes of file, try patterns in order, return first plausible year."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(head_bytes)
    except OSError:
        return None
    for pat in _DATE_PATTERNS:
        m = pat.search(chunk)
        if m:
            try:
                year = int(m.group(1))
            except (ValueError, IndexError):
                continue
            # Plausibility filter — defensive against `2099` placeholders, etc.
            if 2000 <= year <= datetime.now(timezone.utc).year + 1:
                return year
    return None


def load_index() -> dict[str, int]:
    if INDEX_PATH.is_file():
        try:
            data = json.loads(INDEX_PATH.read_text())
            if isinstance(data, dict):
                return {k: int(v) for k, v in data.items() if isinstance(v, (int, str))}
        except (OSError, json.JSONDecodeError, ValueError):
            print(f"WARN: existing index at {INDEX_PATH} unreadable; starting fresh", file=sys.stderr)
    return {}


def save_index(idx: dict[str, int]) -> None:
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Stable serialization for diff-friendly inspection
    INDEX_PATH.write_text(json.dumps(dict(sorted(idx.items())), indent=2) + "\n")


def iter_content_files(scan_roots: tuple[str, ...]):
    """Yield (rel_path, abs_path) for HTML-scrape files worth extracting from.

    A common bulk-archive layout uses `<sub>/<slug>/content.txt` for the HTML
    body and `<slug>/link.md` for the source URL. Only `content.txt` (or
    bare `.html`) holds extractable metadata. `link.md` files are stubs;
    their year is propagated from the sibling content.txt in a second pass.
    """
    for root_name in scan_roots:
        root = VAULT / "raw" / root_name
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part.startswith(".") for part in p.parts):
                continue
            if any("\\" in part for part in p.parts):
                continue
            name = p.name.lower()
            if name == "content.txt" or p.suffix.lower() == ".html":
                rel = str(p.relative_to(VAULT))
                yield rel, p


def iter_sibling_paths(scan_roots: tuple[str, ...]):
    """Yield (rel_path, abs_path, parent_rel) for every non-content.txt file
    under scan_roots so we can propagate year from content.txt to siblings."""
    for root_name in scan_roots:
        root = VAULT / "raw" / root_name
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in LEAF_EXTS:
                continue
            if any(part.startswith(".") for part in p.parts):
                continue
            if any("\\" in part for part in p.parts):
                continue
            if p.name.lower() == "content.txt":
                continue  # the source of truth, not a propagation target
            if p.suffix.lower() == ".html":
                continue  # extractable directly, already handled
            rel = str(p.relative_to(VAULT))
            parent_rel = str(p.parent.relative_to(VAULT))
            yield rel, p, parent_rel


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a year-index for raw/ HTML scrapes")
    parser.add_argument("--force", action="store_true",
                        help="Re-scan files already in the index (default: skip them)")
    parser.add_argument("--scan-root", action="append", dest="scan_roots", default=None,
                        help="Top-level subdir of raw/ to scan (default: $BULK_DIR or 'bulk'). May repeat.")
    parser.add_argument("--progress-every", type=int, default=500,
                        help="Print progress every N files scanned")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N new files (0 = no limit). For dry-run tuning.")
    args = parser.parse_args()

    scan_roots = tuple(args.scan_roots) if args.scan_roots else DEFAULT_SCAN_ROOTS

    if not (VAULT / "raw").is_dir():
        print(f"ERROR: raw/ not found at {VAULT}", file=sys.stderr)
        return 1

    index = load_index()
    initial_size = len(index)
    print(f"loaded index: {initial_size} entries at {INDEX_PATH}", file=sys.stderr)
    print(f"scan roots: {scan_roots}", file=sys.stderr)

    # --- Pass 1: extract from content.txt / *.html ---
    scanned = 0
    new_entries = 0
    matched = unmatched = 0
    dir_year: dict[str, int] = {}  # parent_rel -> year, for pass-2 propagation
    new_years: Counter[int] = Counter()

    for rel, abs_path in iter_content_files(scan_roots):
        if rel in index and not args.force:
            # Still remember this dir's year so pass 2 can propagate
            dir_year[str(abs_path.parent.relative_to(VAULT))] = index[rel]
            continue
        scanned += 1
        if args.limit and new_entries >= args.limit:
            break
        year = extract_year(abs_path)
        if year is not None:
            index[rel] = year
            dir_year[str(abs_path.parent.relative_to(VAULT))] = year
            new_entries += 1
            new_years[year] += 1
            matched += 1
        else:
            unmatched += 1
        if scanned % args.progress_every == 0:
            print(f"  pass-1 scanned {scanned}, matched {matched}", file=sys.stderr)

    # --- Pass 2: propagate dir-level year to sibling stubs (link.md, etc.) ---
    propagated = 0
    for rel, _abs, parent_rel in iter_sibling_paths(scan_roots):
        if rel in index and not args.force:
            continue
        year = dir_year.get(parent_rel)
        if year is None:
            continue
        index[rel] = year
        propagated += 1

    save_index(index)

    print(f"\nDone. Index now has {len(index)} entries (was {initial_size}, "
          f"+{len(index) - initial_size}).", file=sys.stderr)
    print(f"  Pass 1 (content.txt extraction): scanned {scanned}, matched {matched}, "
          f"unmatched {unmatched}", file=sys.stderr)
    print(f"  Pass 2 (sibling propagation): added {propagated}", file=sys.stderr)
    if new_years:
        print("\nExtracted year distribution (pass 1 only):", file=sys.stderr)
        for year in sorted(new_years.keys(), reverse=True):
            print(f"  {year}: {new_years[year]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
