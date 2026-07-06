#!/usr/bin/env python3
"""calibration_refresh.py — forecasting-discipline measurement for okengine.predictions (#159).

Deterministic (no_agent): over RESOLVED predictions, computes the Brier score and a calibration
table (stated confidence vs realized hit-rate), and writes wiki/dashboards/calibration.md. This is
the backbone of forecasting discipline — "are our 70% calls right ~70% of the time?" — with zero
model cost. Empty until predictions resolve; activates as grade/regrade close them out.

Outcome from `status`: confirmed=1.0, refuted=0.0, partial=0.5 (expired-ungraded has no outcome →
skipped). Confidence → probability via a scale (qualitative labels mapped, or a 0-1 / 0-100 number
parsed). Override the scale with PREDICTION_CONFIDENCE_SCALE (JSON {label: prob}).

Env: WIKI_PATH (default /opt/vault) · PREDICTION_CONFIDENCE_SCALE (JSON)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

OUTCOME = {"confirmed": 1.0, "refuted": 0.0, "partial": 0.5}
_DEFAULT_SCALE = {"very-low": 0.1, "low": 0.25, "medium-low": 0.375, "medium": 0.5,
                  "medium-high": 0.625, "high": 0.75, "very-high": 0.9}


def _scale() -> dict:
    override = os.environ.get("PREDICTION_CONFIDENCE_SCALE")
    if override:
        try:
            d = json.loads(override)
            if isinstance(d, dict):
                return {str(k).strip().lower(): float(v) for k, v in d.items()}
        except Exception:
            pass
    return _DEFAULT_SCALE


def confidence_prob(v, scale: dict):
    """Stated confidence -> probability in [0,1]. Number (0-1 or 0-100) parsed; else label mapped."""
    if v is None:
        return None
    s = str(v).strip().lower()
    try:
        f = float(s.rstrip("%"))
        return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
    except ValueError:
        return scale.get(s)


def main() -> int:
    v = P.vault()
    scale = _scale()
    graded = []   # (prob, outcome, label, slug)
    for p, fm in P.predictions(v):
        if P.is_open(fm):
            continue
        out = OUTCOME.get(str(fm.get("status", "")).strip().lower())
        prob = confidence_prob(fm.get("confidence"), scale)
        if out is not None and prob is not None:
            graded.append((prob, out, str(fm.get("confidence")), p.stem))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = ["---", "type: dashboard", 'title: "Forecast calibration"', f"updated: {now}", "---", "",
         f"# Forecast calibration — {now}", "",
         "_Brier score + calibration over RESOLVED predictions (okengine#159). Lower Brier = better; "
         "a well-calibrated band's predicted prob ≈ its realized hit-rate._", ""]
    if not graded:
        L += ["_No resolved+scored predictions yet — fills in as grade/regrade close predictions._", ""]
    else:
        brier = sum((pr - o) ** 2 for pr, o, _, _ in graded) / len(graded)
        base = sum(o for _, o, _, _ in graded) / len(graded)
        L += [f"- resolved & scored: **{len(graded)}**  ·  Brier: **{brier:.3f}**  ·  "
              f"base rate (hit fraction): **{base:.2f}**", "",
              "## Calibration by confidence band", "",
              "| Predicted prob | N | Realized hit-rate | Gap |", "|---|---|---|---|"]
        # bucket into deciles by predicted prob
        buckets: dict[int, list] = {}
        for pr, o, _, _ in graded:
            buckets.setdefault(min(9, int(pr * 10)), []).append((pr, o))
        for b in sorted(buckets):
            rows = buckets[b]
            pred = sum(pr for pr, _ in rows) / len(rows)
            realized = sum(o for _, o in rows) / len(rows)
            L.append(f"| {pred:.2f} | {len(rows)} | {realized:.2f} | {realized - pred:+.2f} |")
    L.append("")
    dash = v / "wiki" / "dashboards" / "calibration.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text("\n".join(L), encoding="utf-8")
    print(f"calibration-refresh: {len(graded)} resolved+scored prediction(s) -> "
          "wiki/dashboards/calibration.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
