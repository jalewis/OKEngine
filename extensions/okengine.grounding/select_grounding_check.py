#!/usr/bin/env python3
"""Wake-gate for okengine.grounding's semantic-grounding lane (Tier-2 of the grounding trust system).

Tier-1 (grounding_audit) checks a claim cites a source that EXISTS. This lane checks whether the
source actually SUPPORTS the claim. It surfaces GROUNDED entities (>=1 resolving source page-ref)
that were recently written and not yet grounding-checked, so the agent can read each entity + its
cited sources and flag unsupported assertions. Pure script / no LLM here.

Self-contained (stdlib + yaml). Wakes only when there are >= GROUNDING_MIN fresh candidates.

Env: WIKI_PATH (/opt/vault) · GROUNDING_NAMESPACES (entities,concepts) · GROUNDING_RECENT_DAYS (14)
     GROUNDING_RECHECK_DAYS (90) · GROUNDING_BATCH (8) · GROUNDING_MIN (3)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
NS = [s.strip() for s in os.environ.get("GROUNDING_NAMESPACES", "entities,concepts").split(",") if s.strip()]
RECENT_DAYS = int(os.environ.get("GROUNDING_RECENT_DAYS", "14"))
RECHECK_DAYS = int(os.environ.get("GROUNDING_RECHECK_DAYS", "90"))
BATCH = int(os.environ.get("GROUNDING_BATCH", "8"))
MIN = int(os.environ.get("GROUNDING_MIN", "3"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def _fm(p: Path) -> dict:
    try:
        import yaml
        m = _FM.match(p.read_text(encoding="utf-8", errors="replace")[:8000])
        return (yaml.safe_load(m.group(1)) or {}) if m else {}
    except Exception:
        return {}


def _d(v):
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v or ""))
    return m.group(1) if m else ""


def _stem(ref) -> str:
    return str(ref).strip().strip("[]").strip("/").split("/")[-1].lower().removesuffix(".md")


def _is_pageref(e) -> bool:
    s = str(e).strip().strip("[]")
    return "/" in s or s.lower().endswith(".md")


def main() -> int:
    sdir = WIKI / "sources"
    src_stems = {p.stem.lower() for p in sdir.rglob("*.md")} if sdir.is_dir() else set()
    recent_cut = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
    recheck_cut = (date.today() - timedelta(days=RECHECK_DAYS)).isoformat()
    cands = []
    for ns in NS:
        base = WIKI / ns
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            if p.name.startswith(("_", ".")) or p.name.startswith("INDEX"):
                continue
            fm = _fm(p)
            if not fm:
                continue
            srcs = fm.get("sources")
            entries = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
            resolving = [e for e in entries if _is_pageref(e) and _stem(e) in src_stems]
            if not resolving:
                continue                                  # not grounded -> Tier-1's problem, skip
            if _d(fm.get("last_updated") or fm.get("created")) < recent_cut:
                continue                                  # not recently written
            gc = _d(fm.get("grounding_checked"))
            if gc and gc >= recheck_cut:
                continue                                  # checked recently
            cands.append((p.relative_to(WIKI).as_posix()[:-3],
                          str(fm.get("name") or fm.get("title") or p.stem),
                          [str(e) for e in resolving]))
    cands.sort()

    print("=== grounding-check wake-gate ===")
    print(f"  grounded, recent, unchecked candidates: {len(cands)}")
    if len(cands) < MIN:
        print(f"  → SKIP: {len(cands)} (threshold {MIN})")
        print(json.dumps({"wakeAgent": False}))
        return 0
    batch = cands[:BATCH]
    print(f"  batch: {len(batch)} of {len(cands)}\n=== entities to verify ===")
    print("For EACH entity: read it AND its cited source pages (mcp_okengine_get_page), then check "
          "whether each substantive claim is SUPPORTED by those sources. Append a `## Grounding "
          "check` note (supported / unsupported / not-found-in-source) and set "
          "`grounding_checked: <today>`. Flag a MATERIAL unsupported claim for review. Be "
          "conservative — only flag clear gaps, not phrasing.\n")
    for rel, name, refs in batch:
        print(f"## {name}\n  page: `{rel}`  ·  sources: {', '.join(refs[:6])}\n")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
