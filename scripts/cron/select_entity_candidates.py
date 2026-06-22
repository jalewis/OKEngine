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
Tracks the set of source filenames already considered + a snapshot of
existing entity filenames so genuine wiki changes (new sources OR new
entities) trigger a fresh run, while idle ticks short-circuit cleanly.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_PATH = HERMES_HOME / "scripts" / "entity-backfill-state.json"
RECENT_SOURCES_N = int(os.environ.get("ENTITY_RECENT_SOURCES", "30"))
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.S)


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
        return {"sources": [], "entities": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {"sources": [], "entities": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


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

    current_sources = sorted(p.name for p in sources_dir.rglob("*.md"))
    current_entities = sorted(p.name for p in entities_dir.rglob("*.md")) if entities_dir.exists() else []

    state = load_state()
    seen_sources = set(state.get("sources", []))
    seen_entities = set(state.get("entities", []))

    new_sources = [s for s in current_sources if s not in seen_sources]
    new_entities = [e for e in current_entities if e not in seen_entities]

    if not new_sources and not new_entities:
        print(f"# entity-backfill: no new sources or entities since last run")
        print(f"**Sources tracked:** {len(current_sources)}")
        print(f"**Entities tracked:** {len(current_entities)}")
        print(f"**Last state recorded:** {state.get('last_run_at', '(never)')}")
        print('{"wakeAgent": false}')
        return 0

    existing_entities = []
    for e in sorted((entities_dir).rglob("*.md")) if entities_dir.exists() else []:
        text = e.read_text(errors="replace")
        fm = parse_frontmatter(text)
        existing_entities.append({
            "filename": e.name,
            "slug": e.stem,
            "type": fm.get("type") or fm.get("source_kind") or "entity",
            "tags": fm.get("tags") or [],
            "title": first_h1(text) or e.stem,
        })

    sources = sorted(
        sources_dir.rglob("*.md"),
        key=lambda p: -p.stat().st_mtime,
    )[:RECENT_SOURCES_N]

    print(f"# Entity-backfill digest — {datetime.now(timezone.utc).isoformat()}\n")
    print(f"**Vault:** `{VAULT}`")
    print(f"**Existing entity pages:** {len(existing_entities)}")
    print(f"**Recent source pages in this window:** {len(sources)} (newest first)")
    print(f"**Total source pages in vault:** {len(current_sources)}")
    print(f"**New source pages since last run:** {len(new_sources)}")
    print(f"**New entity pages since last run:** {len(new_entities)}\n")

    print("## Existing entities (do not duplicate; consider updating with new sources)\n")
    if not existing_entities:
        print("_(none yet)_\n")
    else:
        for e in existing_entities:
            tags = ", ".join(e["tags"][:5]) if e["tags"] else ""
            print(f"- `entities/{e['slug']}` — {e['title']}" + (f" — tags: {tags}" if tags else ""))
        print()

    print("## Recent source pages to scan for entity candidates\n")
    print("Read each, look for organizations/products/actors/notable items that recur across multiple sources and meet the vault CLAUDE.md trajectory rule (worth tracking over time).\n")
    for s in sources:
        text = s.read_text(errors="replace")
        fm = parse_frontmatter(text)
        title = first_h1(text) or s.stem
        publisher = fm.get("publisher") or ""
        published = fm.get("published") or ""
        is_new = " — **NEW SINCE LAST RUN**" if s.name in new_sources else ""
        print(f"- `sources/{s.stem}` — {title}" + (f" ({publisher}, {published})" if publisher or published else "") + is_new)
    print()

    state["sources"] = current_sources
    state["entities"] = current_entities
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
