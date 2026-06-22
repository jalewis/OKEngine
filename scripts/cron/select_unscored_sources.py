#!/usr/bin/env python3
"""Wake-gate + digest builder for the source-quality-backfill cron.

Identifies source pages that lack `reliability` and/or `credibility` in
their YAML frontmatter — sources ingested before the source-rating step
was in place. Emits a batch of N for the agent to score in-place.

Wake-gates if no unscored source pages remain — the cron then naturally
no-ops once the historical corpus has been swept. After the backlog
clears, the cron stays scheduled but does nothing on each tick.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
N = int(os.environ.get("QUALITY_BACKFILL_BATCH_SIZE", "20"))


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.S)


def read_frontmatter(path: Path) -> dict | None:
    """Return parsed frontmatter dict, or None if missing/unparseable.

    Uses a line-aware regex so `---` substrings inside frontmatter
    comments (e.g. `# --- Source quality ---`) don't prematurely split
    the document — the prior `txt.split('---', 2)` impl would lose
    every field after such a comment.
    """
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(txt)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def is_unscored(fm: dict) -> bool:
    if fm.get("type") != "source":
        return False
    # Treat None / missing / empty-string as unscored. The new schema requires
    # both reliability and credibility to be set.
    if not fm.get("reliability"):
        return True
    if fm.get("credibility") in (None, ""):
        return True
    return False


def main() -> int:
    sources_dir = VAULT / "wiki" / "sources"
    if not sources_dir.is_dir():
        print(f"ERROR: sources dir not found at {sources_dir}", file=sys.stderr)
        return 1

    all_sources = sorted(p for p in sources_dir.rglob("*.md") if not p.name.startswith("_"))

    unscored: list[tuple[Path, dict]] = []
    type_source_count = 0
    for p in all_sources:
        fm = read_frontmatter(p)
        if fm is None or fm.get("type") != "source":
            continue
        type_source_count += 1
        if is_unscored(fm):
            unscored.append((p, fm))

    print("=== source-quality-backfill wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  total source pages: {type_source_count}")
    print(f"  unscored: {len(unscored)}")
    print(f"  batch size: {N}")

    if not unscored:
        print("  → SKIP: every source page has reliability + credibility set")
        print(json.dumps({"wakeAgent": False}))
        return 0

    # Pick the most-recently-ingested unscored sources first — newest are
    # most likely to anchor predictions, so score them first.
    unscored.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
    chosen = unscored[:N]

    print()
    print(f"=== batch ({len(chosen)} of {len(unscored)} unscored) ===")
    print(f"Process IN ORDER. Score each in-place by updating frontmatter only — do NOT rewrite the body.\n")
    for i, (p, fm) in enumerate(chosen, 1):
        rel = p.relative_to(VAULT).as_posix()
        publisher = fm.get("publisher") or "(no publisher)"
        published = fm.get("published") or "?"
        kind = fm.get("source_kind") or "?"
        raw_path = fm.get("raw") or "(no raw)"
        print(f"{i}. `{rel}`")
        print(f"   publisher={publisher}  published={published}  kind={kind}")
        print(f"   raw={raw_path}")

    print()
    print(f"After scoring: append a single `wiki/log.md` entry: `## [{datetime.now(timezone.utc).strftime('%Y-%m-%d')}] source-quality-backfill | {len(chosen)} sources scored`. Then respond with exactly `[SILENT]`.")
    print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
