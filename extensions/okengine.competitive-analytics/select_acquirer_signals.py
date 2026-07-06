#!/usr/bin/env python3
"""Wake-gate + digest for the acquirer-signals op (okengine#146).

Scans RECENT sources for acquisition / movement signals (M&A, stakes, funding, IPO) and surfaces
the matching sources + the entities they involve, so the agent can write/refresh an acquirer-signal
dashboard. Generic: the keyword set is configurable (MOVEMENT_KEYWORDS) with an M&A default; no
watchlist required (this is the market-wide movement view, complementing the watchlist quadrants).
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
LOOKBACK = int(os.environ.get("ACQUIRER_LOOKBACK_DAYS", "30"))
MIN_HITS = int(os.environ.get("ACQUIRER_MIN_HITS", "2"))
_DEFAULT_KW = ("acquire", "acquisition", "acquired", "acquires", "merger", "merges", "buyout",
               "takeover", "majority stake", "minority stake", "ipo", "funding round", "raises",
               "valuation", "spin out", "spin-off", "divest")
KEYWORDS = [k.strip().lower() for k in os.environ.get("MOVEMENT_KEYWORDS", "").split(",") if k.strip()] or list(_DEFAULT_KW)

_PUB = re.compile(r"^published:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.M)
_TITLE = re.compile(r"^title:\s*['\"]?(.+?)['\"]?\s*$", re.M)
_ENT = re.compile(r"\[\[entities/([^\]\|#]+)")


def _today() -> date:
    o = os.environ.get("TREND_NOW", "").strip()
    return datetime.strptime(o, "%Y-%m-%d").date() if o else date.today()


def main() -> int:
    today = _today()
    floor = today - timedelta(days=LOOKBACK)
    sdir = WIKI / "sources"
    hits = []
    if sdir.is_dir():
        for p in sdir.rglob("*.md"):
            if p.name.startswith("_") or p.name == "INDEX.md":
                continue
            txt = p.read_text(errors="replace")
            pm = _PUB.search(txt)
            if not pm:
                continue
            try:
                d = datetime.strptime(pm.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (floor <= d <= today):
                continue
            low = txt.lower()
            matched = [k for k in KEYWORDS if k in low]
            if not matched:
                continue
            tm = _TITLE.search(txt)
            ents = sorted({m.group(1).rstrip("/") for m in _ENT.finditer(txt)})
            hits.append((d, p.relative_to(sdir).with_suffix("").as_posix(),
                         tm.group(1).strip() if tm else p.stem, matched[:3], ents[:6]))

    hits.sort(reverse=True)
    print("=== acquirer-signals wake-gate (okengine#146) ===")
    print(f"  lookback: {LOOKBACK}d  keywords: {len(KEYWORDS)}  matching sources: {len(hits)}")
    if len(hits) < MIN_HITS:
        print(f"  -> SKIP: {len(hits)} movement signals below threshold ({MIN_HITS})")
        print(json.dumps({"wakeAgent": False}))
        return 0

    print()
    print(
        "Write/update the acquirer-signal dashboard at dashboards/competitive/acquirer-signals.md "
        "(frontmatter: type: dashboard, title, updated: <TODAY>) from the movement signals below — "
        "group by acquirer/target where clear, link the entities + sources with [[wikilinks]], and "
        "note what each signal implies for the competitive map. Skip noise (a keyword match that "
        "isn't a real movement). Write via the MCP write path.\n"
    )
    for d, slug, title, kw, ents in hits[:20]:
        print(f"## {d.isoformat()} — {title[:90]}  ({', '.join(kw)})")
        print(f"    source: [[sources/{slug}]]")
        if ents:
            print(f"    entities: {' '.join('[[entities/' + e + ']]' for e in ents)}")
        print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
