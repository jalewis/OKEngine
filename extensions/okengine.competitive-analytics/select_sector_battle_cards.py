#!/usr/bin/env python3
"""Wake-gate + digest for the sector-battle-cards op (okengine#146).

For each watchlist segment, marshal its competitors so the agent can write head-to-head battle
cards (positioning / strengths / weaknesses / differentiators). Watchlist is PACK config.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from comp_lib import entity_summary, read_watchlist, watchlist_path  # noqa: E402


def main() -> int:
    wl = read_watchlist()
    segments = wl.get("segments") or {}
    print("=== sector-battle-cards wake-gate (okengine#146) ===")
    print(f"  watchlist: {watchlist_path()}  segments: {len(segments)}")
    if not segments:
        print("  -> SKIP: no watchlist segments (set WATCHLIST_PATH; the extension ships none)")
        print(json.dumps({"wakeAgent": False}))
        return 0

    print()
    print(
        "For each segment below, write/update a battle-card dashboard at "
        "dashboards/competitive/battle-cards-<segment>.md (frontmatter: type: dashboard, title, "
        "updated: <TODAY>). One card per competitor: positioning, key strengths, weaknesses/gaps, "
        "and the differentiator vs the segment — grounded in the entity data + recent activity, with "
        "[[wikilinks]]. Skip a competitor with no evidence rather than inventing one. Write via the "
        "MCP write path.\n"
    )
    for seg, d in segments.items():
        comps = d.get("competitors") or []
        print(f"## segment `{seg}` — {d.get('label', seg)} ({len(comps)} competitors)")
        for c in comps:
            s = entity_summary(c, max_activity=4)
            if not s["found"]:
                print(f"  - `{c}` — no entity page yet")
                continue
            print(f"  - `[[entities/{s['slug']}]]` ({s.get('type')})")
            for a in s["activity"]:
                print(f"      · {a}")
        print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
