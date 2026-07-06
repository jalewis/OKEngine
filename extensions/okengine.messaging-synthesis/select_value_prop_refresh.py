#!/usr/bin/env python3
"""Wake-gate for messaging-synthesis's value-prop-gap-refresh op (ported from the origin system's
value-prop-gap-refresh). Re-runs the value-prop gap analysis: where does the configured
product's capability surface fall short against watchlist competitors' recent moves.

Wake condition (ported verbatim from the origin system): >=3 new competitor sources in the past 14
days, OR >=28 days since the last snapshot. Silent (no product configured) unless
PRODUCT_ANCHOR_PATH names one — see msg_lib.read_anchor.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msg_lib import VAULT, WIKI, page_summary, read_anchor, slug_mention_pattern, watchlist_segments

LOOKBACK_DAYS = int(os.environ.get("VALUE_PROP_LOOKBACK_DAYS", "14"))
MIN_NEW_SIGNALS = int(os.environ.get("VALUE_PROP_MIN_NEW_SIGNALS", "3"))
MAX_AGE_DAYS = int(os.environ.get("VALUE_PROP_MAX_AGE_DAYS", "28"))
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _latest_snapshot_age_days() -> "int | None":
    snaps = sorted(glob.glob(str(WIKI / "briefings" / "value-prop-gaps-*.md")), reverse=True)
    if not snaps:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(snaps[0]).name)
    if not m:
        return None
    return (date.today() - date.fromisoformat(m.group(1))).days


def _new_competitor_source_count(competitor_slugs: set[str], since: date) -> int:
    n = 0
    for p in glob.glob(str(WIKI / "sources" / "**" / "*.md"), recursive=True):
        try:
            txt = Path(p).read_text(errors="replace")
        except OSError:
            continue
        m = _FM.match(txt)
        if not m:
            continue
        import yaml
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            continue
        pub = fm.get("published")
        try:
            if not pub or date.fromisoformat(str(pub)[:10]) < since:
                continue
        except (ValueError, TypeError):
            continue
        body_l = txt.lower()
        if any(slug_mention_pattern(s).search(body_l) for s in competitor_slugs):
            n += 1
    return n


def main() -> int:
    anchor = read_anchor()
    if not anchor:
        print("# no product configured (PRODUCT_ANCHOR_PATH absent) — value-prop-gap-refresh "
              "stays silent; see README for the config format")
        print(json.dumps({"wakeAgent": False}))
        return 0

    product = anchor.get("product_name", "the product")
    segments = watchlist_segments(anchor.get("watchlist_segments"))
    competitor_slugs = {c for seg in segments.values() for c in (seg.get("competitors") or [])}

    since = date.today() - timedelta(days=LOOKBACK_DAYS)
    new_signals = _new_competitor_source_count(competitor_slugs, since) if competitor_slugs else 0
    age = _latest_snapshot_age_days()

    if new_signals < MIN_NEW_SIGNALS and (age is not None and age < MAX_AGE_DAYS):
        print(f"# quiet: only {new_signals} new competitor source(s) in {LOOKBACK_DAYS}d "
              f"(need {MIN_NEW_SIGNALS}), snapshot is {age}d old (max {MAX_AGE_DAYS})")
        print(json.dumps({"wakeAgent": False}))
        return 0

    today = date.today().isoformat()
    out_path = f"briefings/value-prop-gaps-{today}"
    print("=== value-prop-gap-refresh wake-gate ===")
    print(f"  product: {product}  |  {new_signals} new competitor source(s) in {LOOKBACK_DAYS}d, "
          f"prior snapshot age: {age if age is not None else 'none yet'}")
    print(f"  write via mcp_okengine_write_create_entity to: {out_path}")
    print(f"  frontmatter: type: value-prop-snapshot, title: \"Value-prop gap snapshot — "
          f"{today}\", published: {today}, updated: {today}")
    print()
    print("  our capability anchors:")
    for p in anchor.get("capability_pages") or []:
        s = page_summary(p)
        print(f"  --- [[{p}]] found={s.get('found')} ---")
        for a in s.get("activity", []):
            print(f"    - {a}")
    print()
    print("  watchlist competitors (recent moves to gap-check against):")
    for seg_key, seg in segments.items():
        for comp_slug in seg.get("competitors") or []:
            c = page_summary(comp_slug)
            if c.get("found"):
                print(f"  --- [{seg_key}] [[{comp_slug}]] updated={c.get('updated')} ---")
                for a in c.get("activity", []):
                    print(f"    - {a}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
