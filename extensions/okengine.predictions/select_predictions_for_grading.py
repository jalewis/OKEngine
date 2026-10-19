#!/usr/bin/env python3
"""Wake-gate + digest for the prediction-grade cron (okengine#36).

Lists OPEN predictions whose `resolves_by` date has passed, so the agent can
resolve each (add a `## Postmortem` and flip `status:`). Wakes only when at least
one prediction is overdue. Pure script / no LLM.
"""
from __future__ import annotations

import json
import os
import sys

from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# selection_manifest lives one level UP in the staged layout (/opt/data/scripts/<ext-id>/ ->
# /opt/data/scripts/) and in scripts/cron in the repo layout. Appended, so no shared lib can
# shadow a sibling of this extension.
sys.path.append(str(_HERE.parent))
sys.path.append(str(_HERE.parents[1] / "scripts" / "cron"))
import pred_lib as P   # noqa: E402
from selection_manifest import (  # noqa: E402
    page_selection_key, print_receipt_template, write_selection_manifest)

SELECTION_MANIFEST = Path(os.environ.get(
    "OKENGINE_SELECTION_MANIFEST", str(P.selection_manifest_path("grade"))))

N = int(os.environ.get("PREDICTION_GRADE_BATCH_SIZE", "15"))


def main() -> int:
    v = P.vault()
    today = P.today_iso()
    overdue = []
    for p, fm in P.predictions(v):
        if not P.is_open(fm):
            continue
        rb = P.fm_date(fm, "resolves_by")
        if rb and rb < today:
            overdue.append((rb, p, fm))
    overdue.sort(key=lambda t: t[0])   # most overdue first

    print("=== prediction-grade wake-gate ===")
    print(f"  vault: {v}  ·  today: {today}")
    print(f"  open predictions past resolves_by: {len(overdue)}")

    if not overdue:
        write_selection_manifest([], SELECTION_MANIFEST)   # a no-work run refreshes it too
        print("  → SKIP: no overdue predictions")
        print(json.dumps({"wakeAgent": False}))
        return 0

    chosen = overdue[:N]
    selected = [page_selection_key(p, v) for _, p, _ in chosen]
    manifest = write_selection_manifest(selected, SELECTION_MANIFEST)
    print(f"  batch: {len(chosen)} of {len(overdue)}\n")
    print("=== batch ===")
    print("For each overdue prediction below: append a `## Postmortem` "
          "(append_to_section), then flip `status:` to "
          "confirmed/refuted/partial/expired-ungraded (update_entity). Never edit "
          "made_on/horizon/basis or rewrite earlier sections.\n")
    for i, (rb, p, fm) in enumerate(chosen, 1):
        rel = p.relative_to(v).as_posix()
        title = str(fm.get("title") or fm.get("name") or p.stem)
        conf = fm.get("confidence")
        print(f"## {i}. {title}")
        print(f"  page: `{rel}`  ·  resolves_by: {rb}  ·  confidence: {conf}")
        subj = fm.get("subject")
        if subj:
            print(f"  subject: {subj}")
        print()

    print_receipt_template(manifest)
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
