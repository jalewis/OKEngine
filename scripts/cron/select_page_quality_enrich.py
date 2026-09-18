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
  PQ_ENRICH_BATCH           pages per run (default 1)
  PQ_ENRICH_CTX             inbound sources surfaced per page (default 6)
  PQ_ENRICH_QUEUE           queue path override (default wiki/operational/page-quality-queue.json)
  ENRICH_COOLDOWN_DAYS      don't re-enrich within N days (default 21)
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from selection_manifest import write_selection_manifest  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
STATE = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "scripts" / "page-quality-enrich-state.json"
_queue_override = os.environ.get("PQ_ENRICH_QUEUE")
QUEUE = Path(_queue_override) if _queue_override else WIKI / "operational" / "page-quality-queue.json"
if not QUEUE.is_absolute():
    QUEUE = VAULT / QUEUE
BATCH = int(os.environ.get("PQ_ENRICH_BATCH", "1"))
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


def _import_receipts(state: dict, today) -> int:
    """Start cooldown only after a verified terminal receipt."""
    lane_id = os.environ.get("OKENGINE_LANE_ID", "")
    if not lane_id:
        return 0
    receipt_dir = Path(os.environ.get("HERMES_HOME", "/opt/data")) / \
        "cron-plus" / "receipts" / lane_id
    imported = set(state.get("_imported_receipts") or [])
    count = 0
    # glob-ok: receipt files are intentionally flat under one lane-id directory.
    for path in sorted(receipt_dir.glob("*.json")):
        key = str(path.resolve())
        if key in imported:
            continue
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if document.get("valid") is False:
            continue
        receipt = document.get("receipt") or document
        for item in receipt.get("items") or []:
            if item.get("disposition") in {"accepted", "duplicate", "skipped"}:
                key = str(item.get("key") or "").split("|", 1)[0]
                state[key.removeprefix("wiki:").removeprefix("wiki/").removesuffix(".md")] = today.isoformat()
                count += 1
        imported.add(key)
    state["_imported_receipts"] = sorted(imported)
    return count


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
        print(f"# no page-quality queue at {QUEUE} — run page-quality-audit first")
        print(json.dumps({"wakeAgent": False}))
        return 0
    try:
        queue = json.loads(QUEUE.read_text())
    except (OSError, ValueError):
        print(json.dumps({"wakeAgent": False}))
        return 0

    state = _load_state()
    _import_receipts(state, today)
    _save_state(state)
    def _nskey(s: str) -> str:
        # <namespace>/<slug> from a page path or wikilink target — collapse partition/shard/alias/
        # anchor so an entity and a concept sharing a slug never share an inbound bucket (the bare-
        # stem key cross-wired homographs: a deficient entity got a concept's inbound context). '' if
        # not namespace-qualified (a bare `[[slug]]` link is ambiguous — don't guess a namespace).
        parts = [x for x in s.strip().strip("[]").split("|", 1)[0].split("#", 1)[0].strip().split("/") if x]
        return f"{parts[0]}/{parts[-1]}" if len(parts) >= 2 else ""

    # candidates: deficient, inbound>=1, not enriched within cooldown; keep queue order (inbound desc)
    cands = []
    seen_pages = set()
    for q in queue:
        page = str(q.get("page") or "")
        if (
            page
            and page not in seen_pages
            and Path(page).name.casefold() != "index"
            and q.get("inbound", 0) >= 1
            and _days_since(state.get(page, "2000-01-01"), today) >= COOLDOWN
        ):
            cands.append(q)
            seen_pages.add(page)
    targets = {_nskey(q["page"]): q for q in cands[:BATCH * 3] if _nskey(q["page"])}  # over-select

    # one pass over sources: collect inbound (source path, excerpt) per target <ns>/<slug>
    inbound_ctx: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in (WIKI / "sources").rglob("*.md"):
        if p.name.startswith("_") or "_archive" in p.parts:
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        links = {_nskey(l) for l in _WIKILINK_RE.findall(txt)}
        links.discard("")
        hit = links & targets.keys()
        for key in hit:
            if len(inbound_ctx[key]) < CTX:
                ex = _excerpt_around(txt, key.split("/")[-1])
                inbound_ctx[key].append((p.relative_to(WIKI).as_posix(), ex))

    # build batch: queue order, but only pages with usable inbound context
    batch = []
    for q in cands:
        key = _nskey(q["page"])
        target = WIKI / f"{q['page']}.md"
        if target.is_file() and key in inbound_ctx and inbound_ctx[key]:
            batch.append((q, inbound_ctx[key]))
        if len(batch) >= BATCH:
            break

    print("=== page-quality-enrich wake-gate ===")
    print(f"  queue size: {len(queue)}  eligible (cooldown {COOLDOWN}d): {len(cands)}  this batch: {len(batch)}")
    if not batch:
        print("  → SKIP: nothing to enrich")
        print(json.dumps({"wakeAgent": False}))
        return 0

    selected = []
    for q, _ in batch:
        target = WIKI / f"{q['page']}.md"
        revision = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        selected.append(f"wiki/{q['page']}.md|{revision}")
    manifest = write_selection_manifest(
        selected,
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "cron-plus" / "selections" / "page-quality-enrich.json",
    )

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
    print(f"selection input_digest: {manifest['input_digest']}")
    print()
    receipt = {
        "api": 1,
        "lane_id": manifest["lane_id"],
        "contract_digest": manifest["contract_digest"],
        "input_digest": manifest["input_digest"],
        "items": [{
            "key": selected[index],
            "disposition": "<accepted|duplicate|skipped|rejected|failed|deferred>",
            "writes": [{"path": f"wiki/{q['page']}.md", "sha256": None}],
            "reason": "<required unless accepted>",
        } for index, (q, _) in enumerate(batch)],
    }
    print("FINAL RESPONSE CONTRACT (MANDATORY): return ONLY this fenced receipt. "
          "Remove placeholder writes from non-accepted items.")
    print("```okengine-receipt")
    print(json.dumps(receipt, indent=2))
    print("```")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
