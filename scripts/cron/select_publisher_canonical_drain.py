#!/usr/bin/env python3
"""Wake-gate + digest for the publisher-canonical-drain cron.

Scans `wiki/sources/*.md` for `publisher:` values that don't match the
canonical list in vault `CLAUDE.md`, groups by count, surfaces any with
≥PCD_MIN_SOURCES occurrences. Wakes only when at least one new
candidate appears (single-source one-offs don't fire — they go in the
ordinary lint report for human review).

The agent then judges each candidate: legitimate new publisher → add to
vault CLAUDE.md canonical list AND `config/publishers.canonical.json`
with empty variants; obvious drift variant of existing canonical → add
to that canonical's variants array AND normalize affected source pages.
"Unknown" / "TBD" / similar data-quality flags are surfaced separately
for human review (NOT auto-added).

Why this exists: a periodic lint report can flag proposed canonical
additions without anything applying them, so the backlog accumulates
silently. Drain crons watch source state directly so it can't.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
MIN_SOURCES = int(os.environ.get("PCD_MIN_SOURCES", "10"))
MIN_HITS = int(os.environ.get("PCD_MIN_HITS", "1"))

# Vault CLAUDE.md is the source-of-truth for the canonical list.
# Parse the inline list under "Canonical names" — one big inline-code
# section with backtick-quoted entries separated by commas.
_CLAUDE_MD = VAULT / "CLAUDE.md"
_CANONICAL_BLOCK_RE = re.compile(
    r"\*\*Canonical names\*\*[^`]*?\n\n((?:`[^`]+`(?:,\s*)?)+)",
    re.DOTALL,
)
_CANONICAL_NAME_RE = re.compile(r"`([^`]+)`")
_PUBLISHER_LINE_RE = re.compile(r'^publisher:\s*(.+?)\s*$', re.MULTILINE)

# Data-quality flags (not real publishers — surface for human review, never auto-add)
DATA_QUALITY_FLAGS = {"Unknown", "TBD", "N/A", "Unspecified", "Various"}


def load_canonical_list() -> set[str]:
    """Read the canonical names from vault CLAUDE.md."""
    try:
        txt = _CLAUDE_MD.read_text(errors="replace")
    except OSError:
        return set()
    m = _CANONICAL_BLOCK_RE.search(txt)
    if not m:
        return set()
    return {name.strip() for name in _CANONICAL_NAME_RE.findall(m.group(1))}


def extract_publisher(text: str) -> str | None:
    """Pull `publisher:` value from a source page's frontmatter, stripping
    quotes and trailing comments."""
    m = _PUBLISHER_LINE_RE.search(text)
    if not m:
        return None
    val = m.group(1)
    # Strip trailing YAML comment (# ...) — but only if not inside quotes
    if val and not val.startswith(('"', "'")):
        val = val.split("#", 1)[0].strip()
    val = val.strip().strip('"').strip("'")
    return val or None


def scan_publishers() -> Counter:
    """Count publisher occurrences across wiki/sources/*.md."""
    sdir = VAULT / "wiki" / "sources"
    counts: Counter = Counter()
    if not sdir.is_dir():
        return counts
    for p in sdir.rglob("*.md"):   # rglob: sources may be sharded (sources/<year>/<month>/)
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        pub = extract_publisher(txt)
        if pub:
            counts[pub] += 1
    return counts


def looks_like_drift_variant(name: str, canonical: set[str]) -> str | None:
    """Heuristic: if `name` looks like a casing/whitespace variant of an
    existing canonical entry, return that canonical entry. Returns None
    if it's a genuinely new publisher (or an unrecoverable variant)."""
    norm = re.sub(r"[\s./()-]", "", name).lower()
    for c in canonical:
        if norm == re.sub(r"[\s./()-]", "", c).lower():
            return c
    # Substring containment (one-way) — e.g., "Acme Research" → "Acme / Acme Blog"
    for c in canonical:
        c_first = c.split("/")[0].strip().lower()
        if c_first and (c_first in name.lower() or name.lower() in c_first):
            if abs(len(name) - len(c_first)) < 20:  # avoid false-pos on long names
                return c
    return None


def main() -> int:
    canonical = load_canonical_list()
    if not canonical:
        # No canonical publisher list configured (the vault CLAUDE.md has no `**Canonical names**`
        # block) — there is nothing to drain against. A wake-gate must SKIP cleanly here, NOT exit 1:
        # the list is OPTIONAL pack config, so a vault that doesn't curate one (e.g. a persona with no
        # publisher taxonomy) must not error the lane every run — that reads as a fleet failure and
        # feeds a spurious `## Script Error` to the agent. Add a backtick-quoted, comma-separated
        # `**Canonical names**` block to CLAUDE.md to activate the drain.
        print("=== publisher-canonical-drain wake-gate ===")
        print(f"  vault: {VAULT}")
        print("  SKIP: no canonical publisher list in CLAUDE.md (**Canonical names** block absent)")
        print(json.dumps({"wakeAgent": False}))
        return 0
    counts = scan_publishers()

    new_publishers: list[tuple[str, int]] = []
    drift_variants: list[tuple[str, int, str]] = []
    data_quality_flags: list[tuple[str, int]] = []
    canonical_present: list[tuple[str, int]] = []

    for pub, cnt in counts.most_common():
        if pub in canonical:
            canonical_present.append((pub, cnt))
            continue
        if pub in DATA_QUALITY_FLAGS:
            data_quality_flags.append((pub, cnt))
            continue
        if cnt < MIN_SOURCES:
            continue  # below threshold — wait for it to grow
        variant_of = looks_like_drift_variant(pub, canonical)
        if variant_of:
            drift_variants.append((pub, cnt, variant_of))
        else:
            new_publishers.append((pub, cnt))

    print("=== publisher-canonical-drain wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  canonical entries: {len(canonical)}")
    print(f"  unique publishers in sources: {len(counts)}")
    print(f"  threshold: ≥{MIN_SOURCES} sources to be a candidate")
    print()
    print(f"=== NEW canonical candidates: {len(new_publishers)} ===")
    print("Likely-legitimate publishers not in canonical list. Recommended action:")
    print("add to vault CLAUDE.md canonical list AND config/publishers.canonical.json")
    print("with empty variants array. Order by source count, descending.")
    print()
    for pub, cnt in new_publishers[:30]:
        print(f"  - `{pub}` ({cnt} sources)")
    print()
    print(f"=== DRIFT variants: {len(drift_variants)} ===")
    print("Look like variants of existing canonical entries. Recommended action:")
    print("(a) add `<variant>` to the existing canonical's variants array in")
    print("config/publishers.canonical.json, AND (b) rewrite affected source pages'")
    print("`publisher:` field to the canonical form. Verify the variant→canonical")
    print("mapping is correct before applying — bad merges fragment data.")
    print()
    for pub, cnt, canon in drift_variants[:30]:
        print(f"  - `{pub}` ({cnt} sources) → likely canonical: `{canon}`")
    print()
    print(f"=== DATA-QUALITY FLAGS: {len(data_quality_flags)} ===")
    print("Not real publishers — `Unknown` / `TBD` / etc. Surface for HUMAN review;")
    print("DO NOT auto-add to canonical list. The right action is to investigate the")
    print("source pages and fill in the actual publisher (often recoverable from `url:`).")
    print()
    for pub, cnt in data_quality_flags:
        print(f"  - `{pub}` ({cnt} sources)")
    print()

    total_actionable = len(new_publishers) + len(drift_variants)
    wake = total_actionable >= MIN_HITS
    print(json.dumps({"wakeAgent": wake}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
