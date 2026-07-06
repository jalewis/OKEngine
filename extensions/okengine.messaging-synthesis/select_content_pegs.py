#!/usr/bin/env python3
"""Wake-gate for messaging-synthesis's content-pegs op (ported from the origin system's
weekly-content-pegs). Turns emerging market signals from the past week into outbound content
angles (blog/LinkedIn/podcast/newsletter) anchored to the configured product.

Silent (no product configured) unless PRODUCT_ANCHOR_PATH names one — see msg_lib.read_anchor.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msg_lib import VAULT, WIKI, page_summary, read_anchor, slug_mention_pattern, watchlist_segments

LOOKBACK_DAYS = int(os.environ.get("CONTENT_PEGS_LOOKBACK_DAYS", "7"))
_FM = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)


def _split(p: Path) -> tuple[dict, str]:
    try:
        t = p.read_text(errors="replace")
    except OSError:
        return {}, ""
    m = _FM.match(t)
    if not m:
        return {}, t.strip()
    fm: dict = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^(\w+):\s*(.*)$", line)
        if mm:
            fm[mm.group(1)] = mm.group(2).strip().strip("'\"")
    return fm, m.group(2).strip()


def _today():
    """Today, overridable via CONTENT_PEGS_NOW (ISO date) so the lane is deterministic/testable and
    reproducible for a backfill — else a fixed-date test time-bombs once now drifts past the window."""
    import os
    from datetime import date
    ov = os.environ.get("CONTENT_PEGS_NOW")
    return date.fromisoformat(ov) if ov else date.today()


def main() -> int:
    anchor = read_anchor()
    if not anchor:
        print("# no product configured (PRODUCT_ANCHOR_PATH absent) — content-pegs stays "
              "silent; see README for the config format")
        print(json.dumps({"wakeAgent": False}))
        return 0

    product = anchor.get("product_name", "the product")
    segments = watchlist_segments(anchor.get("watchlist_segments"))
    since = _today() - timedelta(days=LOOKBACK_DAYS)

    watched_slugs = {c for seg in segments.values() for c in (seg.get("competitors") or [])}
    hits = []
    for p in glob.glob(str(WIKI / "sources" / "**" / "*.md"), recursive=True):
        path = Path(p)
        fm, body = _split(path)
        if fm.get("type") != "source":
            continue
        pub = fm.get("published")
        try:
            if not pub or date.fromisoformat(str(pub)[:10]) < since:
                continue
        except ValueError:
            continue
        haystack = (fm.get("title", "") + "\n" + body).lower()
        if watched_slugs and not any(slug_mention_pattern(s).search(haystack) for s in watched_slugs):
            continue
        hits.append((path, fm, body))

    if not hits:
        print(f"# no watchlist-relevant sources published since {since.isoformat()} for {product}")
        print(json.dumps({"wakeAgent": False}))
        return 0

    week_ending = _today().isoformat()
    out_path = f"briefings/content-pegs-{week_ending}"
    print("=== content-pegs wake-gate ===")
    print(f"  product: {product}  |  window: published since {since.isoformat()}")
    print(f"  {len(hits)} candidate source(s) — turn the strongest 3-7 into outbound content "
          "angles (blog/LinkedIn/podcast/newsletter); skip the rest")
    print(f"  write via mcp_okengine_write_create_entity to: {out_path}")
    print(f"  frontmatter: type: marketing-pulse, title: \"Content pegs — week of "
          f"{week_ending}\", published: {week_ending}, updated: {week_ending}")
    print()
    for p, fm, body in hits[:30]:
        rel = p.relative_to(WIKI).with_suffix("").as_posix()
        print(f"--- [[{rel}]] --- {fm.get('title', '?')!r} published={fm.get('published')}")
        print(body[:400])
        print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
