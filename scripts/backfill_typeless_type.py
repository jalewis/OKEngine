#!/usr/bin/env python3
"""One-time backfill: add a `type:` field to typeless wiki pages (#3 / OKF).

The schema-drain crons (schema_type_drain, schema_classify, normalize_entity_schema)
all REMAP an existing `type:` value — none of them handle a page whose frontmatter
has NO `type:` key at all. Those pages predate the OKF conformance contract and
are the residual the write-guard now blocks at creation time but never cleaned up
retroactively. This is the durable one-time sweep for the unambiguous slice:

  - wiki/sources/*.md  -> type: source   (dir + source-shaped frontmatter is
                                           dispositive; zero inference)
  - wiki/entities/*.md -> type: <native> ONLY when a tag deterministically maps
                          to exactly one canonical entity type; else LEFT for the
                          classify-drain cron (per-page judgment, not a blind set).

It inserts the `type:` line immediately after the opening `---` and leaves every
other byte — frontmatter and body — identical (no yaml round-trip, which would
reorder/reformat). Idempotent: a page that already has `type:` is skipped.

Adding `type: source` may leave a page still missing source_kind/published — that
is expected (it moves from "typeless" to the separate field-tail bucket); this
script only fixes the missing-`type:` violation, honestly.

Usage:
  python backfill_typeless_type.py [--apply] [--root /opt/vault]
Default is dry-run (reports what it WOULD change). --apply writes.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", re.S)

def _tag_to_type(root: Path) -> dict[str, str]:
    """Build a {tag -> canonical type} map from the vault schema's `classify_hints`
    ({type: [tags that imply it]}). The engine ships NO taxonomy of its own — the
    map is empty unless the pack declares hints, in which case only tags that map
    to exactly one type are kept (ambiguous tags are left for the classify-drain).
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
        import schema_lib  # noqa: E402
        hints = schema_lib.merged_schema(root).get("classify_hints")
    except Exception:
        return {}
    if not isinstance(hints, dict):
        return {}
    tag_types: dict[str, set] = {}
    for typ, tags in hints.items():
        for t in (tags or []):
            tag_types.setdefault(str(t).strip().lower(), set()).add(str(typ))
    return {tag: next(iter(ts)) for tag, ts in tag_types.items() if len(ts) == 1}


def _classify_entity(fm: dict, tag_to_type: dict[str, str]) -> str | None:
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    hits = {tag_to_type[str(t).strip().lower()] for t in tags
            if str(t).strip().lower() in tag_to_type}
    return next(iter(hits)) if len(hits) == 1 else None


def _decide_type(rel_posix: str, fm: dict, tag_to_type: dict[str, str]) -> str | None:
    if rel_posix.startswith("wiki/sources/"):
        return "source"
    if rel_posix.startswith("wiki/entities/"):
        return _classify_entity(fm, tag_to_type)
    return None


def process(root: Path, apply: bool) -> int:
    set_count = 0
    skipped_entity = 0
    tag_to_type = _tag_to_type(root)
    for sub in ("sources", "entities"):
        base = root / "wiki" / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = _FM_RE.match(content)
            if not m:
                continue  # no-FM pages handled by a different sweep
            try:
                fm = yaml.safe_load(m.group(2))
            except Exception:
                continue
            if not isinstance(fm, dict):
                continue
            if "type" in fm and str(fm.get("type") or "").strip():
                continue  # already typed — idempotent
            rel = p.relative_to(root).as_posix()
            t = _decide_type(rel, fm, tag_to_type)
            if t is None:
                if rel.startswith("wiki/entities/"):
                    skipped_entity += 1
                continue
            new = m.group(1) + f"type: {t}\n" + m.group(2) + m.group(3) + content[m.end():]
            if apply:
                try:
                    p.write_text(new, encoding="utf-8")
                except OSError as e:
                    print(f"  ! cannot write {rel}: {e}", file=sys.stderr)
                    continue
            print(f"  {'set' if apply else 'would set'} type: {t:13s} {rel}")
            set_count += 1
    print(f"\n{'applied' if apply else 'dry-run'}: {set_count} file(s) "
          f"get a type: line; {skipped_entity} entit(y/ies) left for classify-drain")
    return set_count


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--root", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    args = ap.parse_args(argv)
    process(Path(args.root), args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
