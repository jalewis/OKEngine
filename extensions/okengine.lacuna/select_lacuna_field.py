#!/usr/bin/env python3
"""okengine.lacuna wake-gate (okengine#145).

Picks ONE field to run the 6-step lacuna procedure over, from the vault's REAL concept graph
— defeating the raw prompt's main weakness (mapping the field from a model's averaged recall).

A "field" is a concept cluster: the set of pages that link `[[concepts/<slug>]]`. Its
**density** (distinct referencing pages) is the measurable signal step 6 needs — a thick
fabric means the geometry is well-sampled (a gap there is a strong inference), a thin patch
means extrapolation. We surface only clusters dense enough to map (`min_density`) and not
already analyzed within `reanalyze_days` (rotation across the graph), densest first.

Prints a human-readable digest (with each cluster's density + namespace breakdown, so the
agent can record `surround_density`), then a final `{"wakeAgent": bool}` line (the cron-plus
wake-gate protocol). LOCAL-ONLY; no writes here — the agent writes the `lacuna/<slug>` page
(and any prediction candidate) via the okengine-write MCP path.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
MIN_DENSITY = int(os.environ.get("OKENGINE_LACUNA_MIN_DENSITY", "8"))      # config.min_density
REANALYZE_DAYS = int(os.environ.get("OKENGINE_LACUNA_REANALYZE_DAYS", "90"))  # config.reanalyze_days
BATCH = int(os.environ.get("OKENGINE_LACUNA_BATCH_SIZE", "3"))            # config.batch_size

# Match `[[concepts/<slug>]]` AND sharded `[[concepts/<shard>/.../<slug>]]` (vaults shard a large
# namespace by leading char, e.g. concepts/s/supply-chain-compromise), capturing the final slug so
# both link forms fold into one cluster. (okengine#145 follow-up — flat-only regex missed sharding.)
_CONCEPT_LINK = re.compile(r"\[\[concepts/(?:[a-z0-9][a-z0-9-]*/)*([a-z0-9][a-z0-9-]*)(?:[#|][^\]]*)?\]\]")
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _today() -> str:
    return os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat()


def _cutoff() -> str:
    return (date.fromisoformat(_today()) - timedelta(days=REANALYZE_DAYS)).isoformat()


def _ns(md: Path) -> str:
    """The top-level wiki namespace a page lives in (entities/sources/concepts/…)."""
    rel = md.relative_to(WIKI).parts
    return rel[0] if len(rel) > 1 else ""


def _read_fm(md: Path) -> dict:
    try:
        text = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    m = _FM.match(text)
    if not m:
        return {}
    try:
        import yaml
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _recently_analyzed() -> set[str]:
    """Concept slugs covered by a `lacuna` page dated within REANALYZE_DAYS (or undated —
    treated as recent, so we don't churn). A lacuna page declares the field it analyzed via
    the `[[concepts/<slug>]]` links in its frontmatter/body; we read them all back."""
    cutoff = _cutoff()
    covered: set[str] = set()
    ldir = WIKI / "lacuna"
    if not ldir.is_dir():
        return covered
    for md in ldir.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _read_fm(md)
        if str(fm.get("type", "")).strip() != "lacuna":
            continue
        when = str(fm.get("updated") or fm.get("created") or "")[:10]
        if when and when < cutoff:
            continue                       # old enough to refresh — leave eligible
        covered |= set(_CONCEPT_LINK.findall(text))
    return covered


def _clusters() -> tuple[dict[str, set[str]], dict[str, Counter]]:
    """Walk the vault once. Returns (slug -> referencing page rels, slug -> namespace counts),
    counting a concept only from pages OUTSIDE the lacuna namespace and not the concept's own
    page (neither should seed the field it belongs to)."""
    refs: dict[str, set[str]] = defaultdict(set)
    by_ns: dict[str, Counter] = defaultdict(Counter)
    for md in WIKI.rglob("*.md"):
        ns = _ns(md)
        if ns == "lacuna":
            continue                       # a lacuna page's see_also shouldn't seed a cluster
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = md.relative_to(WIKI).as_posix()
        for slug in set(_CONCEPT_LINK.findall(text)):
            if ns == "concepts" and md.stem == slug:
                continue                   # the concept page doesn't count toward its own density
            refs[slug].add(rel)
            by_ns[slug][ns or "(root)"] += 1
    return refs, by_ns


def _has_concept_page(slug: str) -> bool:
    return any((WIKI / "concepts").rglob(f"{slug}.md")) if (WIKI / "concepts").is_dir() else False


def _density_str(counts: Counter) -> str:
    """e.g. '23 links · entities 18 · sources 4 · concepts 1' — the surround_density signal."""
    total = sum(counts.values())
    parts = [f"{ns} {n}" for ns, n in counts.most_common()]
    return f"{total} links · " + " · ".join(parts)


def main() -> int:
    if not WIKI.is_dir():
        print(json.dumps({"wakeAgent": False}))
        return 0

    refs, by_ns = _clusters()
    recently = _recently_analyzed()

    cands = []
    for slug, pages in refs.items():
        density = len(pages)
        if density < MIN_DENSITY:
            continue
        if slug in recently:
            continue
        if not _has_concept_page(slug):
            continue                       # require a real, named field to map (not a dangling link)
        cands.append((density, slug))
    cands.sort(key=lambda c: (-c[0], c[1]))

    print("=== lacuna field-selection wake-gate ===")
    print(f"  vault: {WIKI}")
    print(f"  concept clusters >= {MIN_DENSITY} refs: "
          f"{sum(1 for s, p in refs.items() if len(p) >= MIN_DENSITY and _has_concept_page(s))}")
    print(f"  excluded (analyzed since {_cutoff()}): {len(recently)}")
    print(f"  eligible fields: {len(cands)}")

    if not cands:
        print("  → SKIP: no dense, unanalyzed field to map")
        print(json.dumps({"wakeAgent": False}))
        return 0

    chosen = cands[:BATCH]
    print(f"  batch: {len(chosen)} of {len(cands)}\n")
    print("=== fields to analyze ===")
    print("Run the 6-step lacuna procedure over ONE of the fields below (the densest you can "
          "genuinely map). Record the cluster's density as `surround_density`. DEFER the whole "
          "field if you cannot name a real force keeping a cell empty — coverage is not a goal.\n")
    for i, (density, slug) in enumerate(chosen, 1):
        print(f"## {i}. concept: {slug}  ({density} referencing pages)")
        print(f"  field anchor: `[[concepts/{slug}]]`  ·  surround_density: `{_density_str(by_ns[slug])}`\n")

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
