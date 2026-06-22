#!/usr/bin/env python3
"""Wake-gate + digest for the orphans-drain cron.

Walks the wiki's knowledge namespaces (schema.yaml
`partitioning.namespaces`; on-disk top-level wiki dirs as a fallback when
the pack declares none), identifies pages with zero inbound `[[wikilinks]]`
from anywhere in the vault (orphans), and surfaces the highest-evidence
candidate referencers per orphan so the agent can decide whether to add a
`related:` cross-link.

Inbound counting matches `lint_watcher.py` exactly so the drain's view
of "orphan" matches the queue depth shown in the daily snapshot:
  - Only pages with a valid YAML-dict frontmatter contribute inbound refs
    (so `index.md`, `overview.md`, `log.md`, lint reports — none of
    which have FM — don't rescue an orphan via curated/operational links).
  - Inbound is counted by final-slug match (`[[entities/foo]]` and
    `[[foo]]` both ping the page with stem `foo`).

Adding the orphan to a candidate's `related:` array IS the cure — the
candidate has a valid FM dict, so the new wikilink counts as a real
inbound reference and removes the page from the orphan queue.

Age filter: orphans created within ORPHAN_MIN_AGE_DAYS (default 7) are
deferred — the ingest/backfill pipelines are still working through fresh
pages and will naturally cross-link them as new sources land. Flagging them
too early generates churn.

Candidate scoring (per orphan):
  - Shared sources/basis (Jaccard over wikilink targets in
    `sources:`/`basis:` arrays) — strongest signal: two pages citing
    the same source page are almost always topically adjacent.
  - Shared tags (Jaccard) — moderate signal; useful tiebreaker when
    sources don't overlap.
  - A candidate whose `subject:` points at the orphan = perfect signal
    (the candidate page is literally about the orphan).

Top 3 candidates per orphan are surfaced; the agent then reads the
candidate's body to confirm topical fit before patching its frontmatter.

Designed batch size of 5 keeps cost bounded and lets the agent do real
prose verification on each candidate (~3 reads per orphan = ~15
reads/run).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
BATCH_SIZE = int(os.environ.get("ORPHAN_BATCH_SIZE", "5"))
MIN_AGE_DAYS = int(os.environ.get("ORPHAN_MIN_AGE_DAYS", "7"))
TOP_CANDIDATES = int(os.environ.get("ORPHAN_TOP_CANDIDATES", "3"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def all_pages():
    """Every .md under wiki/, excluding backup directories and hidden dirs."""
    out = []
    for p in (VAULT / "wiki").rglob("*.md"):
        if not p.is_file():
            continue
        if any(".bak." in part or part.startswith(".") for part in p.parts):
            continue
        out.append(p)
    return out


def parse_fm(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter_dict, body_text). Frontmatter may be None on
    parse failure."""
    m = _FM_RE.match(text)
    if not m:
        return None, text
    body = text[m.end():]
    try:
        fm = yaml.safe_load(m.group(1))
        return (fm if isinstance(fm, dict) else None), body
    except yaml.YAMLError:
        return None, body


def extract_wikilink_targets(value) -> set[str]:
    """Extract `[[target]]` strings from a frontmatter value (string or
    list of strings). Returns the set of target slugs (last path
    segment, .md stripped)."""
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    out = set()
    for item in items:
        if not isinstance(item, str):
            continue
        for wm in _WIKILINK_RE.finditer(item):
            tgt = wm.group(1).strip()
            slug = tgt.split("/")[-1]
            if slug.endswith(".md"):
                slug = slug[:-3]
            out.add(slug)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def crosslink_namespaces() -> set[str]:
    """Knowledge namespaces whose pages are orphan-checked. Schema-driven
    (schema.yaml `partitioning.namespaces`, minus `exclude:` dirs); if the pack
    declares none, fall back to the on-disk top-level wiki dirs (minus excluded
    + dot/underscore dirs). The engine ships no hardcoded namespace list."""
    schema = schema_lib.governing_schema(VAULT)
    excluded = schema_lib.excluded_dirs(schema) | {"operational", "dashboards"}
    names = schema_lib.knowledge_namespaces(schema) - excluded
    if not names:
        wiki = VAULT / "wiki"
        if wiki.is_dir():
            names = {
                d.name for d in wiki.iterdir()
                if d.is_dir()
                and not d.name.startswith((".", "_"))
                and d.name not in excluded
            }
    return names


def main() -> int:
    pages = all_pages()
    if not pages:
        print("ERROR: no pages found in wiki/", file=sys.stderr)
        return 1

    crosslink_types = crosslink_namespaces()

    today = datetime.now(timezone.utc).date()
    age_threshold = today - timedelta(days=MIN_AGE_DAYS)

    # Per-page: parsed frontmatter, body, and feature sets used for scoring
    page_info: dict[str, dict] = {}
    inbound_count: dict[str, int] = defaultdict(int)

    for p in pages:
        rel = str(p.relative_to(VAULT / "wiki"))
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        fm, body = parse_fm(txt)
        page_info[rel] = {
            "path": p,
            "stem": p.stem,
            "fm": fm or {},
            "body": body,
            "txt": txt,
        }
        # Match lint_watcher orphan-counting exactly: skip pages without
        # a valid YAML-dict frontmatter (index.md, overview.md, lint
        # reports, etc. don't contribute inbound refs). This keeps the
        # drain's view aligned with the queue-snapshot metric.
        if fm is None:
            continue
        for wm in _WIKILINK_RE.finditer(txt):
            tgt = wm.group(1).strip()
            slug = tgt.split("/")[-1]
            inbound_count[slug] += 1

    # Identify orphan candidates: knowledge-namespace pages with 0 inbound
    orphans: list[dict] = []
    for rel, info in page_info.items():
        parts = rel.split("/")
        if not parts or parts[0] not in crosslink_types:
            continue
        if inbound_count.get(info["stem"], 0) > 0:
            continue
        # Age filter — orphans younger than threshold are deferred. Accept
        # either a `created:` or a `made_on:` date field (generic fallback).
        date_val = info["fm"].get("created") or info["fm"].get("made_on")
        created_d = None
        if isinstance(date_val, str):
            try:
                created_d = datetime.strptime(date_val[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
        elif hasattr(date_val, "year"):
            created_d = date_val.date() if hasattr(date_val, "date") and callable(getattr(date_val, "date")) else date_val
        if created_d and created_d > age_threshold:
            continue
        orphans.append({
            "rel": rel,
            "info": info,
            "type": parts[0],
            "created": created_d.isoformat() if created_d else "(no date)",
        })

    # Sort orphans: oldest first (longest unrescued — highest signal of needing curation)
    orphans.sort(key=lambda o: o["created"] or "9999")

    # Build feature sets per orphan and per candidate-pool page
    def features(info: dict) -> dict:
        fm = info["fm"]
        srcs = extract_wikilink_targets(fm.get("sources"))
        srcs |= extract_wikilink_targets(fm.get("basis"))
        tags = set()
        if isinstance(fm.get("tags"), list):
            tags = {str(t).lower().strip() for t in fm["tags"] if isinstance(t, (str, int))}
        subject = extract_wikilink_targets(fm.get("subject"))
        return {"sources": srcs, "tags": tags, "subject": subject}

    # Pre-compute features for ALL crosslink-namespace pages (candidate pool)
    pool: dict[str, dict] = {}
    for rel, info in page_info.items():
        parts = rel.split("/")
        if not parts or parts[0] not in crosslink_types:
            continue
        pool[rel] = {"info": info, "features": features(info)}

    # Score candidates per orphan
    def score(orphan_feat: dict, cand_feat: dict, orphan_stem: str) -> tuple[float, list[str]]:
        reasons = []
        s = 0.0
        # A candidate whose `subject:` is the orphan = perfect match
        if orphan_stem in cand_feat["subject"]:
            s += 10.0
            reasons.append("candidate.subject = orphan")
        # Shared sources
        shared_src = orphan_feat["sources"] & cand_feat["sources"]
        if shared_src:
            j = jaccard(orphan_feat["sources"], cand_feat["sources"])
            s += 5.0 * j + len(shared_src) * 0.5
            reasons.append(f"shared sources: {sorted(shared_src)[:3]} (j={j:.2f})")
        # Shared tags
        shared_tags = orphan_feat["tags"] & cand_feat["tags"]
        if shared_tags:
            j = jaccard(orphan_feat["tags"], cand_feat["tags"])
            s += 2.0 * j + len(shared_tags) * 0.2
            reasons.append(f"shared tags: {sorted(shared_tags)[:3]} (j={j:.2f})")
        return s, reasons

    # Build batch
    batch = []
    for orph in orphans[:BATCH_SIZE]:
        orph_feat = features(orph["info"])
        scored: list[tuple[float, str, list[str]]] = []
        for cand_rel, cand in pool.items():
            if cand_rel == orph["rel"]:
                continue
            sc, reasons = score(orph_feat, cand["features"], orph["info"]["stem"])
            if sc > 0:
                scored.append((sc, cand_rel, reasons))
        scored.sort(key=lambda t: -t[0])
        orph["candidates"] = scored[:TOP_CANDIDATES]
        batch.append(orph)

    print("=== orphans-drain wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  total wiki pages scanned: {len(pages)}")
    print(f"  total orphans ({'/'.join(sorted(crosslink_types)) or '(none)'}, 0 inbound, age >={MIN_AGE_DAYS}d): {len(orphans)}")
    print(f"  batch size: {BATCH_SIZE}")
    print()

    print(f"=== batch ({len(batch)} of {len(orphans)}, max {BATCH_SIZE} per run) ===")
    print("Process IN ORDER. For each orphan: read 1-2 candidates' bodies,")
    print("decide if each is genuinely topically related, then either")
    print("(a) patch the candidate's `related:` array to add the orphan, or")
    print("(b) append `## Triage note` to the orphan listing the strongest 2")
    print("candidates and the reason none qualified for cross-link.")
    print()

    for i, orph in enumerate(batch, 1):
        print(f"{i}. `{orph['rel']}` (type={orph['type']}, created={orph['created']})")
        title = orph["info"]["fm"].get("title") or orph["info"]["stem"]
        print(f"   title: {title}")
        feat = features(orph["info"])
        print(f"   sources cited: {len(feat['sources'])}, tags: {sorted(feat['tags'])[:5]}")
        if not orph["candidates"]:
            print(f"   candidates: (none with overlapping sources or tags) — likely TRIAGE candidate")
        else:
            print(f"   top {len(orph['candidates'])} candidates:")
            for sc, cand_rel, reasons in orph["candidates"]:
                print(f"     - {cand_rel}  score={sc:.2f}")
                for r in reasons:
                    print(f"         {r}")
        print()

    if len(orphans) > len(batch):
        print(f"=== long-tail: {len(orphans) - len(batch)} additional orphans deferred ===")
        print("(processed in subsequent runs at 4 fires/day — queue drains in ~5d)")
        print()

    wake = bool(batch)
    print(json.dumps({"wakeAgent": wake}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
