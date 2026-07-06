#!/usr/bin/env python3
"""prediction_schema_audit.py — field-hygiene audit for okengine.predictions (#159 follow-up).

Deterministic (no_agent): complements prediction_date_audit.py (which only checks `resolves_by`
sanity) by checking the REST of a prediction's structural fields — the class of defect a
`resolves_by`-only audit cannot see. Flags:
  - missing `made_on` / `confidence` / `subject`
  - `confidence` that doesn't parse as a number (0-1 or 0-100) or a recognized qualitative label
  - `horizon` that doesn't match the day-count computed from `made_on`/`resolves_by`
    (short <=90d, medium <=365d, long <=1825d, strategic >1825d — the rubric daily-pdb/
    weekly-synthesis/lacuna's prompts already carry; this makes it a checked INVARIANT instead
    of prompt-compliance-and-hope)
  - missing a `## What would refute this` section (a hard requirement in every prediction-
    filing prompt in the fleet, but never previously machine-checked)
Writes wiki/dashboards/prediction-schema-audit.md. Zero model cost.

Motivating incident: a live audit of 10 lacuna-filed predictions found 6 with a miscalculated
horizon and 4 with invented field names / a missing made_on entirely — caught by hand, once,
after the fact. This dashboard makes that check continuous and generic across every lane that
files predictions, not just lacuna's.

Env: WIKI_PATH (default /opt/vault)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

_QUALITATIVE = {"very-low", "low", "medium-low", "medium", "medium-high", "high", "very-high"}
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _parse_date(v) -> "date | None":
    m = _ISO.match(str(v or "").strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def _horizon_for(days: int) -> str:
    if days <= 90:
        return "short"
    if days <= 365:
        return "medium"
    if days <= 1825:
        return "long"
    return "strategic"


def _confidence_valid(v) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    if s in _QUALITATIVE:
        return True
    try:
        f = float(s.rstrip("%"))
        return 0.0 <= (f / 100.0 if f > 1.0 else f) <= 1.0
    except ValueError:
        return False


def _has_refutation_section(path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r"^#{1,3}\s*what would refute this", text, re.IGNORECASE | re.MULTILINE))


def main() -> int:
    v = P.vault()
    total = 0
    flagged = []   # (issue, stem, rel)
    for p, fm in P.predictions(v):
        total += 1
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        issues = []

        if not fm.get("subject"):
            issues.append("missing subject")
        if not fm.get("confidence"):
            issues.append("missing confidence")
        elif not _confidence_valid(fm.get("confidence")):
            issues.append(f"unparseable confidence={fm.get('confidence')!r}")
        if not fm.get("made_on"):
            issues.append("missing made_on")
        if not _has_refutation_section(p):
            issues.append("missing '## What would refute this'")

        made_on = _parse_date(fm.get("made_on"))
        resolves_by = _parse_date(fm.get("resolves_by"))
        horizon = str(fm.get("horizon") or "").strip().lower()
        if made_on and resolves_by:
            correct = _horizon_for((resolves_by - made_on).days)
            if not horizon:
                issues.append(f"missing horizon (should be {correct!r})")
            elif horizon != correct:
                issues.append(f"horizon={horizon!r} should be {correct!r} "
                               f"({(resolves_by - made_on).days}d)")

        for issue in issues:
            flagged.append((issue, p.stem, rel))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Prediction schema audit"', f"updated: {now}", "---", "",
         f"# Prediction schema audit — {now}", "",
         "_Field-hygiene audit complementing prediction-date-audit (which only checks "
         "`resolves_by` sanity): missing required fields, unparseable confidence, and "
         "`horizon` mismatched against the computed made_on→resolves_by day-count "
         "(okengine#159 follow-up)._", "",
         f"- predictions: **{total}**  ·  issue(s) flagged: **{len(flagged)}**", ""]
    if flagged:
        L += ["| Issue | Prediction | Page |", "|---|---|---|"]
        for issue, stem, rel in sorted(flagged):
            L.append(f"| {issue} | {stem} | [[{rel}]] |")
    else:
        L.append("_No schema issues found._")
    L.append("")
    dash = v / "wiki" / "dashboards" / "prediction-schema-audit.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("\n".join(L), encoding="utf-8")
    print(f"prediction-schema-audit: {len(flagged)} issue(s) across {total} prediction(s) -> "
          "wiki/dashboards/prediction-schema-audit.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
