#!/usr/bin/env python3
"""Wake-gate + delta digest for the trends-refresh cron (okengine#37).

Detects DIRECTIONAL, time-windowed shifts in the corpus: entities whose source-mention
frequency rose sharply over a rolling window vs the immediately-prior window. Emits the top
movers + their recent sources so the agent can synthesize a grounded `trend` page.

A `trend` is distinct from its neighbours:
  - concept  = a TIMELESS cross-cutting pattern.
  - briefing = a POINT-IN-TIME snapshot.
  - trend    = a DATED, DIRECTIONAL observation over a window ("X sightings up 3x in 6 weeks").

Signal = the canonical entity -> `sources:` relationship, dated by each source's `published`
(path `sources/YYYY/MM/` as a fallback). Domain-agnostic: every OKF vault has dated sources +
entities that cite them, so the delta selector is generic (engine-template); only the trend
voice + the `trend` type are pack-supplied.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
WIN_DAYS = int(os.environ.get("TREND_WINDOW_DAYS", "42"))        # rolling window (6 weeks)
MIN_THIS = int(os.environ.get("TREND_MIN_THIS", "3"))           # min recent mentions to qualify
RISE_RATIO = float(os.environ.get("TREND_RISE_RATIO", "2.0"))   # this >= ratio*prior (or prior==0)
TOP_N = int(os.environ.get("TREND_TOP_N", "12"))
MIN_MOVERS_TO_FIRE = int(os.environ.get("TREND_MIN_MOVERS", "3"))

_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
_SRCREF = re.compile(r"sources/([^\]\s|#)]+)")
_PUB = re.compile(r"^published:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.M)
_TYPE = re.compile(r"^type:\s*['\"]?([A-Za-z0-9_.-]+)", re.M)


def _today() -> date:
    override = os.environ.get("TREND_NOW", "").strip()
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()
    return date.today()


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def source_dates() -> dict[str, date]:
    """{slug-relative-to-sources/: published-date} for every source page. Frontmatter
    `published` wins; the `sources/YYYY/MM/` path (mid-month) is the fallback."""
    out: dict[str, date] = {}
    sdir = WIKI / "sources"
    if not sdir.is_dir():
        return out
    for p in sdir.rglob("*.md"):
        if p.name.startswith("_") or p.name == "INDEX.md":
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        m = _PUB.search(txt)
        d = _parse_date(m.group(1)) if m else None
        if d is None:
            mm = re.search(r"(\d{4})/(\d{2})/", p.relative_to(sdir).as_posix())
            if mm:
                try:
                    d = date(int(mm.group(1)), int(mm.group(2)), 15)
                except ValueError:
                    d = None
        if d is not None:
            out[p.relative_to(sdir).with_suffix("").as_posix()] = d
    return out


def scan_entities() -> list[tuple[str, str, list[str]]]:
    """[(slug, type, [source-slugs])] — source refs anywhere in the page (frontmatter + body)."""
    out: list[tuple[str, str, list[str]]] = []
    edir = WIKI / "entities"
    if not edir.is_dir():
        return out
    for p in edir.rglob("*.md"):
        if p.name.startswith("_") or p.name == "INDEX.md":
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        fm = _FM.search(txt)
        head = fm.group(1) if fm else txt[:800]
        tm = _TYPE.search(head)
        refs = {m.group(1).rstrip("/") for m in _SRCREF.finditer(txt) if m.group(1).strip()}
        if refs:
            out.append((p.relative_to(edir).with_suffix("").as_posix(), tm.group(1) if tm else "?", sorted(refs)))
    return out


def main() -> int:
    today = _today()
    this_lo = today - timedelta(days=WIN_DAYS)
    prior_lo = today - timedelta(days=2 * WIN_DAYS)
    sdates = source_dates()
    ents = scan_entities()

    movers = []
    for slug, typ, refs in ents:
        this_c = prior_c = 0
        recent: list[tuple[date, str]] = []
        for r in refs:
            d = sdates.get(r)
            if d is None:
                continue
            if this_lo <= d <= today:
                this_c += 1
                recent.append((d, r))
            elif prior_lo <= d < this_lo:
                prior_c += 1
        if this_c >= MIN_THIS and (prior_c == 0 or this_c >= RISE_RATIO * prior_c):
            ratio = (this_c / prior_c) if prior_c else float("inf")
            movers.append((slug, typ, this_c, prior_c, ratio, sorted(recent, reverse=True)[:4]))

    movers.sort(key=lambda m: (m[4] if m[4] != float("inf") else 1e9, m[2]), reverse=True)
    movers = movers[:TOP_N]

    print("=== trends-refresh wake-gate (okengine#37) ===")
    print(f"  vault: {VAULT}  window: {WIN_DAYS}d (this vs prior {WIN_DAYS}d)")
    print(f"  sources dated: {len(sdates)}  entities scanned: {len(ents)}")
    print(f"  rising movers (>= {MIN_THIS} recent, >= {RISE_RATIO}x prior or new): {len(movers)}")

    if len(movers) < MIN_MOVERS_TO_FIRE:
        print(f"  -> SKIP: {len(movers)} movers below fire threshold ({MIN_MOVERS_TO_FIRE})")
        print(json.dumps({"wakeAgent": False}))
        return 0

    print()
    print("=== rising movers (candidate trends) ===")
    print(
        f"Synthesize 1-3 grounded `trend` pages in wiki/trends/ from the DIRECTIONAL shifts below "
        f"— each over the ~{WIN_DAYS // 7}-week window, with [[wikilinks]] to the entities + their "
        f"recent sources, a window/period, and a direction. A trend is a DATED, DIRECTIONAL "
        f"observation, NOT a timeless concept or a daily snapshot.\n"
    )
    for slug, typ, this_c, prior_c, ratio, recent in movers:
        rs = f"{ratio:.1f}x" if ratio != float("inf") else "new (no prior)"
        print(f"## `[[entities/{slug}]]` ({typ}) — {this_c} recent vs {prior_c} prior ({rs})")
        for d, r in recent:
            print(f"    - {d.isoformat()}  [[sources/{r}]]")
        print()

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
