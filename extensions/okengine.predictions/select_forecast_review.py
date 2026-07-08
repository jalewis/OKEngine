#!/usr/bin/env python3
"""Wake-gate for the forecast-review lane (okengine#159 follow-up, ported from the origin system's
weekly-forecast-review). The META-LAYER over the other predictions ops: synthesizes this week's
resolutions + confidence shifts against the calibration/date-audit/schema-audit dashboards
(calibration_refresh.py, prediction_date_audit.py, prediction_schema_audit.py) into one weekly
discipline review — "are we forecasting well, and what should change."

Wakes only if something actually moved this week (a resolution, or an open prediction re-
evaluated by regrade) — otherwise there is nothing new for a WEEKLY layer to say beyond what
the daily dashboards already show. Pure script / no LLM.

No `prior_confidence` field exists anywhere in this vault's predictions (regrade updates
`confidence:` in place and appends to `## Evidence log`, keeping no delta), so "re-evaluated
this week" — not "confidence shifted by X" — is the honest signal available; the agent reads
each flagged page's Evidence log for what actually changed.

Env: WIKI_PATH (default /opt/vault) · FORECAST_REVIEW_DAYS (window, default 7)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

WINDOW_DAYS = int(os.environ.get("FORECAST_REVIEW_DAYS", "7"))


def _read_dashboard(v, name: str) -> str:
    p = v / "wiki" / "dashboards" / name
    if not p.is_file():
        return f"(no {name} yet)"
    return p.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    v = P.vault()
    since = P.days_ago_iso(WINDOW_DAYS)
    week_ending = P.today_iso()

    resolved_this_week = []
    reevaluated_this_week = []   # open predictions touched (regrade appends to Evidence log +
                                  # bumps confidence/updated) since — no prior_confidence field
                                  # exists anywhere in this vault, so a delta can't be computed;
                                  # "touched this week" is the honest signal available.
    for p, fm in P.predictions(v):
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        status = str(fm.get("status", "")).strip().lower()
        updated = P.fm_date(fm, "updated", "last_updated")
        if not updated or updated < since:
            continue
        if status in P.RESOLVED_VALUES:
            resolved_this_week.append((rel, fm))
        elif status in P.OPEN_VALUES:
            reevaluated_this_week.append((rel, fm))

    if not resolved_this_week and not reevaluated_this_week:
        print(f"# nothing resolved or re-evaluated since {since} — a weekly review has nothing "
              "new to add over the daily dashboards")
        print(json.dumps({"wakeAgent": False}))
        return 0

    # briefings/, not dashboards/ — matches the sibling weekly-synthesis-style lanes (weekly-pdb-
    # review, messaging-synthesis): dashboards/ is for the no_agent mechanical views this lane
    # reads (calibration.md, prediction-date-audit.md, prediction-schema-audit.md); briefings/ is
    # for agent-authored narrative synthesis. A live run before this fix asked for dashboards/ and
    # the agent correctly self-corrected to briefings/ instead — this makes that the documented
    # contract instead of an unexplained deviation.
    out_path = f"briefings/forecast-review-{week_ending}"
    print("=== forecast-review wake-gate ===")
    print(f"  window: {since} .. {week_ending}  |  {len(resolved_this_week)} resolved, "
          f"{len(reevaluated_this_week)} re-evaluated")
    print(f"  write via mcp_okengine_write_create_entity to: {out_path}")
    print(f"  frontmatter: type: dashboard, title: \"Forecast review — {week_ending}\", "
          f"updated: {week_ending}")
    print()
    print("=== calibration.md (Brier + calibration bands) ===")
    print(_read_dashboard(v, "calibration.md")[:2000])
    print()
    print("=== prediction-date-audit.md (dating hygiene) ===")
    print(_read_dashboard(v, "prediction-date-audit.md")[:1000])
    print()
    print("=== prediction-schema-audit.md (field hygiene) ===")
    print(_read_dashboard(v, "prediction-schema-audit.md")[:1000])
    print()
    if resolved_this_week:
        print(f"=== resolved this week ({len(resolved_this_week)}) ===")
        for rel, fm in resolved_this_week[:30]:
            print(f"  [[{rel}]] status={fm.get('status')} confidence={fm.get('confidence')} "
                  f"subject={fm.get('subject')}")
    if reevaluated_this_week:
        print(f"\n=== re-evaluated this week ({len(reevaluated_this_week)}) — read each page's "
              "'## Evidence log' for what changed and why ===")
        for rel, fm in reevaluated_this_week[:30]:
            print(f"  [[{rel}]] confidence={fm.get('confidence')} subject={fm.get('subject')}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
