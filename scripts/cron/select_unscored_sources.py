#!/usr/bin/env python3
"""Wake-gate + digest builder for the source-quality-backfill cron.

Identifies source pages that lack `reliability` and/or `credibility` in
their YAML frontmatter — sources ingested before the source-rating step
was in place. Emits a batch of N for the agent to score in-place.

Wake-gates if no unscored source pages remain — the cron then naturally
no-ops once the historical corpus has been swept. After the backlog
clears, the cron stays scheduled but does nothing on each tick.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from selection_manifest import write_selection_manifest  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
N = int(os.environ.get("QUALITY_BACKFILL_BATCH_SIZE", "4"))
CONTROLLED_TARGET = os.environ.get("QUALITY_BACKFILL_TARGET", "").strip().lstrip("/")
DEFAULT_MANIFEST = Path("/opt/data/cron-plus/selections/source-quality-backfill.json")


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.S)


def read_frontmatter(path: Path) -> dict | None:
    """Return parsed frontmatter dict, or None if missing/unparseable.

    Uses a line-aware regex so `---` substrings inside frontmatter
    comments (e.g. `# --- Source quality ---`) don't prematurely split
    the document — the prior `txt.split('---', 2)` impl would lose
    every field after such a comment.
    """
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(txt)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def is_unscored(fm: dict) -> bool:
    if fm.get("type") != "source":
        return False
    # Tombstones are immutable by contract and inherit their canonical page's
    # quality. Selecting one guarantees a refused write and a bad receipt.
    if str(fm.get("status") or "").lower() == "tombstoned":
        return False
    # Treat None / missing / empty-string as unscored. The new schema requires
    # both reliability and credibility to be set.
    if not fm.get("reliability"):
        return True
    if fm.get("credibility") in (None, ""):
        return True
    return False


def main() -> int:
    lane_id = os.environ.get("OKENGINE_LANE_ID", "").strip()
    contract_digest = os.environ.get("OKENGINE_CONTRACT_DIGEST", "").strip()
    if not lane_id or not contract_digest:
        print("ERROR: source-quality receipt identity unavailable", file=sys.stderr)
        return 1
    manifest_path = Path(os.environ.get(
        "OKENGINE_SELECTION_MANIFEST", str(DEFAULT_MANIFEST)))
    manifest_path.unlink(missing_ok=True)
    sources_dir = VAULT / "wiki" / "sources"
    if not sources_dir.is_dir():
        print(f"ERROR: sources dir not found at {sources_dir}", file=sys.stderr)
        return 1

    all_sources = sorted(p for p in sources_dir.rglob("*.md") if not p.name.startswith("_"))

    unscored: list[tuple[Path, dict]] = []
    type_source_count = 0
    for p in all_sources:
        fm = read_frontmatter(p)
        if fm is None or fm.get("type") != "source":
            continue
        type_source_count += 1
        if is_unscored(fm):
            unscored.append((p, fm))

    print("=== source-quality-backfill wake-gate ===")
    print(f"  vault: {VAULT}")
    print(f"  total source pages: {type_source_count}")
    print(f"  unscored: {len(unscored)}")
    print(f"  batch size: {N}")

    if not unscored:
        print("  → SKIP: every source page has reliability + credibility set")
        print(json.dumps({"wakeAgent": False}))
        return 0

    # Pick the most-recently-ingested unscored sources first — newest are
    # most likely to anchor predictions, so score them first.
    unscored.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
    if CONTROLLED_TARGET:
        unscored = [
            item for item in unscored
            if item[0].relative_to(VAULT).as_posix() == CONTROLLED_TARGET
        ]
        if not unscored:
            print(f"  → SKIP: controlled target is not pending: {CONTROLLED_TARGET}")
            print(json.dumps({"wakeAgent": False}))
            return 0
    chosen = unscored[:N]
    selected = [p.relative_to(VAULT).as_posix() for p, _ in chosen]
    manifest = write_selection_manifest(selected, DEFAULT_MANIFEST)

    print()
    print(f"=== batch ({len(chosen)} of {len(unscored)} unscored) ===")
    print("Process IN ORDER. For each item call ONLY `score_source(path, reliability, "
          "credibility)`. Do not use update_entity, patch_entity, or converge_entity. "
          "Update ONLY `reliability` and `credibility`. Do not send or change "
          "type, id, version, created, last_updated, publisher, published, source_kind/kind, "
          "title, URL, raw, TLP, confidence, or the body. Missing metadata remains an explicit "
          "quality problem for its repair lane; never write `undefined` or invent a value.\n")
    for i, (p, fm) in enumerate(chosen, 1):
        rel = p.relative_to(VAULT).as_posix()
        publisher = fm.get("publisher") or "(no publisher)"
        published = fm.get("published") or "?"
        kind = fm.get("source_kind") or "?"
        raw_path = fm.get("raw") or "(no raw)"
        print(f"{i}. `{rel}`")
        print(f"   publisher={publisher}  published={published}  kind={kind}")
        print(f"   raw={raw_path}")

    print()
    receipt = {
        "api": 1,
        "lane_id": manifest["lane_id"],
        "contract_digest": manifest["contract_digest"],
        "input_digest": manifest["input_digest"],
        "items": [{
            "key": key,
            "disposition": None,
            "writes": [{"path": key, "sha256": None}],
            "reason": None,
        } for key in selected],
    }
    print("Each accepted patch is logged automatically by the governed write server. Do not edit "
          "wiki/log.md or any other page. Account for every selected key exactly once. Keep the "
          "runner-owned keys and identity unchanged; the runner supplies authoritative write hashes.")
    print("```okengine-receipt")
    print(json.dumps(receipt, indent=2))
    print("```")
    print()
    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
