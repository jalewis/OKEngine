#!/usr/bin/env python3
"""Wake-gate for the prediction-schema-drain lane (ported and adapted from the origin system's
prediction-schema-drain).

The REMEDIATION counterpart to `prediction_schema_audit.py` (which only MEASURES). It surfaces
prediction pages with fixable VALUE drift and hands the agent a small batch to normalize via the
write path — the gap between the measure-only audit and the refute-only structural-backfill:

  - missing required fields (made_on / horizon / resolves_by / confidence / subject; `status`
    defaults are handled too) — derivable ones the agent fills from the claim body / `created:`;
  - horizon drift — a value outside {short, medium, long, strategic} (e.g. `medium-term`);
  - status drift — a status outside the canonical set. NOTE: `active` is NOT drift here — it is a
    canonical open synonym per `config/base-schema.yaml` (`open_values: [open, active]`);
  - unparseable confidence — neither a number (0-1 / 0-100) nor a recognized qualitative label.
    Qualitative `low`/`medium`/`high` IS valid in this pack's flag-not-gate model, so only genuine
    garbage counts (detection reuses the same rule as the audit);
  - batch-container files (>=2 `## Prediction N` sections) — FLAGGED for human review, never
    auto-split (splitting/re-typing is a human decision), so they do NOT drive the wake.

Deliberately does NOT touch the structural-frontmatter half (no / broken / malformed frontmatter):
the engine's generic repair lanes (repair-broken-frontmatter, repair-yaml-*, schema-type-drain)
already own that across every type. Pages whose frontmatter doesn't parse never reach this lane —
`pred_lib.predictions` filters them out — so there is no overlap.

Env: WIKI_PATH (default /opt/vault) · PSD_BATCH_SIZE (5) · PSD_MIN_HITS (1)
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

BATCH_SIZE = int(os.environ.get("PSD_BATCH_SIZE", "5"))
MIN_HITS = int(os.environ.get("PSD_MIN_HITS", "1"))

# `active` is canonical (open synonym, base-schema open_values). Only genuinely non-canonical
# statuses are drift.
CANONICAL_STATUS = {"open", "active", "confirmed", "refuted", "partial", "expired-ungraded"}
CANONICAL_HORIZON = {"short", "medium", "long", "strategic"}
REQUIRED_FIELDS = ("made_on", "horizon", "resolves_by", "confidence", "status", "subject")
_QUALITATIVE = {"very-low", "low", "medium-low", "medium", "medium-high", "high", "very-high"}

# Common drift → canonical suggestions (agent confirms against the body before applying).
STATUS_HINTS = {"resolved-true": "confirmed", "resolved-false": "refuted", "resolved": "confirmed"}
HORIZON_HINTS = {"medium-term": "medium", "mid": "medium", "near": "short", "near-term": "short",
                 "long-term": "long", "far": "strategic"}

_BATCH_RE = re.compile(r"^##\s+Prediction\s+\d+\b", re.MULTILINE | re.IGNORECASE)
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _confidence_valid(v) -> bool:
    """Identical rule to prediction_schema_audit._confidence_valid — keep in lockstep."""
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


def _body(path) -> str:
    try:
        t = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = _FM.match(t)
    return t[m.end():] if m else t


def classify(fm: dict, body: str) -> tuple[list[str], bool]:
    """(fixable-issue strings, is_batch_container) for one prediction page."""
    if len(_BATCH_RE.findall(body)) >= 2:
        return [], True   # batch container — flagged, not agent-fixable; skip value checks

    issues: list[str] = []
    missing = [f for f in REQUIRED_FIELDS if not fm.get(f)]
    if missing:
        issues.append(f"missing required: {', '.join(missing)}")

    s = fm.get("status")
    if s is not None and str(s).strip().lower() not in CANONICAL_STATUS:
        hint = STATUS_HINTS.get(str(s).strip().lower())
        issues.append(f"status drift: {s!r}" + (f" -> '{hint}'" if hint else " (read body)"))

    h = fm.get("horizon")
    if h is not None and str(h).strip().lower() not in CANONICAL_HORIZON:
        hint = HORIZON_HINTS.get(str(h).strip().lower())
        issues.append(f"horizon drift: {h!r}" + (f" -> '{hint}'" if hint else ""))

    c = fm.get("confidence")
    if c is not None and not _confidence_valid(c):
        issues.append(f"unparseable confidence: {c!r}")

    return issues, False


def main() -> int:
    v = P.vault()
    needs: list[tuple] = []       # (rel, issues, fm)
    batch_containers: list[str] = []

    for p, fm in P.predictions(v):
        rel = p.relative_to(v / "wiki").as_posix()[:-3]
        issues, is_batch = classify(fm, _body(p))
        if is_batch:
            batch_containers.append(rel)
        elif issues:
            needs.append((rel, issues, fm))

    batch = needs[:BATCH_SIZE]
    print("=== prediction-schema-drain wake-gate ===")
    print(f"  vault: {v}")
    print(f"  predictions with fixable value drift: {len(needs)}")
    print(f"  batch-container files (human review — NOT agent-fixable): {len(batch_containers)}")
    for rel in batch_containers:
        print(f"    ⚑ [[{rel}]] — split into individual prediction files OR re-type; human decides")
    print()
    print(f"=== batch ({len(batch)} of {len(needs)}, max {BATCH_SIZE} per run) — process IN ORDER ===")
    for i, (rel, issues, fm) in enumerate(batch, 1):
        print(f"{i}. [[{rel}]]")
        for iss in issues:
            print(f"   - {iss}")
    print()
    # Batch-containers do NOT drive the wake — the agent can't fix them, so waking to re-flag
    # every run would spin forever. Only fixable value drift wakes the agent.
    print(json.dumps({"wakeAgent": len(needs) >= MIN_HITS}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
