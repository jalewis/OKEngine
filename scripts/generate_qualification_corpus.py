#!/usr/bin/env python3
"""Generate a deterministic typed and sharded qualification corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import date, timedelta
from pathlib import Path

TYPES = ("source", "entity", "concept", "observation", "prediction")
NAMESPACES = {"entity": "entities", "concept": "concepts", "observation": "observations",
              "prediction": "predictions"}


def _path(wiki: Path, kind: str, index: int) -> Path:
    slug = f"qualification-{kind}-{index:06d}"
    if kind == "source":
        day = date(2020, 1, 1) + timedelta(days=index % 2190)
        return wiki / "sources" / day.strftime("%Y/%m/%d") / f"{slug}.md"
    if kind == "entity":
        return wiki / "entities" / slug[0] / f"{slug}.md"
    return wiki / NAMESPACES[kind] / slug[-3] / slug[-2] / f"{slug}.md"


def _page(kind: str, index: int) -> str:
    slug = f"qualification-{kind}-{index:06d}"
    source_index = index - (index % len(TYPES))
    source_day = date(2020, 1, 1) + timedelta(days=source_index % 2190)
    source = (f"sources/{source_day:%Y/%m/%d}/"
              f"qualification-source-{source_index:06d}")
    common = ["---", f"type: {kind}", f'title: "Qualification {kind} {index:06d}"',
              f"id: q-{kind}-{index:06d}", "created: 2026-08-26"]
    if kind != "source":
        common += ["sources:", f'  - "[[{source}]]"']
    else:
        common += [f'url: "https://qualification.invalid/{index:06d}"',
                   "publisher: qualification-fixture", "published: 2026-08-26"]
    common += ["---", "", f"# {slug}", "",
               f"Deterministic qualification content {index:06d} for integrated scale testing.", ""]
    return "\n".join(common)


def generate(root: Path, count: int) -> dict:
    if count < len(TYPES):
        raise ValueError(f"count must be at least {len(TYPES)}")
    wiki = root / "wiki"
    if wiki.exists():
        shutil.rmtree(wiki)
    wiki.mkdir(parents=True, exist_ok=True)
    # The full maintenance audit includes ingest-shape checks.  A production-like
    # deployment has this root even when no raw inputs are pending.
    (root / "raw").mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    counts = {kind: 0 for kind in TYPES}
    for index in range(count):
        kind = TYPES[index % len(TYPES)]
        target = _path(wiki, kind, index)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = _page(kind, index)
        target.write_text(body, encoding="utf-8")
        relative = target.relative_to(root).as_posix()
        digest.update(relative.encode() + b"\0" + body.encode() + b"\0")
        counts[kind] += 1
    manifest = {"schema_version": 1, "generator": "okengine-qualification-corpus-v1",
                "pages": count, "types": counts, "sha256": digest.hexdigest()}
    output = root / ".okengine/qualification-corpus.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, output)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("--pages", type=int, default=100_000)
    args = parser.parse_args(argv)
    try:
        manifest = generate(Path(args.root), args.pages)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
