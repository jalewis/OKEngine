#!/usr/bin/env python3
"""Wake-gate + digest for the prediction-regrade cron (okengine#36).

Lists OPEN predictions plus the recent source pages, so the agent can re-grade
open claims against fresh evidence (append to `## Evidence log`, update `evidence:`
+ `confidence:`). Wakes only when there are BOTH open predictions AND recent
sources — otherwise there's nothing new to weigh. Pure script / no LLM.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P   # noqa: E402

MAX_PRED = int(os.environ.get("PREDICTION_REGRADE_MAX_PRED", "12"))
MAX_SRC = int(os.environ.get("PREDICTION_REGRADE_MAX_SRC", "25"))
RECENT_DAYS = int(os.environ.get("PREDICTION_REGRADE_RECENT_DAYS", "3"))


def main() -> int:
    v = P.vault()
    cutoff = P.days_ago_iso(RECENT_DAYS)

    open_preds = [(p, fm) for p, fm in P.predictions(v) if P.is_open(fm)]

    recent_sources = []
    for p in P.iter_pages(v, "sources"):
        fm = P.read_fm(p)
        d = P.fm_date(fm, "published", "last_updated", "updated")
        if d and d >= cutoff:
            recent_sources.append((d, p, str(fm.get("title") or p.stem)))
    recent_sources.sort(key=lambda t: t[0], reverse=True)

    print("=== prediction-regrade wake-gate ===")
    print(f"  vault: {v}")
    print(f"  open predictions: {len(open_preds)}  ·  sources since {cutoff}: {len(recent_sources)}")

    if not open_preds or not recent_sources:
        print("  → SKIP: need both open predictions and recent sources")
        print(json.dumps({"wakeAgent": False}))
        return 0

    preds = open_preds[:MAX_PRED]
    srcs = recent_sources[:MAX_SRC]
    print(f"  batch: {len(preds)} open prediction(s) vs {len(srcs)} recent source(s)\n")
    print("=== open predictions ===")
    print("For each source below that bears on a claim, append a record to the "
          "prediction's `## Evidence log` and update its `evidence:` + `confidence:`. "
          "No-op is correct when no new source bears.\n")
    for i, (p, fm) in enumerate(preds, 1):
        rel = p.relative_to(v).as_posix()
        title = str(fm.get("title") or fm.get("name") or p.stem)
        print(f"## P{i}. {title}  (confidence {fm.get('confidence')})")
        print(f"  page: `{rel}`  ·  subject: {fm.get('subject')}\n")
    print("=== recent sources ===")
    for d, p, title in srcs:
        rel = p.relative_to(v).as_posix()
        print(f"  - {d}  `{rel}`  — {title}")

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
