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
the pack's alias map are left for the classify-drain cron. A reviewed one-time
migration may supply ``--map <yaml>``. It may be a flat old->canonical map, or
contain ``types:`` plus page-specific ``paths:`` maps. Those entries are explicit
migration data, not engine taxonomy.

The engine ships ZERO domain taxonomy: both the canonical type set and the
alias map are read at runtime from the governing schema.yaml. If the pack
declared no `types:`, the case-only normalization is skipped (any type is
accepted); if it declared no `type_aliases:`, nothing is remapped.

Scans every authored page under ``wiki/`` (not only entities), resolving the
governing schema per page. Rewrites ONLY the `type:` line; everything else stays
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
WIKI_DIR = VAULT / "wiki"

_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*\n)(.*)\Z", re.S)
_TYPE_BLOCK_RE = re.compile(r"^type:[ \t]*[^\n]*(?:\n[ \t]+[^\n]*)*", re.M)


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
    ci_aliases = {str(k).casefold(): str(v) for k, v in aliases.items()}
    if low.casefold() in ci_aliases:
        return ci_aliases[low.casefold()]
    # Case-only normalization, only when the pack declared a canonical type set.
    if canonical and low in canonical and cur_type != low:
        return low
    return None


def current_type(text: str) -> str:
    parts = split_fm_body(text)
    if parts is None:
        return ""
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return ""
    value = frontmatter.get("type") if isinstance(frontmatter, dict) else None
    return str(value) if value is not None else ""


def remap(text: str, canonical: set[str], aliases: dict[str, str]) -> tuple[str | None, str, str]:
    """Return (new_text|None, old_type, new_type). None if not applicable/unsafe."""
    parts = split_fm_body(text)
    if parts is None:
        return None, "", ""
    opening, fm_body, closing, body = parts
    m = _TYPE_BLOCK_RE.search(fm_body)
    if not m:
        return None, "", ""
    try:
        parsed = yaml.safe_load(fm_body) or {}
    except yaml.YAMLError:
        return None, "", ""
    if not isinstance(parsed, dict) or parsed.get("type") is None:
        return None, "", ""
    cur_type = str(parsed["type"])
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
    map_path = None
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--map" and i + 2 <= len(sys.argv[1:]):
            map_path = Path(sys.argv[1:][i + 1])
    map_path = map_path or (Path(os.environ["OKENGINE_TYPE_MAP"])
                            if os.environ.get("OKENGINE_TYPE_MAP") else None)
    explicit: dict[str, str] = {}
    path_explicit: dict[str, str] = {}
    if map_path:
        try:
            loaded = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            print(f"ERROR: cannot load type map {map_path}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(loaded, dict):
            print(f"ERROR: type map {map_path} must be a mapping", file=sys.stderr)
            return 2
        structured = "types" in loaded or "paths" in loaded
        if structured:
            if set(loaded) - {"types", "paths"}:
                print(f"ERROR: structured type map {map_path} only accepts types/paths", file=sys.stderr)
                return 2
            explicit = loaded.get("types") or {}
            path_explicit = loaded.get("paths") or {}
        else:
            explicit = loaded
        for label, mapping in (("types", explicit), ("paths", path_explicit)):
            if not isinstance(mapping, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
                print(f"ERROR: type map {map_path} {label} must be a string:string mapping",
                      file=sys.stderr)
                return 2
    if not WIKI_DIR.is_dir():
        print(f"No wiki dir at {WIKI_DIR}")
        print(json.dumps({"wakeAgent": False}))
        return 0

    drained = 0
    perm_skips = 0
    by_map: dict[str, int] = {}
    print(f"=== schema-type-drain{' (DRY RUN)' if dry else ''} ===")
    schemas: dict[Path, tuple[set[str], dict[str, str]]] = {}
    for p in sorted(WIKI_DIR.rglob("*.md")):
        name = p.name
        if (name.startswith(("_", ".")) or name.lower() in {"bundle.md", "hot.md", "health.md"}
                or name.upper().startswith("INDEX") or ".bak" in name or "_archive" in str(p)):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        ns = p.parent.relative_to(WIKI_DIR).as_posix()
        govdir = schema_lib._governing_dir(VAULT, ns)
        if govdir not in schemas:
            schema = schema_lib.merged_schema(VAULT, ns)
            schemas[govdir] = (schema_lib.canonical_types(schema), schema_lib.type_aliases(schema))
        canonical, aliases = schemas[govdir]
        rel = p.relative_to(WIKI_DIR).as_posix()
        old_type = current_type(text)
        page_map = {old_type: path_explicit[rel]} if old_type and rel in path_explicit else {}
        new_text, old_t, new_t = remap(text, canonical, {**aliases, **explicit, **page_map})
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
