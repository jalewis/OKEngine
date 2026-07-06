#!/usr/bin/env python3
"""review_queue.py — the human-in-the-loop review queue (okengine#69). A single prioritized view of
what an autonomous, LLM-maintained vault needs a HUMAN to look at, with sign-off that clears items.

Deterministic (no_agent). A page enters the queue for any of:
  - GROUNDING   — its body carries a `## Grounding check` flagging an UNSUPPORTED / not-in-source
                  claim (Tier-2 found a possible falsehood) — highest priority.
  - NEEDS-REVIEW — `needs_review: true` (the universal low-trust flag: lacuna, write-path flags).
  - UNVETTED    — a pack-declared high-stakes type (schema `review_required_types`) with no current
                  sign-off.
A page is VETTED (off the queue) when `reviewed_on` >= its `last_updated` — i.e. a human signed off
AT the current version. Edit the page later and it returns to the queue (re-review). Sign off with
`framework review <pack> --approve <path> --by <name>` (goes through the enforced write path).

Writes wiki/dashboards/review-queue.md. Env: WIKI_PATH (/opt/vault) · REVIEW_SAMPLES (60)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH = WIKI / "dashboards" / "review-queue.md"
SAMPLES = int(os.environ.get("REVIEW_SAMPLES", "60"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)
_UNSUPPORTED = re.compile(r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)",
                          re.S | re.I)
_PRIO = {"GROUNDING": 0, "NEEDS-REVIEW": 1, "UNVETTED": 2}


def _split(p: Path):
    try:
        t = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    m = _FM.match(t)
    if not m:
        return {}, t
    try:
        import yaml
        fm = schema_lib.fast_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _d(v) -> str:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v or ""))
    return m.group(1) if m else ""


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    review_types = set(schema_lib.governing_schema(VAULT).get("review_required_types") or [])
    items = []          # (prio, reason, rel, detail)
    for p in WIKI.rglob("*.md"):
        n = p.name
        if n.startswith(("_", ".")) or n == "INDEX.md" or n.startswith("INDEX-") or ".bak." in n:
            continue
        fm, body = _split(p)
        if not fm:
            continue
        reviewed = _d(fm.get("reviewed_on"))
        updated = _d(fm.get("last_updated") or fm.get("created"))
        vetted = bool(reviewed) and reviewed >= updated      # signed off at the current version
        reason = detail = None
        if _UNSUPPORTED.search(body):
            reason, detail = "GROUNDING", "grounding check flagged an unsupported claim"
        elif fm.get("needs_review") is True:
            reason, detail = "NEEDS-REVIEW", "needs_review flag set"
        elif str(fm.get("type") or "") in review_types and not vetted:
            reason, detail = "UNVETTED", f"{fm.get('type')} not signed off at current version"
        if not reason:
            continue
        if vetted:
            continue   # signed off AT the current version — cleared for any reason; an edit later
            #            advances last_updated past reviewed_on, returning it to the queue.
        rel = p.relative_to(WIKI).as_posix()[:-3]
        items.append((_PRIO[reason], reason, rel, detail + (f" · last vetted {reviewed}" if reviewed else "")))
    items.sort()

    by_reason = {}
    for _, reason, _, _ in items:
        by_reason[reason] = by_reason.get(reason, 0) + 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = "  ·  ".join(f"{r}: **{c}**" for r, c in sorted(by_reason.items())) or "**empty — all clear**"
    L = ["---", "type: dashboard", 'title: "Review queue"', f"updated: {now}", "---", "",
         f"# Review queue — {now}", "", f"**{len(items)} item(s) awaiting a human** · {head}", "",
         "_Sign off with `framework review <pack> --approve <path> --by <name>` (enforced write "
         "path); editing a page after sign-off returns it for re-review._", ""]
    if items:
        L += ["| Priority | Page | Why |", "|---|---|---|"]
        # [[wikilinks]], not file-relative md links: the reader/cockpit linkifiers
        # turn wikilinks into SPA-internal navigation; a raw (path.md) href walks the
        # browser off the app (review feedback: "do any of these links work?"). Every
        # other dashboard already emits wikilinks — this was the odd one out.
        L += [f"| {reason} | [[{rel}]] | {detail} |" for _, reason, rel, detail in items[:SAMPLES]]
        if len(items) > SAMPLES:
            L.append(f"\n_…and {len(items) - SAMPLES} more._")
    L.append("")
    DASH.parent.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L), encoding="utf-8")
    print(f"review-queue: {len(items)} awaiting review ({head.replace('**', '')}) -> "
          "wiki/dashboards/review-queue.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
