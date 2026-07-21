#!/usr/bin/env python3
"""okengine.glossary wake-gate.

Scans the vault for `[[glossary/<slug>]]` references whose definition page doesn't exist yet,
and wakes the agent to define the ones that have accrued enough references (mirrors the
concept-backfill signal). Prints a human-readable digest of the candidates, then a final
`{"wakeAgent": bool}` line (the cron-plus wake-gate protocol). LOCAL-ONLY; no writes here —
the agent writes the definition pages via the okengine-write MCP path.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
MIN_REFS = int(os.environ.get(
    "OKENGINE_GLOSSARY_MIN_REFS", os.environ.get("OKENGINE_GLOSSARY_MIN_REFERENCES", "3")
))
# Tolerate the alias/anchor wikilink forms — `[[glossary/api-gateway|API gateway]]` and
# `[[glossary/api-gateway#usage]]` are canonical (rebuild_index / broken-wikilinks-drain / lacuna
# all handle them). The old `]]`-immediately-after-slug pattern counted zero references for a term
# only ever linked by its display alias, so a heavily-used undefined term never reached MIN_REFS.
_LINK = re.compile(r"\[\[glossary/([a-z0-9][a-z0-9-]*)(?:[#|][^\]]*)?\]\]")


def _undefined_terms() -> dict[str, int]:
    """slug -> reference count, for [[glossary/<slug>]] links with no glossary/<slug>.md yet."""
    refs: Counter[str] = Counter()
    for md in WIKI.rglob("*.md"):
        if os.sep + "glossary" + os.sep in str(md):
            continue                       # a term page linking another term shouldn't self-seed
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for slug in _LINK.findall(text):
            refs[slug] += 1
    return {s: n for s, n in refs.items()
            if n >= MIN_REFS and not (WIKI / "glossary" / f"{s}.md").is_file()}


def main() -> int:
    if not WIKI.is_dir():
        print(json.dumps({"wakeAgent": False}))
        return 0
    undefined = _undefined_terms()
    if not undefined:
        print(json.dumps({"wakeAgent": False}))
        return 0
    print(f"{len(undefined)} undefined glossary term(s) at >= {MIN_REFS} references "
          "— define each at glossary/<slug> (type: term):")
    for slug, n in sorted(undefined.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  - {slug}  ({n} references)")
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
