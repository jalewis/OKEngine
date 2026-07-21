#!/usr/bin/env python3
"""Incremental signal-role producer with bounded backfill mode (#221)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importer_guard import guard  # noqa: E402
from signal_classifier import ALL_CLASSES, classify  # noqa: E402

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.S)


def _config(vault: Path) -> dict:
    path = Path(os.environ.get("SIGNAL_CLASSIFIER_CONFIG",
                               str(vault / "config" / "signal-classifier.yaml")))
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except OSError:
        return {}


def _supported(vault: Path) -> bool:
    try:
        import schema_lib
        schema = schema_lib.merged_schema(vault, "sources")
        enum = schema.get("field_enums", {}).get("signal_class", {}).get("enum")
        values = set(schema.get("enums", {}).get(enum, []))
        return set(ALL_CLASSES) <= values
    except Exception:
        return False


def run(vault: Path, *, apply: bool, force: bool = False,
        limit: int | None = None) -> dict:
    counts = Counter(seen=0, classified=0, unchanged=0, rejected=0, no_frontmatter=0)
    if not _supported(vault):
        counts["unsupported_schema"] = 1
        return dict(counts)
    config = _config(vault)
    pages = sorted((vault / "wiki" / "sources").rglob("*.md"))
    if limit is not None:
        pages = pages[:limit]
    for path in pages:
        if path.name.startswith(("_", ".")):
            continue
        counts["seen"] += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            counts["vanished"] += 1
            continue
        match = _FM.match(text)
        if not match:
            counts["no_frontmatter"] += 1
            continue
        try:
            fm = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            fm = None
        if not isinstance(fm, dict):
            counts["no_frontmatter"] += 1
            continue
        if not force and fm.get("signal_class") in ALL_CLASSES:
            counts["unchanged"] += 1
            continue
        rel = path.relative_to(vault / "wiki").as_posix()
        value, reason = classify(rel, fm, match.group(2), config=config)
        fm["signal_class"] = value
        problems = guard(fm, vault=vault, namespace="sources")
        if problems:
            counts["rejected"] += 1
            for problem in problems:
                print(f"signal-class-reject: {rel}: {problem}", file=sys.stderr)
            continue
        if apply:
            head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True,
                                  default_flow_style=False).rstrip("\n")
            path.write_text(f"---\n{head}\n---\n{match.group(2)}", encoding="utf-8")
        counts[value] += 1
        counts[f"reason:{reason}"] += 1
        counts["classified"] += 1
    return dict(counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    counts = run(Path(args.vault), apply=not args.dry_run, force=args.force, limit=args.limit)
    print("signal-classifier: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
