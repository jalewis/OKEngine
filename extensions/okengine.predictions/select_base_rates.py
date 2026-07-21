#!/usr/bin/env python3
"""Wake-gate for the base-rates lane (okengine#159 P2). Lists RESOLVED predictions so the agent can
maintain a base-rate reference (for recurring prediction classes, the historical resolution rate).
Wakes only with enough resolved history to be meaningful. Pure script / no LLM."""
from __future__ import annotations
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402
from numeric_metrics import compute_base_rates  # noqa: E402

MIN = int(os.environ.get("BASE_RATES_MIN", "8"))
MAX = int(os.environ.get("BASE_RATES_MAX", "60"))
# graded outcomes only — expired-ungraded has no result, so it must not enter a resolution rate
# (P.GRADED_VALUES, not P.RESOLVED_VALUES; see pred_lib).


def main() -> int:
    v = P.vault()
    rows, state_path, dashboard_path = compute_base_rates(v)
    resolved = [(p, fm) for p, fm in P.predictions(v)
                if str(fm.get("status", "")).strip().lower() in P.GRADED_VALUES]
    print("=== base-rates wake-gate ===")
    print(f"  vault: {v}\n  resolved predictions: {len(resolved)}")
    print(f"  deterministic metrics: {len(rows)} rows -> {state_path}")
    print(f"  dashboard: {dashboard_path}")
    coverage = sorted((row for row in rows if row["rate_kind"] == "event-coverage" and
                       row["class_label"] != "(all-event-types)"), key=lambda row: row["value"])
    if coverage:
        print("  lowest event coverage: " + ", ".join(
            f"{row['class_label']}={row['value']:.1%} (N={row['n_observations']})"
            for row in coverage[:5]))
    overall = next((row for row in rows if row["rate_kind"] == "outcome-rate" and
                    row["class_label"] == "(all-resolved)"), None)
    if overall:
        print(f"  overall resolved hit rate: {overall['value']:.1%} "
              f"(N={overall['n_observations']}, small_n={overall['small_n']})")
    if len(resolved) < MIN:
        print(f"  → SKIP: {len(resolved)} resolved (need {MIN} for meaningful base rates)")
        print(json.dumps({"wakeAgent": False}))
        return 0
    print(f"  batch: {min(len(resolved), MAX)} resolved\n=== resolved predictions ===")
    print("Interpret the deterministic metrics in `dashboards/base-rate-metrics`; do not estimate "
          "rates the substrate already computes. Add only judgmental recurring claim-shape classes "
          "to `dashboards/base-rates`, with citations and explicit N.\n")
    for p, fm in resolved[:MAX]:
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        print(f"  [[{rel}]] subject={fm.get('subject')} status={fm.get('status')} conf={fm.get('confidence')}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
