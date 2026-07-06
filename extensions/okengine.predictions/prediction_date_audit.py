#!/usr/bin/env python3
"""prediction_date_audit.py — dating-hygiene audit for okengine.predictions (#159).

Deterministic (no_agent): a falsifiable forecast MUST carry a sane resolution date. This flags
predictions whose `resolves_by` is missing, unparseable, or implausible:
  - missing / unparseable  → can never be graded (grade-watch keys off resolves_by)
  - far-overdue & still open → should have been graded long ago (grade lane not catching it)
  - absurdly far future     → effectively unfalsifiable within any useful horizon
Writes wiki/dashboards/prediction-date-audit.md. Zero model cost.

Env: WIKI_PATH (default /opt/vault) · PRED_OVERDUE_DAYS (default 30) · PRED_MAX_HORIZON_DAYS (1825)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

OVERDUE = int(os.environ.get("PRED_OVERDUE_DAYS", "30"))
MAX_HORIZON = int(os.environ.get("PRED_MAX_HORIZON_DAYS", "1825"))   # ~5y
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _parse(v):
    m = _ISO.match(str(v or "").strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def main() -> int:
    v = P.vault()
    today = datetime.now(timezone.utc).date()
    flagged = []   # (issue, slug, rel, raw)
    total = 0
    for p, fm in P.predictions(v):
        total += 1
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        raw = fm.get("resolves_by")
        d = _parse(raw)
        if d is None:
            flagged.append(("missing/unparseable resolves_by", p.stem, rel, str(raw)))
            continue
        if P.is_open(fm) and (today - d).days > OVERDUE:
            flagged.append((f"open + overdue {(today - d).days}d", p.stem, rel, str(raw)))
        elif (d - today).days > MAX_HORIZON:
            flagged.append((f"horizon {(d - today).days}d (> {MAX_HORIZON})", p.stem, rel, str(raw)))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Prediction date audit"', f"updated: {now}", "---", "",
         f"# Prediction date audit — {now}", "",
         "_Falsifiable forecasts need a sane resolution date (okengine#159). Flags missing, "
         "overdue-but-open, or unfalsifiably-distant `resolves_by`._", "",
         f"- predictions: **{total}**  ·  flagged: **{len(flagged)}**", ""]
    if flagged:
        L += ["| Issue | Prediction | resolves_by |", "|---|---|---|"]
        for issue, stem, rel, raw in sorted(flagged):
            L.append(f"| {issue} | [{stem}]({rel}.md) | {raw} |")
    L.append("")
    dash = v / "wiki" / "dashboards" / "prediction-date-audit.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("\n".join(L), encoding="utf-8")
    print(f"prediction-date-audit: {len(flagged)}/{total} flagged -> "
          "wiki/dashboards/prediction-date-audit.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
