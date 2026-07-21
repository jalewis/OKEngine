#!/usr/bin/env python3
"""Wake-gate for the output-outcome-eval lane (okengine#159 P2). Evaluates delivered OUTPUTS
(briefings) against what actually happened — distinct from okengine.critic (which judges output
QUALITY, not ACCURACY). Surfaces recent briefings + recently-RESOLVED predictions so the agent can
assess which calls held up. Wakes only when there are outputs AND resolved outcomes. No LLM here."""
from __future__ import annotations
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402
from numeric_metrics import compute_output_outcomes  # noqa: E402

MIN_OUTCOMES = int(os.environ.get("OUTCOME_EVAL_MIN", "3"))
RECENT_DAYS = int(os.environ.get("OUTCOME_EVAL_RECENT_DAYS", "30"))
# graded outcomes only (P.GRADED_VALUES) — an ungraded expiry is not a hit/miss.


def main() -> int:
    v = P.vault()
    metrics, state_path, dashboard_path = compute_output_outcomes(v)
    cutoff = P.days_ago_iso(RECENT_DAYS)
    resolved = []
    for p, fm in P.predictions(v):
        if str(fm.get("status", "")).strip().lower() in P.GRADED_VALUES:
            d = P.fm_date(fm, "last_updated", "updated", "resolves_by")
            if d and d >= cutoff:
                resolved.append((p, fm))
    briefs = [p for p in P.iter_pages(v, "briefings")]
    print("=== output-outcome-eval wake-gate ===")
    print(f"  vault: {v}\n  briefings: {len(briefs)}  ·  predictions resolved since {cutoff}: {len(resolved)}")
    print(f"  deterministic metrics: {len(metrics)} rows -> {state_path}")
    print(f"  dashboard: {dashboard_path}")
    for metric in metrics:
        print(f"  {metric['metric_label']}: {metric['value']:.1%} "
              f"(N={metric['n_observations']}, small_n={metric['small_n']})")
    if len(resolved) < MIN_OUTCOMES or not briefs:
        print(f"  → SKIP: need >={MIN_OUTCOMES} recent outcomes AND briefings")
        print(json.dumps({"wakeAgent": False}))
        return 0
    print(f"  batch: {len(resolved)} recent outcome(s)\n")
    print("Interpret the deterministic joins in `dashboards/output-outcome-metrics`, then assess "
          "whether recent briefings' notable calls HELD UP against these resolved outcomes "
          "(accuracy, not prose quality). Write `dashboards/output-outcome-eval` with hits/misses + "
          "patterns; cite the brief + the resolved prediction. Be specific and fair.\n=== recently resolved ===")
    for p, fm in resolved:
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        print(f"  [[{rel}]] subject={fm.get('subject')} status={fm.get('status')}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
