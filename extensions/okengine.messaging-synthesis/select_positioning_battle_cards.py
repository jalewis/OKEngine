#!/usr/bin/env python3
"""Wake-gate for messaging-synthesis's positioning-battle-cards op (ported from the origin system's
sector-competitor-battle-cards). Writes/refreshes an "us vs them" card per (competitor, segment)
in the configured product's watchlist — distinct from okengine.competitive-analytics's cards,
which compare competitors to EACH OTHER, not to a product this vault doesn't otherwise track.

Drift-gated: wakes only for a (competitor, segment) pair whose competitor entity has newer
activity than its existing card. Silent (no product configured) unless PRODUCT_ANCHOR_PATH
names one — see msg_lib.read_anchor.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msg_lib import VAULT, WIKI, page_summary, read_anchor, watchlist_segments

CARDS_DIR = WIKI / "briefings"
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
# Bounded per-run batch (matches raw-backfill / lacuna's batch_size convention): a first-ever
# run against a large watchlist can find every competitor "stale" (no card exists yet), which
# would otherwise ask one agent turn-budget to write 15+ full cards in a single session. Drain
# the backlog over successive daily runs instead of forcing it all into one.
BATCH_SIZE = int(os.environ.get("POSITIONING_BATCH_SIZE", "5"))


def _card_meta(competitor: str, segment: str) -> "tuple[str | None, float | None]":
    """(card `updated` date, card file mtime) for an existing card — (None, None) if no card yet.
    The mtime gives intra-day granularity the date string lacks (#326 [16])."""
    p = CARDS_DIR / f"positioning-{segment}-{competitor}.md"
    if not p.is_file():
        return None, None
    mtime = p.stat().st_mtime
    m = _FM.match(p.read_text(errors="replace"))
    if not m:
        return None, mtime
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None, mtime
    return (fm.get("updated") or fm.get("last_updated")), mtime


def _is_stale(comp: dict, competitor: str, segment: str) -> bool:
    """A card is stale when no card exists, the competitor's `updated` date is strictly newer, OR —
    on the SAME date — the competitor page file was modified after the card was written. The old
    date-only string compare missed a same-day competitor edit made after the card (#326 [16]); the
    mtime tiebreak mirrors the messaging-synthesis sibling's fix (invariant-audit #18)."""
    card_updated, card_mtime = _card_meta(competitor, segment)
    if card_updated is None:
        return True
    comp_updated = str(comp["updated"])
    if comp_updated > str(card_updated):
        return True
    if comp_updated == str(card_updated) and card_mtime is not None:
        comp_path = comp.get("path")
        return bool(comp_path and os.path.getmtime(comp_path) > card_mtime)
    return False


def main() -> int:
    anchor = read_anchor()
    if not anchor:
        print("# no product configured (PRODUCT_ANCHOR_PATH absent) — positioning-battle-cards "
              "stays silent; see README for the config format")
        print(json.dumps({"wakeAgent": False}))
        return 0

    product = anchor.get("product_name", "the product")
    segments = watchlist_segments(anchor.get("watchlist_segments"))
    if not segments:
        print(f"# {product}'s anchor names no watchlist_segments (or the watchlist itself is "
              "absent) — nothing to build cards against")
        print(json.dumps({"wakeAgent": False}))
        return 0

    stale = []
    for seg_key, seg in segments.items():
        for comp_slug in seg.get("competitors") or []:
            comp = page_summary(comp_slug)
            if not comp.get("found") or not comp.get("updated"):
                continue
            if _is_stale(comp, comp_slug.split("/")[-1], seg_key):
                stale.append((seg_key, comp_slug, comp))

    if not stale:
        print(f"# no watchlist competitor has newer activity than its existing card for {product}")
        print(json.dumps({"wakeAgent": False}))
        return 0

    total_stale = len(stale)
    stale = stale[:BATCH_SIZE]

    print("=== positioning-battle-cards wake-gate ===")
    print(f"  product: {product}  |  {total_stale} card(s) need refresh (competitor activity is "
          f"newer than the existing card, or no card exists yet) — this run covers "
          f"{len(stale)} (batch size {BATCH_SIZE}); the rest drain on subsequent runs")
    print()
    capability_pages = anchor.get("capability_pages") or []
    print(f"  our capability anchors (a claimed wedge MUST be visible on one of these, or drop "
          f"it): {capability_pages}")
    for p in capability_pages:
        s = page_summary(p)
        print(f"  --- [[{p}]] found={s.get('found')} ---")
        for a in s.get("activity", []):
            print(f"    - {a}")
    print()
    for seg_key, comp_slug, comp in stale:
        card_path = f"briefings/positioning-{seg_key}-{comp_slug.split('/')[-1]}"
        print(f"--- segment={seg_key} competitor=[[{comp_slug}]] -> write to {card_path} ---")
        print(f"  title={comp.get('title')} updated={comp.get('updated')}")
        for a in comp.get("activity", []):
            print(f"    - {a}")
        print()
    print("  frontmatter per card: type: battle-card, title: \"<Competitor> vs "
          f"{product} — <segment>\", published: <today>, updated: <today>")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
