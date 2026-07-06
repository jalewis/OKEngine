#!/usr/bin/env python3
"""Wake-gate for the prediction-structural-backfill lane (okengine#159 follow-up; ported and
adapted from the origin system's prediction-structural-backfill).

Drains the backlog `prediction_schema_audit.py` measures: still-gradable predictions that LACK a
`## What would refute this` section. Every prediction is supposed to carry falsification criteria
(pack CLAUDE.md refuses to *file* one without them), but predictions bulk-imported or written
before that rule landed never got the section. Without it a prediction cannot be honestly graded —
so it silently corrupts the calibration / Brier numbers the whole predictions discipline rests on.
This lane hands the agent a small batch per run to author real, per-prediction refutation criteria
via the enforced write path (`append_to_section`, which creates the section without touching
frontmatter or confidence).

Scope is deliberate. RESOLVED predictions (confirmed / refuted / partial / expired-ungraded /
tombstoned) and anything under `predictions/_archive/` are EXCLUDED — retroactively bolting "what
would refute this" onto an already-decided prediction is revisionist and does nothing for future
grading. The target is the load-bearing set: predictions still open enough to be graded later.
Priority within that set is by soonest `resolves_by` (the ones about to be graded need criteria
most urgently), then oldest `made_on`.

Detection MATCHES `prediction_schema_audit._has_refutation_section` exactly, so the audit's flagged
count and this drain are the same backlog — run the audit after a batch and the count drops.

Env: WIKI_PATH (default /opt/vault) · PSB_BATCH_SIZE (5) · PSB_MIN_HITS (1)
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

BATCH_SIZE = int(os.environ.get("PSB_BATCH_SIZE", "5"))
MIN_HITS = int(os.environ.get("PSB_MIN_HITS", "1"))

# resolved / retired statuses whose predictions are NOT in scope (see module docstring).
RESOLVED = {"confirmed", "refuted", "partial", "expired-ungraded", "tombstoned"}
# Identical to prediction_schema_audit._has_refutation_section — keep the two in lockstep.
_REFUTE_RE = re.compile(r"^#{1,3}\s*what would refute this", re.IGNORECASE | re.MULTILINE)


def _has_refutation(path) -> bool:
    try:
        return bool(_REFUTE_RE.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def main() -> int:
    v = P.vault()
    needs: list[tuple] = []            # (path, fm)
    skipped = {"resolved": 0, "archived": 0, "has-section": 0}

    for p, fm in P.predictions(v):
        if "/_archive/" in p.as_posix():
            skipped["archived"] += 1
            continue
        if str(fm.get("status", "")).strip().lower() in RESOLVED:
            skipped["resolved"] += 1
            continue
        if _has_refutation(p):
            skipped["has-section"] += 1
            continue
        needs.append((p, fm))

    # soonest-resolving first (most urgent to make gradable), then oldest-made.
    needs.sort(key=lambda pf: (P.fm_date(pf[1], "resolves_by") or "9999-99-99",
                               P.fm_date(pf[1], "made_on", "created") or "9999-99-99"))
    batch = needs[:BATCH_SIZE]

    print("=== prediction-structural-backfill wake-gate ===")
    print(f"  vault: {v}")
    print(f"  gradable predictions missing '## What would refute this': {len(needs)}")
    print(f"  skipped — resolved/retired: {skipped['resolved']}  ·  archived: {skipped['archived']}"
          f"  ·  already has section: {skipped['has-section']}")
    print()
    print(f"=== batch ({len(batch)} of {len(needs)}, max {BATCH_SIZE} per run) — process IN ORDER ===")
    for i, (p, fm) in enumerate(batch, 1):
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        print(f"{i}. [[{rel}]]")
        print(f"   status={fm.get('status')}  confidence={fm.get('confidence')}  "
              f"resolves_by={fm.get('resolves_by')}  subject={fm.get('subject')}")
    print()
    print(json.dumps({"wakeAgent": len(needs) >= MIN_HITS}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
