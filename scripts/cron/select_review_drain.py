#!/usr/bin/env python3
"""Bounded local-evidence review drain for substantive ``needs_review`` pages (#240).

Prioritizes the oldest flagged pages that cite local source pages. The agent reads
the page and its complete local evidence set, then records a machine assessment.
Machine assessment never impersonates a human reviewer or clears quarantine.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
BATCH = int(os.environ.get("REVIEW_DRAIN_BATCH", "20"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)


def _existing_source_refs(wiki: Path, values) -> list[str]:
    values = [values] if isinstance(values, str) else values
    if not isinstance(values, list):
        return []
    out = []
    for value in values:
        rel = str(value).strip().strip("[] ")
        if rel.endswith(".md"):
            rel = rel[:-3]
        if not rel.startswith("sources/"):
            continue
        direct = wiki / f"{rel}.md"
        if direct.is_file():
            out.append(rel)
            continue
        # A stale partition prefix can survive a source reshard. Resolve only a
        # unique basename; ambiguity is not enough evidence to review against.
        slug = Path(rel).name
        hits = [p for p in (wiki / "sources").rglob(f"{slug}.md") if p.stem == slug]
        if len(hits) == 1:
            out.append(hits[0].relative_to(wiki).as_posix()[:-3])
    return out


def candidates(vault: Path) -> list[dict]:
    wiki = vault / "wiki"
    out = []
    for p in wiki.rglob("*.md"):
        rel = p.relative_to(wiki)
        if {"dashboards", "operational", "_archived"}.intersection(rel.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _FM.match(text)
        if not match:
            continue
        try:
            fm = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            continue
        body = match.group(2).strip()
        if not isinstance(fm, dict) or fm.get("needs_review") is not True or len(body) <= 200:
            continue
        refs = _existing_source_refs(wiki, fm.get("sources") or [])
        if not refs:
            continue
        stamp = str(fm.get("last_updated") or fm.get("updated") or fm.get("created") or "9999")
        out.append({"path": rel.as_posix()[:-3], "stamp": stamp, "sources": refs})
    return sorted(out, key=lambda item: (item["stamp"], item["path"]))


def main() -> int:
    chosen = candidates(VAULT)[:BATCH]
    if not chosen:
        print("# review-drain: no substantive, locally-groundable needs_review pages")
        # JSON sentinel, not a bare string: the Hermes cron wake-gate parses ONLY the last stdout
        # line as JSON and FAILS OPEN (any non-JSON line → wake the agent). A bare "wakeAgent=false"
        # raises in json.loads, so the no-work path woke a WRITE-capable agent anyway (audit HIGH #8).
        print(json.dumps({"wakeAgent": False}))
        return 0
    print("=== review-drain batch ===")
    print("Review each page against the listed LOCAL source pages. Do not use web tools.")
    for item in chosen:
        print(f"\n- page: {item['path']}")
        print(f"  flagged/updated: {item['stamp']}")
        print("  evidence:")
        for source in item["sources"]:
            print(f"    - {source}")
    print(
        "\nFor each page: read it and every cited source shown above. Use "
        "record_machine_review with verdict=supported, unsupported, or unresolved and a "
        "specific note. Machine review is evidence preparation only: never set reviewed_by, "
        "never clear needs_review, and never use update_entity to manufacture human approval. "
        "End with [SILENT]."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
