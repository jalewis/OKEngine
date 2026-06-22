#!/usr/bin/env python3
"""source_backlink_drain.py — reconnect declared source→entity evidence.

Many source pages declare, in their body, that they update an entity:

    ## Wiki Impact
    - Updates [[entities/example-corp]] — updated positioning, new field value...

…but the named entity never lists that source in its `sources:` frontmatter.
The signal is captured at the source layer and never reaches the entity
layer: verb-declared source→entity pairs where the entity exists but does
not backlink the source.

This drain closes the DETERMINISTIC half: it adds `[[sources/<stem>]]` to
the declared entity's `sources:` list. It is:
  - additive only (never drops or reorders other keys → write-guard-safe,
    preserves inline comments in entity frontmatter),
  - idempotent (skips if the backlink already exists anywhere in the file),
  - batched (BACKLINK_DRAIN_BATCH entities per run, newest sources first),
  - script-only (wakeAgent=false).

The JUDGMENT half — folding the source's content into the entity body and
its fields — is left to the existing LLM `entity-backfill` cron, which will
now see the reconnected sources. Propose (this, deterministic) / dispose
(entity-backfill, LLM).

Env:
  WIKI_PATH               vault root (default /opt/vault)
  BACKLINK_DRAIN_BATCH    max entity edits per run (default 200)
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
SRC_DIR = VAULT / "wiki" / "sources"
ENT_DIR = VAULT / "wiki" / "entities"
OUT_DIR = VAULT / "wiki" / "operational"
BATCH = int(os.environ.get("BACKLINK_DRAIN_BATCH", "200"))

_FM = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*(?:\n|\Z))", re.S)

# A declaration = an update/create/enrich verb stem, then up to 30 non-bracket
# characters on the same logical span, then an entities wikilink. The verb
# anchor avoids backlinking entities merely mentioned for context.
_DECL = re.compile(
    r"(?:new entity|creat|updat|enrich|affect)[a-z]*[^\[\]\n]{0,30}"
    r"\[\[entities/([a-z0-9][a-z0-9-]*)",
    re.I,
)


def parse_declarations(text: str) -> set[str]:
    """Return the set of entity slugs a source body declares it updates."""
    return {m.lower() for m in _DECL.findall(text)}


def insert_source_backlink(entity_text: str, source_stem: str) -> tuple[str, bool]:
    """Add `[[sources/<source_stem>]]` to the entity's `sources:` frontmatter.

    Returns (new_text, changed). Idempotent: if the backlink already appears
    anywhere in the file, returns it unchanged. Preserves all other
    frontmatter (including inline comments) by editing surgically rather than
    re-dumping YAML. Skips files with no frontmatter.
    """
    link = f"[[sources/{source_stem}]]"
    if link in entity_text:
        return entity_text, False

    m = _FM.match(entity_text)
    if not m:
        return entity_text, False  # no frontmatter — can't place a backlink
    open_d, fm, close_d = m.group(1), m.group(2), m.group(3)
    rest = entity_text[m.end():]
    item = f'  - "{link}"'

    lines = fm.split("\n")
    src_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^sources:", ln)), None
    )

    if src_idx is None:
        # No sources key — append one at the end of the frontmatter block.
        new_fm = fm.rstrip("\n") + f"\nsources:\n{item}\n"
        return open_d + new_fm + close_d + rest, True

    head = lines[src_idx]

    # inline empty `sources: []`  → convert to a block list
    if re.match(r"^sources:\s*\[\s*\]\s*$", head):
        lines[src_idx] = "sources:"
        lines.insert(src_idx + 1, item)
        return open_d + "\n".join(lines) + close_d + rest, True

    # inline non-empty `sources: [a, b]`  → insert before the closing bracket
    inline = re.match(r"^(sources:\s*\[)(.*)(\]\s*)$", head)
    if inline:
        pre, body, post = inline.group(1), inline.group(2).rstrip(), inline.group(3)
        sep = ", " if body and not body.endswith(",") else " " if body else ""
        lines[src_idx] = f'{pre}{body}{sep}"{link}"{post.rstrip()}'
        return open_d + "\n".join(lines) + close_d + rest, True

    # block list `sources:` followed by `  - ...` items → append after the
    # last contiguous list item (preserve the existing item indentation)
    j = src_idx + 1
    last_item = None
    indent = "  "
    while j < len(lines) and re.match(r"^(\s*)-\s", lines[j]):
        last_item = j
        indent = re.match(r"^(\s*)-", lines[j]).group(1)
        j += 1
    block_item = f'{indent}- "{link}"'
    if last_item is not None:
        lines.insert(last_item + 1, block_item)
    else:
        # `sources:` with no items underneath (bare key) → add the first item
        lines.insert(src_idx + 1, block_item)
    return open_d + "\n".join(lines) + close_d + rest, True


def _entity_index() -> dict[str, Path]:
    return {p.stem: p for p in ENT_DIR.rglob("*.md")} if ENT_DIR.is_dir() else {}


def main() -> int:
    now = datetime.now(timezone.utc)
    print("=== source-backlink-drain ===")
    print(f"  vault: {VAULT}  batch: {BATCH}")

    ent_idx = _entity_index()
    if not ent_idx or not SRC_DIR.is_dir():
        print("  no entities/sources dir; nothing to do")
        print('{"wakeAgent": false}')
        return 0

    # Newest sources first (date-prefixed filenames sort lexically by recency).
    # rglob: sources may be sharded (sources/<year>/<month>/); skip shard INDEX pages.
    sources = sorted((p for p in SRC_DIR.rglob("*.md")
                      if p.name != "INDEX.md" and not p.name.startswith(("_", "INDEX-"))),
                     key=lambda p: p.name, reverse=True)

    applied: list[tuple[str, str]] = []  # (entity_slug, source_stem)
    scanned = 0
    for sp in sources:
        if len(applied) >= BATCH:
            break
        try:
            txt = sp.read_text(errors="replace")
        except OSError:
            continue
        decls = parse_declarations(txt)
        if not decls:
            continue
        scanned += 1
        for slug in sorted(decls):
            if len(applied) >= BATCH:
                break
            ep = ent_idx.get(slug)
            if ep is None:
                continue  # 'new entity needed' — out of scope for this drain
            try:
                etxt = ep.read_text(errors="replace")
            except OSError:
                continue
            new_txt, changed = insert_source_backlink(etxt, sp.stem)
            if changed:
                ep.write_text(new_txt, encoding="utf-8")
                applied.append((slug, sp.stem))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"source-backlink-drain-{now.date().isoformat()}.md"
    lines = [
        "---",
        "type: operational",
        "generated_by: source_backlink_drain.py",
        f"created: {now.date().isoformat()}",
        "tags: [sources, entities, propagation, drain, hygiene]",
        "---",
        "",
        f"# Source→Entity Backlink Drain — {now.date().isoformat()}",
        "",
        f"Reconnected **{len(applied)}** declared source→entity backlinks "
        f"this run (batch cap {BATCH}). Each adds `[[sources/<stem>]]` to the "
        "entity's `sources:` list so the LLM entity-backfill cron can fold in "
        "the content.",
        "",
    ]
    if applied:
        lines += ["| Entity | Source reconnected |", "|---|---|"]
        lines += [f"| [[entities/{e}]] | [[sources/{s}]] |" for e, s in applied]
    else:
        lines.append("✅ No unapplied source→entity declarations found in this pass.")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(out, 0o646)
    except OSError:
        pass

    print(f"  scanned sources with declarations (until batch full): {scanned}")
    print(f"  backlinks applied: {len(applied)}  → {out.relative_to(VAULT)}")
    print('{"wakeAgent": false}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
