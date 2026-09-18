#!/usr/bin/env python3
"""Quarantine redundant raw captures of the same article, keeping the one that is referenced.

A feed whose GUID rotates per fetch defeats guid-keyed dedupe, so the same article is re-captured on
every run into every category directory it matched. One observed URL reached 555 captures and a
single tree held 10,429 redundant files. Every count derived from "raw files" inherits that error --
an ingest backlog read ~8x larger than it was.

The cause is fixed in feed_fetch (dedupe now keys on the article link as well as the guid); this
clears what the old behaviour already produced.

IT MOVES, IT DOES NOT DELETE. Captures are primary evidence: a wrong identity judgement here
destroys the original record, and no downstream check would notice. Files go to a quarantine tree
that mirrors their layout, so a mistake is one `mv` from repaired and an operator can purge on their
own schedule.

WHAT IS KEPT, in order: every capture CITED by a source page (596 of them in the observed tree --
deleting those would sever provenance the repair lane just restored), then the earliest remaining
capture. Everything kept stays where it is; only surplus copies move.

Pure no_agent script. Idempotent. Dry-run by DEFAULT.

Env: WIKI_PATH (default /opt/vault)
Usage: dedupe_raw_captures.py [--apply] [--quarantine DIR]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?", re.S)


def _norm_url(value) -> str:
    u = str(value or "").strip().rstrip("/").casefold()
    u = re.sub(r"^https?://(www\.)?", "", u)
    return re.sub(r"[?&]p=\d+\b", "", u).rstrip("/?&")


def _fm(path: Path):
    try:
        m = _FM.match(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except Exception:
        return None
    return fm if isinstance(fm, dict) else None


def cited_captures(vault: Path) -> set[str]:
    """Every raw path referenced ANYWHERE in the wiki. None of these is ever surplus.

    Scanning only `raw:` on source pages was not enough: on one deployment FIVE distinct frontmatter
    fields pointed into the raw tree across 4,984 paths -- pack-defined importer fields the engine
    has never heard of among them -- and a field-specific scan would have quarantined 1,752 captures
    those other fields cite, manufacturing exactly the dangling references such a corpus spends days
    repairing. So the scan is by VALUE SHAPE, not by field name: any string starting `raw/` counts,
    including fields no schema has declared yet. The engine cannot enumerate pack vocabulary, and
    must not try.
    """
    out: set[str] = set()
    base = vault / "wiki"
    for path in base.rglob("*.md") if base.is_dir() else []:
        fm = _fm(path)
        if not fm:
            continue
        for value in fm.values():
            for item in (value if isinstance(value, list) else [value]):
                text = str(item).strip()
                if text.startswith("raw/"):
                    out.add(text.removeprefix("wiki/"))
    return out


def plan(vault: Path):
    """(surplus paths, stats). Groups by normalised URL; keeps cited copies, then the earliest."""
    cited = cited_captures(vault)
    groups: dict[str, list[Path]] = collections.defaultdict(list)
    stats = collections.Counter()
    base = vault / "raw"
    for path in sorted(base.rglob("*.md")) if base.is_dir() else []:
        fm = _fm(path)
        if fm is None:
            stats["unreadable — left alone"] += 1
            continue
        url = _norm_url(fm.get("url"))
        if not url:
            stats["no url — cannot group, left alone"] += 1
            continue
        groups[url].append(path)

    surplus: list[Path] = []
    for _url, paths in sorted(groups.items()):
        if len(paths) == 1:
            stats["unique"] += 1
            continue
        keep = [p for p in paths if p.relative_to(vault).as_posix() in cited]
        if keep:
            stats["group kept by citation"] += 1
        else:
            keep = [min(paths, key=lambda p: (p.stat().st_mtime, p.name))]
            stats["group kept by earliest capture"] += 1
        keepset = {p.resolve() for p in keep}
        extra = [p for p in paths if p.resolve() not in keepset]
        surplus.extend(extra)
        stats["surplus captures"] += len(extra)
    return surplus, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--quarantine", type=Path,
                    default=VAULT / ".okengine" / "quarantine" / "duplicate-captures")
    args = ap.parse_args(argv)

    surplus, stats = plan(VAULT)
    moved = 0
    for path in surplus:
        if not args.apply:
            continue
        dest = args.quarantine / path.relative_to(VAULT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():                      # a prior run already quarantined this name
            dest = dest.with_name(f"{dest.stem}-{moved}{dest.suffix}")
        shutil.move(str(path), str(dest))
        moved += 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"dedupe-raw-captures: {mode} — {len(surplus)} surplus capture(s), {moved} quarantined "
          f"-> {args.quarantine}")
    for key, value in stats.most_common():
        print(f"    {key}: {value}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
