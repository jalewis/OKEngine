#!/usr/bin/env python3
"""relink_prose_sources.py — remediation for the `source-refs-are-pages` conformance rule
(okengine#158 P2, the deterministic "propose" half).

An entity whose `sources:` carries a PROSE entry ("ESET Gamaredon report") links nothing in the
graph. This drain converts such an entry into a source-PAGE ref (`sources/<path>`) ONLY when the
prose maps CONFIDENTLY to exactly one source page: every distinctive token of the prose
(len>=4, not a generic word) appears in that source's slug, and exactly one source matches. That
strictness is deliberate — a wrong citation is worse than an honest prose one (no-fabricated-facts),
so anything ambiguous or unmatched is LEFT untouched and stays flagged on the conformance dashboard
for the LLM/human "dispose" half (entity-backfill).

Additive-safe (rewrites only the matched item line, preserves order + other keys + comments),
idempotent (a page-ref entry is skipped), batched (RELINK_BATCH entities/run). no_agent.

Env: WIKI_PATH (default /opt/vault) · RELINK_BATCH (default 200)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
BATCH = int(os.environ.get("RELINK_BATCH", "200"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
# Generic words that carry no identifying signal — dropped from prose before matching, so a vague
# "Vendor advisory" yields no distinctive token and is correctly left alone.
_STOP = {"report", "reports", "advisory", "advisories", "disclosure", "blog", "post", "article",
         "source", "sources", "news", "update", "updates", "analysis", "research", "bulletin",
         "alert", "brief", "briefing", "the", "and", "for", "via", "from", "with", "release",
         "writeup", "write-up", "paper", "study", "review", "page", "site", "feed"}


def _tokens(prose: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", prose.lower()) if len(t) >= 4 and t not in _STOP}


def _source_index() -> dict[str, set[str]]:
    """{ source-rel-path (no .md) : set of slug tokens }."""
    idx = {}
    base = WIKI / "sources"
    if not base.is_dir():
        return idx
    for p in base.rglob("*.md"):
        if p.name.startswith(("_", "INDEX")) or p.name == "INDEX.md":
            continue
        rel = p.relative_to(WIKI).as_posix()[:-3]
        idx[rel] = {t for t in re.split(r"[^a-z0-9]+", p.stem.lower()) if len(t) >= 4}
    return idx


def _match(prose: str, idx: dict[str, set[str]]) -> str | None:
    """The UNIQUE source page whose slug contains every distinctive prose token, else None."""
    toks = _tokens(prose)
    if not toks:
        return None
    hits = [rel for rel, stoks in idx.items() if toks <= stoks]
    return hits[0] if len(hits) == 1 else None


def _is_page_ref(entry: str) -> bool:
    return schema_lib.is_page_ref(entry)


def relink_text(text: str, idx: dict[str, set[str]]) -> tuple[str, int]:
    """Rewrite prose `sources:` items that map to a unique source page. Returns (new_text, n)."""
    m = _FM.match(text)
    if not m:
        return text, 0
    lines = text.splitlines(keepends=False)
    try:
        s = next(i for i, ln in enumerate(lines) if re.match(r"^sources:\s*$", ln))
    except StopIteration:
        return text, 0
    n = 0
    j = s + 1
    while j < len(lines):
        im = re.match(r"^(\s*)-\s+(.*\S)\s*$", lines[j])
        if not im:
            if re.match(r"^\S", lines[j]):   # next top-level key — end of block
                break
            j += 1
            continue
        indent, entry = im.group(1), im.group(2).strip().strip('"\'')
        if not _is_page_ref(entry):
            tgt = _match(entry, idx)
            if tgt:
                lines[j] = f"{indent}- {tgt}"
                n += 1
        j += 1
    return ("\n".join(lines) + ("\n" if text.endswith("\n") else ""), n) if n else (text, 0)


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    idx = _source_index()
    edited = relinked = 0
    for p in (WIKI / "entities").rglob("*.md") if (WIKI / "entities").is_dir() else []:
        if edited >= BATCH:
            break
        if p.name.startswith(("_", "INDEX")):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if "sources:" not in text:
            continue
        new, n = relink_text(text, idx)
        if n:
            p.write_text(new, encoding="utf-8")
            edited += 1
            relinked += n
    print(f"relink-prose-sources: {relinked} prose entr(ies) -> page-refs across {edited} "
          f"entit(ies) (batch {BATCH}); ambiguous/unmatched left flagged for the LLM pass")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
