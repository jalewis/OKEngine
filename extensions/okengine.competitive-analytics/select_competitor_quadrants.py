#!/usr/bin/env python3
"""Wake-gate + digest for the competitor-quadrants op (okengine#146).

For each watchlist segment, marshal its competitor entities + their data so the agent can position
them on the segment's two axes and write a quadrant dashboard. The watchlist is PACK config
(WATCHLIST_PATH) — no seeds ship here; an empty/absent watchlist is a clean SKIP.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from comp_lib import entity_summary, read_watchlist, watchlist_path  # noqa: E402


def main() -> int:
    wl = read_watchlist()
    segments = wl.get("segments") or {}
    print("=== competitor-quadrants wake-gate (okengine#146) ===")
    print(f"  watchlist: {watchlist_path()}")
    print(f"  segments: {len(segments)}")
    if not segments:
        print("  -> SKIP: no watchlist segments (set WATCHLIST_PATH; the extension ships none)")
        print(json.dumps({"wakeAgent": False}))
        return 0

    print()
    print(
        "For each segment below, write/update a quadrant dashboard at "
        "dashboards/competitive/quadrant-<segment>.md (frontmatter: type: dashboard, title, "
        "updated: <TODAY>). Position each competitor on the segment's TWO axes from its entity data + "
        "recent activity, with [[wikilinks]] to every competitor. Be evidence-grounded (cite the "
        "activity); call out movers vs the prior quadrant. Write via the MCP write path.\n"
    )
    for seg, d in segments.items():
        comps = d.get("competitors") or []
        axes = d.get("axes") or {}
        label = d.get("label", seg)
        print(f"## segment `{seg}` — {label} · axes: x={axes.get('x', '?')}  y={axes.get('y', '?')} "
              f"({len(comps)} competitors)")
        for c in comps:
            s = entity_summary(c)
            if not s["found"]:
                print(f"  - `{c}` — NO entity page yet (note the gap; don't fabricate a position)")
                continue
            print(f"  - `[[entities/{s['slug']}]]` ({s.get('type')}) updated={s.get('updated')}")
            for a in s["activity"]:
                print(f"      · {a}")
        print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
