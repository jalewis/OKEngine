#!/usr/bin/env python3
"""select_daily_brief.py — generic what-changed digest for the daily-brief lane.
Engine-template tier (okengine#169 class 1): the engine ships THIS selector and the
schedule; the PACK ships the brief's voice via engine-template-prompts.json. The
daily-brief clone was functionally identical across five packs and every fix landed
in exactly one of them — this consolidates the mechanism so a fix lands once.
Genuinely custom briefs (bespoke selectors/machinery) stay domain lanes; a pack
opts into THIS lane by supplying a `daily-brief` prompt, else the stub is skipped.

Digest (domain-agnostic, drawn from conventions the engine owns):
  1. sources ingested in the window (published/ingested, path-bounded scan)
  2. entities/concepts created or updated in the window
  3. open predictions resolving within BRIEF_DUE_DAYS
  4. completeness gaps opened in the window (when the extension is enabled)
Always wakes when the vault has ANY window activity or due predictions; a genuinely
empty window still wakes (a "quiet day" brief IS the daily product) unless the vault
itself is empty (fresh install -> silent, nothing to brief).

Env: WIKI_PATH (/opt/vault) · BRIEF_WINDOW_HOURS (24) · BRIEF_MAX_ITEMS (15) ·
     BRIEF_DUE_DAYS (7)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
WINDOW_H = int(os.environ.get("BRIEF_WINDOW_HOURS", "24"))
MAX_ITEMS = int(os.environ.get("BRIEF_MAX_ITEMS", "15"))
DUE_DAYS = int(os.environ.get("BRIEF_DUE_DAYS", "7"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def _fm(p: Path) -> dict:
    try:
        m = _FM.match(p.read_text(encoding="utf-8", errors="replace")[:4000])
        d = yaml.safe_load(m.group(1)) if m else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _d(v) -> str:
    return str(v or "")[:10]


def _open_prediction_values() -> set:
    """Statuses that count a prediction OPEN — read from the schema's
    tier.namespaces.predictions.open_values (the single config-driven contract tier_lib and
    build_hot_set already consume), not a bare literal that silently forks (invariant-audit L1).
    Defaults to {open, active}."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import schema_lib
        nsc = (((schema_lib.merged_schema(VAULT, "predictions").get("tier") or {})
                .get("namespaces") or {}).get("predictions") or {})
        ov = nsc.get("open_values")
        if ov:
            return {str(v).lower() for v in ov}
    except Exception:
        pass
    return {"open", "active"}


def main() -> int:
    open_vals = _open_prediction_values()
    if not WIKI.is_dir() or not any(WIKI.rglob("*.md")):
        print("# empty vault — nothing to brief (fresh install)")
        print(json.dumps({"wakeAgent": False}))
        return 0
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=WINDOW_H)).strftime("%Y-%m-%d")
    due_by = (date.today() + timedelta(days=DUE_DAYS)).isoformat()

    # 1. fresh sources — path-bounded: current + previous month dirs
    srcs = []
    for base in {(now.year, now.month),
                 ((now - timedelta(days=27)).year, (now - timedelta(days=27)).month)}:
        d = WIKI / "sources" / f"{base[0]:04d}" / f"{base[1]:02d}"
        if d.is_dir():
            for p in d.rglob("*.md"):
                if p.name.startswith(("_", "INDEX")):
                    continue
                fm = _fm(p)
                pd = _d(fm.get("published") or fm.get("ingested"))
                if pd >= since:
                    srcs.append((pd, p.relative_to(WIKI).as_posix()[:-3],
                                 str(fm.get("title") or p.stem)[:90]))
    srcs.sort(reverse=True)

    # 2. entity/concept movement
    moved = []
    for ns in ("entities", "concepts"):
        d = WIKI / ns
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            if p.name.startswith(("_", "INDEX")):
                continue
            fm = _fm(p)
            # OKF's envelope carries `last_updated`, not `updated` — reading only `updated` left the
            # movement section permanently empty. Mirror tier_lib's fallback chain.
            up = _d(fm.get("updated") or fm.get("last_updated") or fm.get("created"))
            if up >= since:
                kind = "new" if _d(fm.get("created")) >= since else "updated"
                moved.append((up, kind, p.relative_to(WIKI).as_posix()[:-3],
                              str(fm.get("type") or "")))
    moved.sort(reverse=True)

    # 3. predictions due soon
    due = []
    pdir = WIKI / "predictions"
    if pdir.is_dir():
        for p in pdir.rglob("*.md"):
            if p.name.startswith(("_", "INDEX")):
                continue
            fm = _fm(p)
            if str(fm.get("status") or "open").lower() in open_vals:
                rb = _d(fm.get("resolves_by"))
                if rb and rb <= due_by:
                    due.append((rb, p.relative_to(WIKI).as_posix()[:-3],
                                str(fm.get("confidence") or "")))
    due.sort()

    # 4. fresh completeness gaps
    gaps = []
    gdir = WIKI / "gaps"
    if gdir.is_dir():
        for p in gdir.rglob("*.md"):
            if p.name.startswith(("_", "INDEX")):
                continue
            fm = _fm(p)
            if str(fm.get("status")) == "open" and _d(fm.get("first_seen")) >= since:
                gaps.append((str(fm.get("severity") or ""), str(fm.get("rule") or ""),
                             str(fm.get("subject") or "")))

    print(f"=== daily-brief digest (engine-template, okengine#169) — window {WINDOW_H}h ===")
    print(f"  sources: {len(srcs)} · entity/concept movement: {len(moved)} · "
          f"predictions due ≤{DUE_DAYS}d: {len(due)} · new gaps: {len(gaps)}")
    print()
    if srcs:
        print(f"## Fresh sources ({len(srcs)}, showing {min(len(srcs), MAX_ITEMS)})")
        for pd, rel, title in srcs[:MAX_ITEMS]:
            print(f"  - {pd}  [[{rel}]] — {title}")
        print()
    if moved:
        print(f"## Entity/concept movement ({len(moved)}, showing {min(len(moved), MAX_ITEMS)})")
        for up, kind, rel, t in moved[:MAX_ITEMS]:
            print(f"  - {up}  {kind}  [[{rel}]] ({t})")
        print()
    if due:
        print(f"## Predictions resolving ≤{DUE_DAYS}d ({len(due)})")
        for rb, rel, conf in due[:MAX_ITEMS]:
            print(f"  - resolves {rb}  [[{rel}]] (confidence {conf})")
        print()
    if gaps:
        print(f"## New completeness gaps ({len(gaps)})")
        for sev, rule, subj in gaps[:MAX_ITEMS]:
            print(f"  - {sev}  `{rule}`  [[{subj}]]")
        print()
    if not (srcs or moved or due or gaps):
        print("## Quiet window — no fresh sources, movement, due predictions, or new gaps.")
        print("   The brief should say so honestly and stay short.")
        print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
