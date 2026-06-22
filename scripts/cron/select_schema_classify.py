#!/usr/bin/env python3
"""Wake-gate + digest for the schema-classify-drain cron.

The deterministic schema-type-drain handles the pack's 1:1-aliasable types. The
HARD residual is the heterogeneous slice that needs per-page judgment:
  - a catch-all type (e.g. `type: organization`) that the pack uses for several
    distinct canonical roles, where only the page content can disambiguate.
  - type: entity (bare, no entity_type/category/... indicator) — what
    normalize_entity_schema cannot classify deterministically.

This wake-gate selects those pages, optionally computes a deterministic
TAG-BASED HINT (advisory — the agent reads the page and decides), and digests a
bounded batch so the classifier cron can set `type:` to the best canonical type,
or leave it when there is no honest fit. Local-only; the page content is the
sole evidence.

The engine ships ZERO domain taxonomy: the canonical type set the classifier may
assign is read at runtime from the governing schema.yaml `types:`. The optional
tag→type hint table is also pack-supplied (schema `classify_hints:`); when the
pack declares none, the digest carries no hint and the agent classifies purely
from content.

Env:
  WIKI_PATH                 vault root (default /opt/vault)
  SCHEMA_CLASSIFY_BATCH     max pages per run (default 30)
  SCHEMA_CLASSIFY_MIN_AGE   skip pages created within N days (default 7)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
ENTITIES_DIR = VAULT / "wiki" / "entities"
BATCH = int(os.environ.get("SCHEMA_CLASSIFY_BATCH", "30"))
MIN_AGE_DAYS = int(os.environ.get("SCHEMA_CLASSIFY_MIN_AGE", "7"))

INDICATOR_FIELDS = ("entity_type", "category", "entity_kind", "kind", "subtype")


def load_canonical() -> list[str]:
    """Canonical types the classifier may assign — the pack's schema.yaml
    `types:` keys. Empty list if the pack declared none (the digest then lists
    no fixed target set and the agent infers an honest type from content)."""
    return sorted(schema_lib.canonical_types(schema_lib.governing_schema(VAULT)))


def load_hints() -> list[tuple[set[str], str]]:
    """Optional pack-supplied tag→type hint table (schema `classify_hints:`),
    shaped as {<canonical-type>: [<tag>, ...]}. Advisory only; the agent
    overrides from content. Default [] (no hints) — the engine ships no
    domain tag vocabulary."""
    schema = schema_lib.governing_schema(VAULT)
    raw = schema.get("classify_hints")
    if not isinstance(raw, dict):
        return []
    out: list[tuple[set[str], str]] = []
    for canon, tags in raw.items():
        if isinstance(tags, (list, tuple, set)):
            out.append(({str(t).lower() for t in tags}, str(canon)))
    return out


def load_catchall_types() -> set[str]:
    """Catch-all types the pack uses for several distinct canonical roles and
    wants the classifier to disambiguate (schema `classify_catchall:` list).
    These are selected alongside bare `entity` pages. Default empty — only the
    generic bare-entity case is selected when the pack declares none."""
    schema = schema_lib.governing_schema(VAULT)
    raw = schema.get("classify_catchall")
    return {str(x) for x in raw} if isinstance(raw, (list, tuple, set)) else set()


CANONICAL = load_canonical()
_HINTS = load_hints()
_CATCHALL = load_catchall_types()

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.S)
_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")


def read_fm_body(path: Path) -> tuple[dict, str]:
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return {}, ""
    m = _FM_RE.match(txt)
    if not m:
        return {}, txt
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        fm = None
    return (fm if isinstance(fm, dict) else {}), txt[m.end():]


def _parse_date(v):
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        m = re.search(r"\d{4}-\d{2}-\d{2}", v)
        if m:
            try:
                return datetime.strptime(m.group(0), "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def _tags(fm: dict) -> set[str]:
    t = fm.get("tags")
    if isinstance(t, list):
        return {str(x).strip().strip('"').strip("'").lower() for x in t}
    if isinstance(t, str):
        return {s.strip().lower() for s in re.split(r"[,\[\]]", t) if s.strip()}
    return set()


def hint_for(tags: set[str]) -> str:
    for tagset, h in _HINTS:
        if tags & tagset:
            return h
    return ""


def _excerpt(body: str, n: int = 300) -> str:
    text = _WIKILINK_RE.sub(lambda m: m.group(0)[2:-2].split("|")[-1], body)
    text = re.sub(r"[#*`>_]", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:n]


def select(today: date) -> list[dict]:
    if not ENTITIES_DIR.is_dir():
        return []
    out = []
    for p in sorted(ENTITIES_DIR.rglob("*.md")):
        if p.name.startswith("_") or "_archive" in p.parts:
            continue
        fm, body = read_fm_body(p)
        ptype = str(fm.get("type") or "")
        is_catchall = ptype in _CATCHALL
        is_bare_entity = ptype == "entity" and not any(
            f in fm for f in INDICATOR_FIELDS)
        if not (is_catchall or is_bare_entity):
            continue
        created = _parse_date(fm.get("created"))
        if created and (today - created).days < MIN_AGE_DAYS:
            continue
        tags = _tags(fm)
        out.append({
            "slug": p.stem,
            "rel": p.relative_to(VAULT).as_posix(),
            "current_type": ptype,
            "tags": sorted(tags),
            "hint": hint_for(tags),
            "excerpt": _excerpt(body),
        })
    return out


def main() -> int:
    today = datetime.now(timezone.utc).date()
    candidates = select(today)

    catchall_label = (", ".join(sorted(_CATCHALL)) + " + ") if _CATCHALL else ""
    print("=== schema-classify wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  candidates ({catchall_label}bare entity, age>={MIN_AGE_DAYS}d): {len(candidates)}")

    if not candidates:
        print("  → SKIP: nothing to classify")
        print(json.dumps({"wakeAgent": False}))
        return 0

    batch = candidates[:BATCH]
    print(f"  this batch: {len(batch)} (of {len(candidates)})")
    print()
    if CANONICAL:
        print(f"Canonical types you may assign: {', '.join(CANONICAL)}")
    else:
        print("No canonical types declared in schema.yaml — assign the most "
              "honest type from the page content.")
    print("Leave `type` UNCHANGED if none is an honest fit.")
    print()
    print("=== batch ===")
    for c in batch:
        print(f"- {c['rel']}")
        print(f"    current_type={c['current_type']}  hint={c['hint'] or '(none)'}")
        print(f"    tags: {', '.join(c['tags']) or '(none)'}")
        print(f"    excerpt: {c['excerpt']}")
    print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
