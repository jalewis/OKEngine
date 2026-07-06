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

MIN = int(os.environ.get("BASE_RATES_MIN", "8"))
MAX = int(os.environ.get("BASE_RATES_MAX", "60"))
_RESOLVED = {"confirmed", "refuted", "partial"}


def main() -> int:
    v = P.vault()
    resolved = [(p, fm) for p, fm in P.predictions(v)
                if str(fm.get("status", "")).strip().lower() in _RESOLVED]
    print("=== base-rates wake-gate ===")
    print(f"  vault: {v}\n  resolved predictions: {len(resolved)}")
    if len(resolved) < MIN:
        print(f"  → SKIP: {len(resolved)} resolved (need {MIN} for meaningful base rates)")
        print(json.dumps({"wakeAgent": False}))
        return 0
    print(f"  batch: {min(len(resolved), MAX)} resolved\n=== resolved predictions ===")
    print("Cluster these into recurring CLASSES (by subject kind / claim shape) and maintain "
          "`dashboards/base-rates` with each class's historical resolution rate + n. Conservative; "
          "a class needs several data points. Cite the predictions.\n")
    for p, fm in resolved[:MAX]:
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        print(f"  [[{rel}]] subject={fm.get('subject')} status={fm.get('status')} conf={fm.get('confidence')}")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
