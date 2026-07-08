#!/usr/bin/env python3
"""Wake-gate for the prediction-falsification-search lane (okengine#159 P2). Unlike `regrade` (which
neutrally folds new evidence), this RED-TEAMS the open book: it surfaces HIGH-CONFIDENCE open
predictions + recent sources so the agent actively seeks DISCONFIRMING evidence. Wakes only when
there are confident open claims AND fresh sources to test them against. Pure script / no LLM."""
from __future__ import annotations
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

MAX_PRED = int(os.environ.get("FALSIFY_MAX_PRED", "10"))
MAX_SRC = int(os.environ.get("FALSIFY_MAX_SRC", "25"))
RECENT_DAYS = int(os.environ.get("FALSIFY_RECENT_DAYS", "7"))
# Confidence labels/numbers worth red-teaming (the costly-if-wrong calls).
_HIGH = {"high", "very-high", "medium-high"}


def _is_high(fm) -> bool:
    c = str(fm.get("confidence", "")).strip().lower()
    if c in _HIGH:
        return True
    try:
        f = float(c.rstrip("%"))
        return (f / 100.0 if f > 1.0 else f) >= 0.6
    except ValueError:
        return False


def main() -> int:
    v = P.vault()
    cutoff = P.days_ago_iso(RECENT_DAYS)
    high_open = [(p, fm) for p, fm in P.predictions(v) if P.is_open(fm) and _is_high(fm)]
    recent = []
    for p in P.iter_pages(v, "sources"):
        fm = P.read_fm(p)
        # genuine publication recency only — `last_updated`/`updated` are bumped by the token-free
        # importers on every ingest, so they'd flag long-old sources as "recent". Matches the
        # published/created/date signal pred_lib.recent_source_slugs uses (keep the two consistent).
        d = P.fm_date(fm, "published", "created", "date")
        if d and d >= cutoff:
            recent.append((d, p, str(fm.get("title") or p.stem)))
    recent.sort(key=lambda t: t[0], reverse=True)
    print("=== falsification-search wake-gate ===")
    print(f"  vault: {v}\n  high-confidence open: {len(high_open)}  ·  sources since {cutoff}: {len(recent)}")
    if not high_open or not recent:
        print("  → SKIP: need confident open predictions AND recent sources")
        print(json.dumps({"wakeAgent": False}))
        return 0
    print(f"  batch: {min(len(high_open),MAX_PRED)} prediction(s) vs {min(len(recent),MAX_SRC)} source(s)\n")
    print("For each confident open prediction below, ACTIVELY SEEK DISCONFIRMING evidence in the "
          "recent sources (steelman the opposite). Where evidence weakens it, append to its "
          "`## Evidence log` and LOWER `confidence:` (or flag for grading). Finding nothing is a "
          "valid, recordable result.\n=== confident open predictions ===")
    for p, fm in high_open[:MAX_PRED]:
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        print(f"  [[{rel}]] subject={fm.get('subject')} conf={fm.get('confidence')} resolves_by={fm.get('resolves_by')}")
    print("=== recent sources ===")
    for d, p, t in recent[:MAX_SRC]:
        print(f"  {d} [[{p.relative_to(v / 'wiki').as_posix()[:-3]}]] {t[:80]}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
