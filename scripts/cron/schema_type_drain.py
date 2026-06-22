#!/usr/bin/env python3
"""schema_type_drain.py — drain the unambiguous slice of schema-drift.

lint_watcher counts a page as schema-drift when its `type:` is neither
canonical (declared in the pack's schema.yaml `types:`) nor operational. There
was NO auto-drain (DRAIN_OWNER["schema-drift"] is None), so the count only grows
as backfill crons mint descriptive types. This is the deterministic backstop for
the SAFE, unambiguous remaps only — type values the PACK has declared in its
`type_aliases:` map (an old/variant value that maps 1:1 to a canonical type with
no judgment), e.g.:

    alias-a -> alpha
    alias-b -> beta

It also fixes case-only drift (e.g. "Alpha" -> "alpha") against the pack's
declared canonical type set. It deliberately does NOT invent remaps for
heterogeneous values that need per-page classification — those that are not in
the pack's alias map are left for the classify-drain cron.

The engine ships ZERO domain taxonomy: both the canonical type set and the
alias map are read at runtime from the governing schema.yaml. If the pack
declared no `types:`, the case-only normalization is skipped (any type is
accepted); if it declared no `type_aliases:`, nothing is remapped.

Rewrites ONLY the `type:` line; everything else (frontmatter and body) stays
byte-identical. Gated: writes only if the result still parses as a mapping with
a `type:` and the body is unchanged. Idempotent. Pass --dry-run to report
without writing.

Env:
  WIKI_PATH   vault root (default /opt/vault)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
ENTITIES_DIR = VAULT / "wiki" / "entities"

_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*\n)(.*)\Z", re.S)
_TYPE_LINE_RE = re.compile(r"^type:[ \t]*(.+?)[ \t]*$", re.M)


def split_fm_body(text: str):
    m = _FM_RE.match(text)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def target_type(cur_type: str, canonical: set[str], aliases: dict[str, str]) -> str | None:
    """Return the canonical type to rewrite to, or None to leave alone.

    Driven entirely by the pack's schema:
      - `aliases` (schema `type_aliases:`) — explicit 1:1 remaps (case-insensitive).
      - `canonical` (schema `types:` keys) — for case-only normalization. Empty
        set => skip the case check (accept any type, no built-in taxonomy)."""
    low = cur_type.lower()
    if low in aliases:
        return aliases[low]
    # Case-only normalization, only when the pack declared a canonical type set.
    if canonical and low in canonical and cur_type != low:
        return low
    return None


def remap(text: str, canonical: set[str], aliases: dict[str, str]) -> tuple[str | None, str, str]:
    """Return (new_text|None, old_type, new_type). None if not applicable/unsafe."""
    parts = split_fm_body(text)
    if parts is None:
        return None, "", ""
    opening, fm_body, closing, body = parts
    m = _TYPE_LINE_RE.search(fm_body)
    if not m:
        return None, "", ""
    cur_type = m.group(1).strip().strip('"').strip("'")
    new_type = target_type(cur_type, canonical, aliases)
    if not new_type or new_type == cur_type:
        return None, cur_type, ""

    new_fm_body = fm_body[:m.start()] + f"type: {new_type}" + fm_body[m.end():]
    new_text = opening + new_fm_body + closing + body
    # Gate: still parses as a mapping with the new type, body byte-identical.
    try:
        d = yaml.safe_load(new_fm_body)
    except yaml.YAMLError:
        return None, cur_type, ""
    if not isinstance(d, dict) or d.get("type") != new_type:
        return None, cur_type, ""
    np = split_fm_body(new_text)
    if np is None or np[3] != body:
        return None, cur_type, ""
    return new_text, cur_type, new_type


def main() -> int:
    dry = "--dry-run" in sys.argv[1:]
    if not ENTITIES_DIR.is_dir():
        print(f"No entities dir at {ENTITIES_DIR}")
        print(json.dumps({"wakeAgent": False}))
        return 0

    schema = schema_lib.governing_schema(VAULT)
    canonical = schema_lib.canonical_types(schema)
    aliases = schema_lib.type_aliases(schema)
    if not aliases and not canonical:
        # No declared taxonomy or alias map: nothing the engine can safely remap.
        print("=== schema-type-drain ===")
        print("  no schema types/aliases declared — nothing to drain")
        print(json.dumps({"wakeAgent": False}))
        return 0

    drained = 0
    perm_skips = 0
    by_map: dict[str, int] = {}
    print(f"=== schema-type-drain{' (DRY RUN)' if dry else ''} ===")
    for p in sorted(ENTITIES_DIR.rglob("*.md")):
        name = p.name
        if name.startswith("_") or ".bak" in name or "_archive" in str(p):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        new_text, old_t, new_t = remap(text, canonical, aliases)
        if new_text is None:
            continue
        key = f"{old_t} -> {new_t}"
        by_map[key] = by_map.get(key, 0) + 1
        drained += 1
        if dry:
            continue
        try:
            p.write_text(new_text)
        except PermissionError:
            perm_skips += 1
            print(f"  ! {p.relative_to(VAULT)}: PERMISSION DENIED")
            continue

    for k in sorted(by_map):
        print(f"  {k}: {by_map[k]}")
    print(f"{'Would drain' if dry else 'Drained'} {drained} page(s).")
    if perm_skips:
        print(f"Permission-skipped: {perm_skips}.")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
