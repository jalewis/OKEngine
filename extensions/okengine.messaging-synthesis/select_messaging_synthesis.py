#!/usr/bin/env python3
"""Wake-gate for the messaging-synthesis meta-layer op (ported from the origin system's
messaging-synthesis). Sits on top of the other 3 ops in this extension (content-pegs,
positioning-battle-cards, value-prop-gap-refresh): synthesizes across whichever of them have
changed since the last messaging brief into one "what should our messaging be" recommendation.

Drift-gated: wakes only if content-pegs / a battle-card / the value-prop snapshot is newer than
the most recent messaging brief (or no brief exists yet). Silent (no product configured) unless
PRODUCT_ANCHOR_PATH names one — see msg_lib.read_anchor.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msg_lib import VAULT, WIKI, page_summary, read_anchor

BRIEFS_DIR = WIKI / "briefings"
_FM = re.compile(r"\A---\s*\n(.*?\n)---\s*\n?(.*)\Z", re.DOTALL)


def _split(p: Path) -> tuple[dict, str]:
    try:
        t = p.read_text(errors="replace")
    except OSError:
        return {}, ""
    m = _FM.match(t)
    if not m:
        return {}, t.strip()
    import yaml
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return fm, m.group(2).strip()


def _latest(glob_pat: str) -> "tuple[Path, dict, str] | None":
    matches = sorted(glob.glob(str(BRIEFS_DIR / glob_pat)), reverse=True)
    if not matches:
        return None
    p = Path(matches[0])
    fm, body = _split(p)
    return p, fm, body


def main() -> int:
    anchor = read_anchor()
    if not anchor:
        print("# no product configured (PRODUCT_ANCHOR_PATH absent) — messaging-synthesis "
              "stays silent; see README for the config format")
        print(json.dumps({"wakeAgent": False}))
        return 0

    product = anchor.get("product_name", "the product")
    prior = _latest("messaging-brief-*.md")
    # Compare file MTIMEs, not the date-only `updated` frontmatter: prior_updated is the extension's
    # OWN brief, which the daily floor regenerates every day, so an upstream artifact written LATER
    # the same day (the routine @morning:0 battle-card vs @morning:15 brief stagger) compares
    # equal-date under `str(updated) > str(prior_updated)` and was NEVER detected as a delta — that
    # day or ever (invariant-audit #18). mtime has intra-day granularity, so 'newer than the last
    # brief' is expressible.
    def _mtime(path) -> float:
        try:
            return os.path.getmtime(str(path))
        except OSError:
            return 0.0
    prior_mtime = _mtime(prior[0]) if prior else None

    inputs = {
        "content-pegs": _latest("content-pegs-*.md"),
        "value-prop-snapshot": _latest("value-prop-gaps-*.md"),
    }
    battle_cards = sorted(glob.glob(str(BRIEFS_DIR / "positioning-*.md")), reverse=True)

    deltas = []
    for name, hit in inputs.items():
        if hit is None:
            continue
        p, fm, _ = hit
        if prior_mtime is None or _mtime(p) > prior_mtime:
            deltas.append((name, p))
    for bc in battle_cards:
        if prior_mtime is None or _mtime(bc) > prior_mtime:
            deltas.append((f"battle-card:{Path(bc).stem}", Path(bc)))

    from datetime import date
    today = date.today().isoformat()
    todays = glob.glob(str(BRIEFS_DIR / f"messaging-brief-{today}.md"))
    drift_only = os.environ.get("MESSAGING_DRIFT_ONLY", "").strip().lower() in ("1", "true", "yes")

    if not deltas:
        if todays:
            print(f"# today's messaging brief already exists ({today}) — not regenerating")
            print(json.dumps({"wakeAgent": False}))
            return 0
        if drift_only:
            print(f"# no upstream artifact newer than the last brief for {product}, and "
                  "MESSAGING_DRIFT_ONLY is set — staying silent")
            print(json.dumps({"wakeAgent": False}))
            return 0
        # DAILY FLOOR (okengine#177): a reader-facing daily brief must run daily. With no
        # delta but no brief yet today, produce a STEADY-STATE brief (reaffirm current
        # messaging, note no material change) — so a MISSING brief can only mean a broken
        # pipeline, never a skipped gate. Opt out with MESSAGING_DRIFT_ONLY=1.
        # BUT only when there's something to reaffirm: a brand-new EMPTY vault (no prior
        # brief, no anchor capability pages, no upstream artifacts) has nothing to say and
        # is legitimately silent — that's a fresh deployment, not a broken one.
        has_material = bool(prior) or bool(anchor.get("capability_pages")) \
            or bool(battle_cards) or any(v is not None for v in inputs.values())
        if not has_material:
            print("# empty vault — no prior brief, anchor pages, or marketing artifacts to "
                  "reaffirm; staying silent (fresh deployment, not a broken one)")
            print(json.dumps({"wakeAgent": False}))
            return 0
        print("# no upstream delta, but no brief yet today -> STEADY-STATE daily brief (daily floor)")

    steady = not deltas
    out_path = f"briefings/messaging-brief-{today}"
    print("=== messaging-synthesis wake-gate ===")
    print(f"  product: {product}  |  {len(deltas)} delta(s) since the last brief "
          f"({'none — first brief' if prior is None else Path(prior[0]).name})"
          + ("  [STEADY-STATE: no material change since the last brief — reaffirm current "
             "messaging from the anchor + prior brief; do NOT invent news]" if steady else ""))
    print(f"  write via mcp_okengine_write_create_entity to: {out_path}")
    print(f"  frontmatter: type: messaging-brief, title: \"Messaging brief — {today}\", "
          f"published: {today}, updated: {today}")
    print()
    print("  our capability anchors (a claimed wedge MUST be visible on one of these, or drop it):")
    for cp in anchor.get("capability_pages") or []:
        s = page_summary(cp)
        print(f"  --- [[{cp}]] found={s.get('found')} ---")
        for a in s.get("activity", []):
            print(f"    - {a}")
    print()
    if prior:
        print(f"  prior brief: [[{prior[0].relative_to(WIKI).with_suffix('').as_posix()}]]")
        print(prior[2][:1500])
        print()
    print(f"  {len(deltas)} delta input(s) to synthesize across:")
    for name, p in deltas:
        fm, body = _split(p)
        rel = p.relative_to(WIKI).with_suffix("").as_posix()
        print(f"\n--- DELTA [{name}] [[{rel}]] ---")
        print(body[:1500])
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
