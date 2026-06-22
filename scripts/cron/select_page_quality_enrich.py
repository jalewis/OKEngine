#!/usr/bin/env python3
"""Wake-gate + digest for the page-quality-enrich cron.

Closes the audit→enrich loop: page_quality_audit.py scores pages and writes a
deficient enrich queue (page-quality-queue.json, ranked by inbound links). This
wake-gate takes the top not-recently-enriched deficient pages and, for each,
gathers the SOURCE pages that already cite it — so the agent can DEEPEN the page
from local evidence (the citing sources contain the content about it) without
web tools. A thin page that 600 sources reference is exactly the page where the
material to write it already exists in the vault.

Skips pages enriched within ENRICH_COOLDOWN_DAYS (state file) so the queue
rotates instead of churning the same page. Local-only — the inbound sources are
the evidence.

Env:
  WIKI_PATH                 vault root (default /opt/vault)
  HERMES_HOME               state dir (default /opt/data)
  PQ_ENRICH_BATCH           pages per run (default 6)
  PQ_ENRICH_CTX             inbound sources surfaced per page (default 6)
  ENRICH_COOLDOWN_DAYS      don't re-enrich within N days (default 21)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
STATE = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "scripts" / "page-quality-enrich-state.json"
QUEUE = WIKI / "operational" / "page-quality-queue.json"
BATCH = int(os.environ.get("PQ_ENRICH_BATCH", "6"))
CTX = int(os.environ.get("PQ_ENRICH_CTX", "6"))
COOLDOWN = int(os.environ.get("ENRICH_COOLDOWN_DAYS", "21"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=0))
    except OSError:
        pass


def _days_since(iso: str, today) -> int:
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
        return (today - d).days
    except (ValueError, TypeError):
        return 10_000


def _excerpt_around(text: str, stem: str) -> str:
    """A sentence-ish window around the first mention of the target stem."""
    body = text
    m = _FM_RE.match(text)
    if m:
        body = text[m.end():]
    needle = stem.replace("-", "[- ]?")
    mm = re.search(needle, body, re.IGNORECASE)
    if not mm:
        return ""
    start = body.rfind(".", 0, mm.start()) + 1
    end = body.find(".", mm.end())
    seg = body[start:(end + 1 if end > 0 else mm.end() + 160)].strip()
    seg = re.sub(r"\s+", " ", seg)
    return seg[:240]


def main() -> int:
    today = datetime.now(timezone.utc).date()
    if not QUEUE.is_file():
        print("# no page-quality-queue.json — run page-quality-audit first")
        print(json.dumps({"wakeAgent": False}))
        return 0
    try:
        queue = json.loads(QUEUE.read_text())
    except (OSError, ValueError):
        print(json.dumps({"wakeAgent": False}))
        return 0

    state = _load_state()
    # candidates: deficient, inbound>=1, not enriched within cooldown; keep queue order (inbound desc)
    cands = [q for q in queue
             if q.get("inbound", 0) >= 1
             and _days_since(state.get(q["page"], "2000-01-01"), today) >= COOLDOWN]
    targets = {q["page"].split("/")[-1]: q for q in cands[:BATCH * 3]}  # over-select; some may have no usable inbound

    # one pass over sources: collect inbound (source path, excerpt) per target stem
    inbound_ctx: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in (WIKI / "sources").rglob("*.md"):
        if p.name.startswith("_") or "_archive" in p.parts:
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        links = {l.strip().split("/")[-1] for l in _WIKILINK_RE.findall(txt)}
        hit = links & targets.keys()
        for stem in hit:
            if len(inbound_ctx[stem]) < CTX:
                ex = _excerpt_around(txt, stem)
                inbound_ctx[stem].append((p.relative_to(WIKI).as_posix(), ex))

    # build batch: queue order, but only pages with usable inbound context
    batch = []
    for q in cands:
        stem = q["page"].split("/")[-1]
        if stem in inbound_ctx and inbound_ctx[stem]:
            batch.append((q, inbound_ctx[stem]))
        if len(batch) >= BATCH:
            break

    print("=== page-quality-enrich wake-gate ===")
    print(f"  queue size: {len(queue)}  eligible (cooldown {COOLDOWN}d): {len(cands)}  this batch: {len(batch)}")
    if not batch:
        print("  → SKIP: nothing to enrich")
        print(json.dumps({"wakeAgent": False}))
        return 0

    # Optimistic cooldown: mark batched pages NOW so the queue rotates instead
    # of re-surfacing the same top-inbound pages every day. The daily audit
    # re-derives the deficient set, so a page that actually got fixed drops off
    # on its own; this only prevents same-day re-hammering of a page we just
    # handed to the agent (even if that agent run partially fails).
    state.update({q["page"]: today.isoformat() for q, _ in batch})
    _save_state(state)

    print()
    print("Deepen each page from its OWN citing sources (local evidence — no web "
          "tools). Add 2-4 `##` sections of analysis, populate `sources:` from the "
          "citing pages, and bump `updated:`. AUGMENT — never drop existing content "
          "(the write-guard enforces this). Modify pages with the file_write/patch "
          "tools ONLY — never shell redirection (echo/cat/heredoc), which can hit "
          "permission errors the native tools don't. Skip a page if the inbound "
          "sources don't actually contain substantive material about it.")
    print()
    print("=== batch ===")
    for q, ctx in batch:
        print(f"- [[{q['page']}]]  tier={q['tier']} words={q.get('words')} "
              f"sections={q.get('sections')} sources={q.get('sources')} inbound={q.get('inbound')}")
        print(f"    citing sources ({len(ctx)}):")
        for src, ex in ctx:
            print(f"      - [[{src[:-3]}]]" + (f" — \"{ex}\"" if ex else ""))
    print()
    print("After enriching, append one `wiki/log.md` line: "
          "`## [YYYY-MM-DD HH:MM UTC] page-quality-enrich | deepened N pages`. "
          "Then respond `[SILENT]`.")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
