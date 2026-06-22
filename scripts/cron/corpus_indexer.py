#!/usr/bin/env python3
"""Emit JSONL views of the wiki corpus, one file per knowledge namespace.

Once this index layer exists, downstream analytics (source-quality scoring,
base rates, output-vs-outcome evaluation, etc.) become cheap queries against
the JSONL views instead of full corpus walks.

Which namespaces to index is a PACK input: the schema.yaml
`partitioning.namespaces` keys. The engine ships no domain taxonomy. When the
pack declares no namespaces, the indexer falls back to the on-disk top-level
wiki directories (minus the schema's `exclude:` dirs and dot/underscore dirs).

Output (one file per namespace, atomic-replaced each run):

  <HERMES_DATA>/state/corpus-index/
    <namespace>.jsonl   — one row per wiki/<namespace>/*.md
    index-summary.json  — counts + run metadata

Each JSONL row contains:
  - rel_path:      path relative to wiki/, .md included
  - stem:          filename without .md
  - frontmatter:   parsed FM dict (or null on parse failure)
  - body_first_500: first 500 chars of body (queryable preview, not full body)

Pure script. Wake-gate emit `wakeAgent=false`. Runs as a daily cron.
The JSONL views are consumed by downstream query tooling (e.g. a pack's
analytics jobs) instead of full corpus walks.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
INDEX_DIR = Path(os.environ.get("HERMES_DATA", "/opt/data")) / "state" / "corpus-index"


def indexed_namespaces() -> dict[str, tuple[str, str]]:
    """Knowledge namespaces to index → (wiki-relative subdir, JSONL filename).

    Schema-driven (schema.yaml `partitioning.namespaces`), minus the schema's
    `exclude:` dirs. If the pack declares no namespaces, fall back to the
    on-disk top-level wiki directories (minus excluded + dot/underscore dirs).
    The engine ships no hardcoded namespace list."""
    schema = schema_lib.governing_schema(VAULT)
    excluded = schema_lib.excluded_dirs(schema) | {"operational", "dashboards"}
    names = schema_lib.knowledge_namespaces(schema) - excluded
    if not names:
        wiki = VAULT / "wiki"
        if wiki.is_dir():
            names = {
                d.name for d in wiki.iterdir()
                if d.is_dir()
                and not d.name.startswith((".", "_"))
                and d.name not in excluded
            }
    return {n: (f"wiki/{n}", f"{n}.jsonl") for n in sorted(names)}

BODY_PREVIEW_CHARS = int(os.environ.get("CORPUS_INDEX_BODY_CHARS", "500"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)


def parse_fm(text: str) -> tuple[dict | None, str]:
    m = _FM_RE.match(text)
    if not m:
        return None, text
    body = text[m.end():]
    try:
        fm = yaml.safe_load(m.group(1))
        return (fm if isinstance(fm, dict) else None), body
    except yaml.YAMLError:
        return None, body


def _yaml_safe(obj):
    """Recursively convert non-JSON-serializable types (datetime.date,
    set, etc.) into JSON-friendly equivalents. yaml.safe_load
    occasionally returns datetime.date for unquoted YYYY-MM-DD values;
    those need str() before json.dumps."""
    from datetime import date, datetime
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, set):
        items = [_yaml_safe(x) for x in obj]
        # Only sort if all items are sortable scalars (yaml-loaded sets
        # of dicts would not be — keep insertion order in that case)
        try:
            return sorted(items)
        except TypeError:
            return items
    if isinstance(obj, dict):
        return {k: _yaml_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_yaml_safe(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def index_subdir(subdir: str, kind_label: str) -> list[dict]:
    """Walk wiki/<subdir>/**/*.md, return one row per file. Recursive (`rglob`)
    so SHARDED pages (e.g. entities/vendor/a/acme.md) are indexed — a plain
    `glob` silently omitted every sharded page."""
    base = VAULT / subdir
    if not base.is_dir():
        return []
    rows: list[dict] = []
    for p in sorted(base.rglob("*.md")):
        if p.name.startswith("_") or p.name == "INDEX.md":
            continue
        if any(".bak." in part for part in p.parts):
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        fm, body = parse_fm(txt)
        rel = str(p.relative_to(VAULT / "wiki"))
        row = {
            "rel_path": rel,
            "stem": p.stem,
            "kind": kind_label,
            "frontmatter": _yaml_safe(fm) if fm else None,
            "body_first_500": (body or "")[:BODY_PREVIEW_CHARS],
            "byte_size": len(txt),
        }
        rows.append(row)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    tmp.replace(path)


def main() -> int:
    started = datetime.now(timezone.utc)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    print("=== corpus-indexer ===")
    print(f"  vault: {VAULT}")
    print(f"  index dir: {INDEX_DIR}")
    print()

    summary: dict = {
        "indexed_at": started.isoformat(),
        "vault": str(VAULT),
        "by_type": {},
    }

    for kind, (subdir, jsonl_name) in indexed_namespaces().items():
        rows = index_subdir(subdir, kind)
        out_path = INDEX_DIR / jsonl_name
        write_jsonl_atomic(out_path, rows)
        summary["by_type"][kind] = {
            "jsonl": jsonl_name,
            "n_rows": len(rows),
            "with_frontmatter": sum(1 for r in rows if r["frontmatter"]),
        }
        print(f"  {kind:12s} → {out_path.relative_to(INDEX_DIR.parent.parent)}  "
              f"{len(rows):>6} rows")

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    summary["elapsed_seconds"] = round(elapsed, 2)

    summary_path = INDEX_DIR / "index-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print()
    print(f"  summary: {summary_path.relative_to(INDEX_DIR.parent.parent)}")
    print(f"  elapsed: {elapsed:.2f}s")
    print()
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
