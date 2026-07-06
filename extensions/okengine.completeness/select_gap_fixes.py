#!/usr/bin/env python3
"""select_gap_fixes.py — wake-gate for okengine.completeness's gap-drain op.

The audit lane SURFACES gaps; this closes the loop: gaps whose rules the pack marked
agent-fixable drain on a schedule instead of accumulating as operator homework. The pack
declares fixability PER RULE (default: human — queue-only):

    - id: prediction-needs-basis
      ...
      fix: agent-draft     # agent | agent-draft | human (default)

  agent        the agent fixes the subject page via the MCP write path; the next audit
               auto-resolves the gap. For mechanical, low-judgment expectations.
  agent-draft  same, but the fix is a DRAFT: the agent must set needs_review: true on the
               touched page. For judgment-bearing expectations (a refutation criterion is
               a commitment, not a lookup).
  human        never surfaced here.

Surfaces the N oldest open gaps from fixable rules (GAP_DRAIN_BATCH, default 5) with the
full context the agent needs; none -> silent. Dismissed/resolved gaps never surface.

Env: WIKI_PATH (/opt/vault) · COMPLETENESS_RULES (config/completeness-rules.yaml) ·
     GAP_DRAIN_BATCH (5)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
RULES_FILE = os.environ.get("COMPLETENESS_RULES", "config/completeness-rules.yaml")
BATCH = int(os.environ.get("GAP_DRAIN_BATCH", "5"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def _fm(p: Path) -> dict:
    try:
        m = _FM.match(p.read_text(encoding="utf-8", errors="replace"))
        d = yaml.safe_load(m.group(1)) if m else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def main() -> int:
    f = VAULT / RULES_FILE
    if not f.is_file():
        print(f"# no completeness rules at {RULES_FILE} — nothing to drain")
        print(json.dumps({"wakeAgent": False}))
        return 0
    try:
        rules = {r["id"]: r for r in (yaml.safe_load(f.read_text()) or {}).get("rules", [])
                 if isinstance(r, dict) and r.get("id")}
    except yaml.YAMLError:
        print("# rules file unparseable — refusing to drain")
        print(json.dumps({"wakeAgent": False}))
        return 0
    fixable = {rid: r for rid, r in rules.items() if r.get("fix") in ("agent", "agent-draft")}
    if not fixable:
        print("# no rule is marked fix: agent|agent-draft — the queue is operator-only by "
              "the pack's own declaration; nothing to drain")
        print(json.dumps({"wakeAgent": False}))
        return 0

    gaps = []
    gdir = WIKI / "gaps"
    if gdir.is_dir():
        for p in gdir.rglob("*.md"):
            if p.name.startswith(("_", ".", "INDEX")):
                continue
            fm = _fm(p)
            if str(fm.get("status")) != "open" or str(fm.get("rule")) not in fixable:
                continue
            gaps.append((str(fm.get("first_seen") or "9999"), p, fm))
    if not gaps:
        print("# no open gaps under agent-fixable rules — the drain is dry")
        print(json.dumps({"wakeAgent": False}))
        return 0

    gaps.sort()
    surfaced = gaps[:BATCH]
    print("=== gap-drain wake-gate (okengine.completeness) ===")
    print(f"  fixable rules: {sorted(fixable)} · open fixable gaps: {len(gaps)} · "
          f"surfaced: {len(surfaced)} (GAP_DRAIN_BATCH={BATCH}; the rest drain next run)")
    print()
    for _, p, fm in surfaced:
        rule = fixable[str(fm.get("rule"))]
        mode = rule.get("fix")
        print(f"## gap [[gaps/{p.stem}]] — rule `{fm.get('rule')}` (fix: {mode})")
        print(f"   subject: [[{fm.get('subject')}]]")
        print(f"   unmet expectation: {fm.get('expectation')}")
        if rule.get("resolution_hint"):
            print(f"   hint: {rule['resolution_hint']}")
        if mode == "agent-draft":
            print("   DRAFT MODE: your fix must set `needs_review: true` on the subject page.")
        print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
