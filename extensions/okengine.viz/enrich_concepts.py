#!/usr/bin/env python3
"""enrich_concepts.py — okengine.viz concept-enrich lane (okengine#172). no_agent.

Fills the two Wardley axis fields on `concept` pages so the strategic map gets TRUE
positions instead of graph heuristics:

  - `evolution`:   genesis | custom | product | commodity   (x — the Wardley scale)
  - `value_chain`: 0.15 | 0.5 | 0.85                        (y — higher = foundational,
                    consistent with the entity-coupling heuristic it replaces)
  - `viz_enriched`: ISO date — provenance marker. Fields present WITHOUT the marker are
                    human/pack-set: NEVER touched. Fields present WITH it: already done
                    (no re-enrichment by default).

Selection is recomputed each run (frontmatter-scanning a few thousand concepts is <1s —
no queue file): concepts missing either field, prioritized (1) the VIZ_ANCHOR
neighborhood (the nodes actually on the map), (2) in-degree, (3) the tail. Two
llm_lib.classify() calls per concept; `uncertain` on either -> the page is skipped
untouched (the heuristic position stays — fail-open). Writes add ONLY the three fields
to the frontmatter; the body is preserved byte-for-byte.

Batching contract (a host timeout is a KILL, not a budget): ENRICH_BATCH per run under an
own-clock ENRICH_TIME_BUDGET; every page is written (checkpointed) as it completes; an
endpoint failure stops the run rather than burning the batch.

Env: WIKI_PATH (/opt/vault) · VIZ_ANCHOR · VIZ_EVOLUTION_FIELD (evolution) ·
     VIZ_VALUE_FIELD (value_chain) · ENRICH_BATCH (25) · ENRICH_TIME_BUDGET (300s) ·
     OKENGINE_LLM_BASE_URL / OKENGINE_LLM_MODEL (llm_lib)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm_lib    # noqa: E402  (vendored — reasoning-off default, truncation raises)

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
EVO_FIELD = os.environ.get("VIZ_EVOLUTION_FIELD", "evolution")
VAL_FIELD = os.environ.get("VIZ_VALUE_FIELD", "value_chain")
BATCH = int(os.environ.get("ENRICH_BATCH", "25"))
BUDGET = int(os.environ.get("ENRICH_TIME_BUDGET", "300"))
# per-call ceiling: a contended local host can take >60s for even a tiny classify
CALL_TIMEOUT = int(os.environ.get("ENRICH_CALL_TIMEOUT", "90"))

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_CLINK = re.compile(r"\[\[\s*concepts/(?:[a-z0-9._-]+/)*([a-z0-9][a-z0-9-]*)\s*(?:[|#\]])")
_ANYLINK = re.compile(r"\[\[\s*(?:[A-Za-z0-9._-]+/)*([a-z0-9][a-z0-9-]*)\s*(?:[|#\]])")

EVO_LABELS = ["genesis", "custom", "product", "commodity"]
VALUE_MAP = {"foundational": 0.85, "supporting": 0.5, "surface": 0.15}

_EVO_PROMPT = """Concept: {title}

{body}

On the Wardley evolution scale, where does this concept sit TODAY?
- genesis: novel, experimental, poorly understood, no established practice
- custom: emerging practice, bespoke implementations, early adopters
- product: established products/methodologies exist, competitive market
- commodity: standardized, ubiquitous, utility-like, taken for granted"""

_VAL_PROMPT = """Concept: {title}

{body}

Within its field's value chain, is this concept:
- foundational: load-bearing infrastructure or theory that other capabilities build on
- supporting: an enabling capability in the middle of the chain
- surface: a user-visible, application-level capability at the top of the chain"""


def _fm_text(text: str):
    m = _FM.match(text)
    return m


def _parse_fm(fm_text: str) -> dict:
    try:
        import yaml
        return yaml.safe_load(fm_text) or {}
    except Exception:
        return {}


def main() -> int:
    cdir = WIKI / "concepts"
    if not cdir.is_dir():
        print("enrich-concepts: no concepts/ — nothing to do")
        print(json.dumps({"wakeAgent": False}))
        return 0

    # anchor scope: stems the anchor pages link (same neighborhood rule as the map builder)
    anchors = [a.strip() for a in os.environ.get("VIZ_ANCHOR", "").split(",") if a.strip()]
    anchor_stems: set = set()
    for a in anchors:
        ap = WIKI / a
        if ap.is_file():
            anchor_stems |= {m.lower() for m in _ANYLINK.findall(ap.read_text(encoding="utf-8", errors="replace"))}
    anchor_self = {Path(a).stem.lower() for a in anchors}

    # concepts + their current fields; body head kept for the prompts
    pages: dict[str, dict] = {}
    for p in cdir.rglob("*.md"):
        if p.name.startswith(("_", "INDEX")):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m = _fm_text(text)
        if not m:
            continue                      # no frontmatter — not a governed concept page, skip
        fm = _parse_fm(m.group(1))
        pages[p.stem.lower()] = {"path": p, "fm": fm, "title": str(fm.get("title") or p.stem),
                                 "body": text[m.end():m.end() + 2000]}

    # in-degree + anchor 1-hop (concepts linked from entities the anchors link)
    indeg = {s: 0 for s in pages}
    scope = {s for s in anchor_stems if s in pages} - anchor_self
    for p in WIKI.rglob("*.md"):
        if p.name.startswith(("_", "INDEX")):
            continue
        rel = p.relative_to(WIKI).as_posix()
        anchor_hop = rel.startswith("entities/") and p.stem.lower() in anchor_stems
        for slug in _CLINK.findall(p.read_text(encoding="utf-8", errors="replace")):
            s = slug.lower()
            if s in indeg:
                indeg[s] += 1
                if anchor_hop:
                    scope.add(s)
    scope -= anchor_self

    # candidates: missing either field. Fields present (marker or not) are never touched.
    cands = [s for s, c in pages.items()
             if c["fm"].get(EVO_FIELD) is None or c["fm"].get(VAL_FIELD) is None]
    cands.sort(key=lambda s: (s not in scope, -indeg[s], s))
    batch = cands[:BATCH]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    deadline = time.monotonic() + BUDGET
    enriched = in_scope_done = uncertain = 0
    for s in batch:
        if time.monotonic() > deadline:
            print(f"enrich-concepts: time budget ({BUDGET}s) reached — stopping (work is checkpointed)")
            break
        c = pages[s]
        try:
            evo = llm_lib.classify(_EVO_PROMPT.format(title=c["title"], body=c["body"]),
                                   EVO_LABELS, timeout=CALL_TIMEOUT, retries=0)
            if evo == "uncertain":
                uncertain += 1
                continue
            val = llm_lib.classify(_VAL_PROMPT.format(title=c["title"], body=c["body"]),
                                   list(VALUE_MAP), timeout=CALL_TIMEOUT, retries=0)
            if val == "uncertain":
                uncertain += 1
                continue
        except llm_lib.LLMError as e:
            print(f"enrich-concepts: model endpoint failed on '{s}' — stopping the run: {e}")
            break
        # checkpoint: insert the three fields before the closing ---, body byte-identical
        text = c["path"].read_text(encoding="utf-8", errors="replace")
        m = _fm_text(text)
        if not m:
            continue
        add = f"{EVO_FIELD}: {evo}\n{VAL_FIELD}: {VALUE_MAP[val]}\nviz_enriched: {today}\n"
        c["path"].write_text(f"---\n{m.group(1)}{add}---{text[m.end():]}", encoding="utf-8")
        enriched += 1
        if s in scope:
            in_scope_done += 1

    remaining = len(cands) - enriched
    print(f"enrich-concepts: {enriched} enriched ({in_scope_done} in the map's anchor scope), "
          f"{uncertain} uncertain-skipped, {remaining} still missing fields")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
