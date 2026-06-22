#!/usr/bin/env python3
"""Normalize foreign-schema entities to the native vault schema.

Agent crons (entity-backfill / raw-backfill) sometimes create or rewrite
entities in a foreign schema:
    type: entity
    entity_type: alpha       # the REAL type, demoted to a sub-field
    name: Example Corp
    description: "..."
…instead of the native schema (`type: alpha` + `tags:`). The
entity-backfill SKILL.md explicitly forbids this (it maps to no index.md
section → the entity is orphaned under `untyped`), but the agent does it
anyway. This deterministic script is the durable backstop.

For each entity with `type: entity` AND a sibling type-indicator field
(one of: entity_type, category, entity_kind, kind, subtype — five foreign
variants observed in the wild):
  1. Map the indicator value → canonical native type via the pack's
     `type_aliases:` map (schema.yaml). Values not in the map pass through
     unchanged — still better than the orphaning `entity`.
  2. Rewrite `type: entity` → `type: <native>`.
  3. Delete the now-redundant indicator line.
  4. Leave EVERYTHING else byte-identical (name, description, aliases,
     sources, created, updated, body).

Entities with `type: entity` and NO recognized indicator field are left
alone (genuinely untyped — a separate lint concern needing judgment).

The engine ships ZERO domain taxonomy: the indicator→type alias map is read
at runtime from the governing schema.yaml `type_aliases:` (default {} = no
remap). The knowledge namespace it scans is taken from the schema's declared
namespaces (default "entities").

Idempotent. Skips backup/_archived artifacts. Skips+logs host-owned files
it can't write (mount-namespace ownership divergence).

Does NOT touch tags — curated-tag restoration on the clobbered subset is a
separate one-time concern (restore_clobbered_tags.py).
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

# Type-indicator field names, in priority order. When `type: entity`, the
# first of these that's present carries the real type.
INDICATOR_KEYS = ("entity_type", "category", "entity_kind", "kind", "subtype")

_FM_RE = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*(?:\n|\Z))", re.S)
_TYPE_ENTITY_RE = re.compile(r"^type:[ \t]*entity[ \t]*$", re.MULTILINE)


def _indicator(fm_body: str) -> tuple[str, str] | None:
    """Return (key, value) of the first present indicator field, or None.
    Strips surrounding quotes and lowercases the value."""
    for key in INDICATOR_KEYS:
        m = re.search(rf"^{key}:[ \t]*[\"']?([A-Za-z0-9_-]+)[\"']?[ \t]*$",
                      fm_body, re.MULTILINE)
        if m:
            return key, m.group(1).lower()
    return None

_SKIP_SUBSTRINGS = (".bak", "_archived/", ".was-broken", ".restored", ".corrupt")


def _is_skippable(path: Path) -> bool:
    s = str(path)
    return any(frag in s for frag in _SKIP_SUBSTRINGS)


def canonical_type(entity_type: str, aliases: dict[str, str]) -> str:
    return aliases.get(entity_type.strip(), entity_type.strip())


def normalize_text(text: str, aliases: dict[str, str]) -> tuple[str, str | None]:
    """Return (new_text, change_desc) or (text, None) if not applicable."""
    m = _FM_RE.match(text)
    if not m:
        return text, None
    opening, fm_body, closing = m.group(1), m.group(2), m.group(3)
    rest = text[m.end():]

    if not _TYPE_ENTITY_RE.search(fm_body):
        return text, None
    ind = _indicator(fm_body)
    if ind is None:
        # type: entity but no recognized indicator — genuinely untyped.
        # Out of scope (needs judgment / lint), leave it.
        return text, None

    key, raw_val = ind
    native = canonical_type(raw_val, aliases)

    # 1. type: entity → type: <native>
    new_fm = _TYPE_ENTITY_RE.sub(f"type: {native}", fm_body, count=1)
    # 2. drop the indicator line (whole line incl. its newline)
    new_fm = re.sub(rf"^{key}:[ \t]*[\"']?[A-Za-z0-9_-]+[\"']?[ \t]*\n", "",
                    new_fm, count=1, flags=re.MULTILINE)

    new_text = opening + new_fm + closing + rest
    desc = f"type: entity ({key}: {raw_val}) → type: {native}"
    return new_text, desc


def _entity_namespaces() -> list[str]:
    """Knowledge namespaces this script scans for `type: entity` pages, read
    from the vault schema's declared namespaces. Falls back to "entities" if
    the pack declares none (keeps the historical default without baking in a
    domain taxonomy)."""
    schema = schema_lib.governing_schema(VAULT)
    ns = sorted(schema_lib.knowledge_namespaces(schema))
    return ns or ["entities"]


def main() -> int:
    aliases = schema_lib.type_aliases(schema_lib.governing_schema(VAULT))
    dirs = [VAULT / "wiki" / ns for ns in _entity_namespaces()]
    existing = [d for d in dirs if d.is_dir()]
    if not existing:
        print(f"ERROR: no knowledge namespace dir found under {VAULT / 'wiki'} "
              f"(looked for: {', '.join(d.name for d in dirs)})", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 1

    fixed = 0
    perm_skips = 0
    print("=== normalize-entity-schema ===")
    paths = sorted(p for d in existing for p in d.rglob("*.md"))
    for path in paths:
        if _is_skippable(path):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        if "type: entity" not in text:
            continue
        new_text, desc = normalize_text(text, aliases)
        if desc is None:
            continue
        try:
            path.write_text(new_text)
        except PermissionError:
            perm_skips += 1
            print(f"  ! {path.name}: PERMISSION DENIED (host-owned) — skipped")
            continue
        fixed += 1
        print(f"  + {path.name}: {desc}")

    print()
    print(f"Normalized {fixed} entit(ies).")
    if perm_skips:
        print(f"{perm_skips} skipped for permissions (chmod 646 + re-run).")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
