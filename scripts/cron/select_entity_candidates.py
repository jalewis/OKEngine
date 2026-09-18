#!/usr/bin/env python3
"""Build a digest of recent source pages + existing entity inventory.

Output is consumed by the `entity-backfill` cron job, which scans the digest
to identify recurring entities that meet the vault's trajectory rule and
either creates new entity pages or updates existing ones.

This script does NOT decide what should be an entity — that's the agent's
synthesis work. It just bounds the input to a manageable window so the model
can reason about a focused set of pages instead of all 250+.

Wake-gate (Hermes cron pre-run script convention, scheduler.py:606):
- If no new source pages have appeared since the last run AND no entity
  page has been edited since then, the script's final line is
  `{"wakeAgent": false}`, which tells Hermes' scheduler to skip the LLM
  invocation entirely — no agent run, no delivery, no cost. The script
  itself is free.
- If there's new work, the digest is emitted as before. The wake-gate
  defaults to true when absent.

State file: $HERMES_HOME/scripts/entity-backfill-state.json
Tracks verified source-content revisions. Selected revisions remain pending
until a cron-plus completion receipt proves that the agent updated an entity or
explicitly disposed the source as having no entity-worthy evidence.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from selection_manifest import write_selection_manifest

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_PATH = HERMES_HOME / "scripts" / "entity-backfill-state.json"
RECENT_SOURCES_N = int(os.environ.get("ENTITY_RECENT_SOURCES", "1"))
SOURCE_EVIDENCE_MAX_CHARS = int(os.environ.get("ENTITY_SOURCE_MAX_CHARS", "16000"))
CONTROLLED_TARGET = os.environ.get("ENTITY_BACKFILL_TARGET", "").strip().lstrip("/")
# Cap on the "related entities" hint list (okengine#476). The digest used to inline EVERY entity
# page in the vault so the model would not create duplicates — O(vault), 8,994 entities ≈ 116k
# tokens on okcti, 94% of the prompt, to reconcile 30 sources. On any model whose window is
# smaller than the vault's entity list the prompt overflows and is SILENTLY truncated, so the lane
# emits a valid receipt having read nothing. Duplicate-avoidance is not the prompt's job: it is
# already enforced deterministically at the write path by write_server._dedup_on_create, backed by
# id_index's name/alias -> path resolver. The list below is a convenience so the model UPDATES
# rather than re-creates, and is explicitly non-exhaustive.
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)
# Tokens too generic to imply a relationship between a source and an entity page.
_STOPWORDS = frozenset("""
a an and are as at be by for from has have in into is it its of on or that the to was were with
new report reports update updates group groups attack attacks campaign campaigns threat actor
security cyber data page index source sources entity entities
""".split())


def is_candidate_source(path: Path, sources_dir: Path) -> bool:
    """Exclude derived indexes and unsafe/corrupt filenames from agent input."""
    try:
        rel = str(path.relative_to(sources_dir))
    except ValueError:
        return False
    return (
        path.name.casefold() != "index.md"
        and not any(ord(char) < 32 or ord(char) == 127 for char in rel)
    )


def _tokens(text: str) -> set:
    """Normalized word tokens, minus stopwords and 1-2 char noise."""
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if len(t) > 2 and t not in _STOPWORDS}


MAX_RELATED_ENTITIES = 200


def related_entities(existing: list, source_texts: list) -> list:
    """Entities plausibly referenced by the selected sources.

    Deterministic join (okengine#476, and the okengine#469 rule — compute it, don't ask the model
    to). An entity is related when every token of its title/slug appears in the combined text of
    the selected sources. O(entities x title tokens) with set lookups, and the OUTPUT is bounded
    by the selection, not by vault size.
    """
    corpus = set()
    for text in source_texts:
        corpus |= _tokens(text)
    if not corpus:
        return []
    hits = []
    for e in existing:
        for field in (e.get("title") or "", e.get("slug") or ""):
            toks = _tokens(str(field))
            if toks and toks <= corpus:
                hits.append(e)
                break
    return hits[:MAX_RELATED_ENTITIES]


def parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def first_h1(text: str) -> str:
    body = text
    m = FRONTMATTER_RE.match(text)
    if m:
        body = text[m.end():]
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line and not line.startswith(("#", "-", "*", ">")):
            return line[:120]
    return ""


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"source_revisions": {}, "sources": [], "entities": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"source_revisions": {}, "sources": [], "entities": []}


def source_revision(path: Path) -> str:
    """Return a content identity so an in-place repair is new downstream work. The read is guarded so
    a source that vanishes mid-scan (a mover lane relocating it) raises OSError for the caller's
    glob-loop to skip, instead of an unguarded read of a path that no longer exists (scan-race rule)."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


def import_receipts(state: dict) -> int:
    """Acknowledge only revisions with verified, non-retryable dispositions."""
    lane_id = os.environ.get("OKENGINE_LANE_ID", "")
    if not lane_id:
        return 0
    receipt_dir = HERMES_HOME / "cron-plus" / "receipts" / lane_id
    imported = set(state.get("imported_receipts") or [])
    revisions = state.setdefault("source_revisions", {})
    count = 0
    for path in sorted(receipt_dir.glob("*.json")):  # glob-ok: cron-plus receipts/<lane_id>/ is a FLAT per-run dir, not a sharded content namespace
        receipt_key = str(path.resolve())
        if receipt_key in imported:
            continue
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and document.get("valid") is False:
            continue
        receipt = document.get("receipt") if isinstance(document, dict) else None
        receipt = receipt if isinstance(receipt, dict) else document
        for item in receipt.get("items") or []:
            if not isinstance(item, dict) or item.get("disposition") not in {
                    "accepted", "duplicate", "skipped"}:
                continue
            key = str(item.get("key") or "")
            rel, separator, revision = key.rpartition("|")
            if separator and rel and revision.startswith("sha256:"):
                revisions[rel] = revision
                count += 1
        imported.add(receipt_key)
    state["imported_receipts"] = sorted(imported)
    return count


def main() -> int:
    if not VAULT.exists():
        print(f"# entity-backfill: vault not found at `{VAULT}`")
        print('{"wakeAgent": false}')
        return 1

    sources_dir = VAULT / "wiki" / "sources"
    entities_dir = VAULT / "wiki" / "entities"
    if not sources_dir.exists():
        print(f"# entity-backfill: `{sources_dir}` does not exist")
        print('{"wakeAgent": false}')
        return 1

    source_paths = sorted(
        path for path in sources_dir.rglob("*.md")
        if is_candidate_source(path, sources_dir)
    )
    current_sources = sorted(str(p.relative_to(sources_dir)) for p in source_paths)
    current_entities = sorted(p.name for p in entities_dir.rglob("*.md")) if entities_dir.exists() else []

    state = load_state()
    imported = import_receipts(state)
    if imported:
        print(f"# entity-backfill: acknowledged {imported} verified source disposition(s)")
    prior_revisions = state.get("source_revisions") or {}
    if not isinstance(prior_revisions, dict):
        prior_revisions = {}
    # `sources` is the pre-revision checkpoint format. Treat matching basenames as
    # already seen during the one-time migration so an engine upgrade does not
    # enqueue the entire corpus.
    legacy_seen = (set(state.get("sources", []))
                   if "source_revisions" not in state else set())
    seen_entities = set(state.get("entities", []))
    current_revisions = {}
    source_by_rel = {}
    for path in source_paths:
        rel = str(path.relative_to(sources_dir))
        try:
            current_revisions[rel] = source_revision(path)
            source_by_rel[rel] = path
        except OSError:
            continue
    changed_sources = [rel for rel, revision in current_revisions.items()
                       if prior_revisions.get(rel) != revision
                       and not (not prior_revisions and Path(rel).name in legacy_seen)]
    all_changed_sources = list(changed_sources)
    if CONTROLLED_TARGET:
        target = CONTROLLED_TARGET.removeprefix("wiki/sources/").removeprefix("sources/")
        changed_sources = [rel for rel in changed_sources if rel == target]
        if not changed_sources:
            print(f"# entity-backfill: controlled target is not pending: {target}")
            print('{"wakeAgent": false}')
            return 0
    new_sources = [rel for rel in changed_sources if rel not in prior_revisions]
    repaired_sources = [rel for rel in changed_sources if rel in prior_revisions]
    new_entities = [e for e in current_entities if e not in seen_entities]

    if not changed_sources:
        # Complete a legacy state migration without waking the model.
        state["source_revisions"] = current_revisions
        state["sources"] = current_sources
        state["entities"] = current_entities
        save_state(state)
        print(f"# entity-backfill: no new or revised sources or entities since last run")
        print(f"**Sources tracked:** {len(current_sources)}")
        print(f"**Entities tracked:** {len(current_entities)}")
        print(f"**Last state recorded:** {state.get('last_run_at', '(never)')}")
        print('{"wakeAgent": false}')
        return 0

    existing_entities = []
    for e in sorted((entities_dir).rglob("*.md")) if entities_dir.exists() else []:
        try:
            text = e.read_text(errors="replace")
        except OSError:
            continue  # page moved/deleted by a concurrent lane mid-scan
        fm = parse_frontmatter(text)
        existing_entities.append({
            "filename": e.name,
            "slug": e.stem,
            "type": fm.get("type") or fm.get("source_kind") or "entity",
            "tags": fm.get("tags") or [],
            "title": first_h1(text) or e.stem,
        })

    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0  # vanished mid-scan; sorts last and the read guard below skips it

    pending_paths = [source_by_rel[rel] for rel in changed_sources if rel in source_by_rel]
    sources = sorted(pending_paths, key=lambda p: -_mtime(p))[:RECENT_SOURCES_N]
    selected_rel = [str(path.relative_to(sources_dir)) for path in sources]

    # Read each selected source ONCE — the related-entity join and the source listing below both
    # need the text, and re-reading is the only other option. Pages that vanish mid-scan (a
    # concurrent lane moved/deleted one) simply drop out, as before.
    source_texts = {}
    for s in sources:
        try:
            source_texts[s] = s.read_text(errors="replace")
        except OSError:
            continue
    sources = [s for s in sources if s in source_texts]
    selected_rel = [str(path.relative_to(sources_dir)) for path in sources]

    print(f"# Entity-backfill digest — {datetime.now(timezone.utc).isoformat()}\n")
    print(f"**Vault:** `{VAULT}`")
    print(f"**Existing entity pages:** {len(existing_entities)}")
    print(f"**Pending source revisions in this window:** {len(sources)} (newest first)")
    print(f"**Total source pages in vault:** {len(current_sources)}")
    print(f"**New source pages since last run:** {len(new_sources)}")
    print(f"**Repaired or revised source pages since last run:** {len(repaired_sources)}")
    print(f"**Source revisions left pending after this window:** {len(changed_sources) - len(sources)}")
    print(f"**New entity pages since last run:** {len(new_entities)}\n")

    print("## Existing-entity reconciliation\n")
    print(
        "Do not search, list, or read entity pages. Read only the selected source below, "
        "then call `converge_entity` for each durable subject it supports. The governed "
        "writer resolves canonical IDs and aliases and refuses duplicate creation. After "
        "the write calls, emit the receipt immediately.\n"
    )

    print("## New or revised source evidence to reconcile with entities\n")
    print(
        "The source evidence is embedded below. It is the complete evidence available "
        "to this lane; do not call any read/search tool. Create or update durable entities, "
        "cite the canonical source reference, and preserve claim-specific confidence. "
        "Select exactly the ONE highest-value durable entity from this source. The "
        "`converge_entity.path` MUST start with `entities/`; NEVER pass a `sources/` or "
        "`concepts/` path as the write target. Put the selected source reference in the "
        "entity's `sources:` frontmatter instead. Emit one converge_entity call, keep its "
        "body under 600 characters and frontmatter under 12 keys, and do not reproduce "
        "the source text. After that call, return the receipt.\n"
    )
    for s in sources:
        text = source_texts[s]
        fm = parse_frontmatter(text)
        title = first_h1(text) or s.stem
        publisher = fm.get("publisher") or ""
        published = fm.get("published") or ""
        rel = str(s.relative_to(sources_dir))
        marker = "NEW" if rel in new_sources else "REPAIRED/REVISED"
        ref = str(Path("sources") / Path(rel).with_suffix(""))
        print(f"- `{ref}` — {title}" + (f" ({publisher}, {published})" if publisher or published else "") + f" — **{marker} SINCE LAST RUN**")
        evidence = text[:SOURCE_EVIDENCE_MAX_CHARS]
        print(f"\n```source-evidence path=wiki/{ref}.md")
        print(evidence)
        print("```")
        if len(text) > len(evidence):
            print(
                f"\n_Evidence truncated at {SOURCE_EVIDENCE_MAX_CHARS} of {len(text)} "
                "characters; defer claims not supported by the embedded excerpt._"
            )
    print()

    selected = [f"{rel}|{current_revisions[rel]}" for rel in selected_rel]
    manifest = write_selection_manifest(
        selected, VAULT / ".okengine" / "entity-reconciliation-selection.json")
    receipt_template = {
        "api": 1,
        "lane_id": manifest["lane_id"],
        "contract_digest": manifest["contract_digest"],
        "input_digest": manifest["input_digest"],
        "items": [{
            "key": key,
            "disposition": "<accepted|duplicate|skipped|rejected|failed|deferred>",
            "writes": [{"path": "wiki/entities/<path>.md",
                        "sha256": None}],
            "reason": "<required unless accepted>",
        } for key in selected],
    }
    print("FINAL RESPONSE CONTRACT (MANDATORY): return ONLY this fenced receipt; ")
    print("use skipped with a reason when a source has no entity-worthy evidence; ")
    print("remove placeholder writes from every non-accepted item; the runner supplies ")
    print("the authoritative post-write sha256 for accepted writes.")
    print("```okengine-receipt")
    print(json.dumps(receipt_template, indent=2))
    print("```")

    # Selection is not acknowledgement. Only a verified receipt imported by a
    # later run advances a changed revision; failed runs therefore retry.
    checkpoint = {rel: revision for rel, revision in current_revisions.items()
                  if rel not in all_changed_sources}
    state["source_revisions"] = checkpoint
    state["sources"] = current_sources  # retained for downgrade compatibility
    state["entities"] = current_entities
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return 0


if __name__ == "__main__":
    # DeepSeek off-peak deferral (CRON_DEFER_UTC_HOURS): during the configured peak UTC window
    # emit nothing — cron-plus wakes the agent only on non-empty stdout (scheduler.py), so this
    # bulk drain silently defers to the next off-peak fire (no model call at 2x price).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from offpeak import offpeak_defer
    if offpeak_defer():
        sys.exit(0)
    sys.exit(main())
