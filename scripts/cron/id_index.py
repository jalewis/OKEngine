#!/usr/bin/env python3
"""id_index — the `id -> path` resolver over an OKF vault (composable-okpacks P1).

Shared-namespace dedup needs to resolve a page `id` to its file fast (RFC §5a):
a write for an existing **authority** id merges into that page; a new id creates.
This module builds that index by walking the vault (with `rglob`, so SHARDED
pages are included — `corpus_indexer`'s `glob` historically missed them), and
exposes:

  - `resolve(id)`     -> the page's wiki-relative path (consulting `aliases:`),
  - `is_tombstoned(id)`,
  - `collisions()`    -> ids claimed by >1 LIVE page (routed to review, never
                         auto-merged — slug ids collide by design, §5a).

The batch index here is the backing store; the *write-synchronous* atomic id
claim lives in the MCP write path (a later P1 increment). Pure read-side: safe to
run against a copy of any vault.

Env: WIKI_PATH (vault root, default /opt/vault), HERMES_DATA (state dir).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
INDEX_PATH = Path(os.environ.get("HERMES_DATA", "/opt/data")) / "state" / "id-index.json"

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_RESERVED = {"index.md", "log.md", "agents.md", "hot.md", "bundle.md", "health.md"}


def _skip(p: Path) -> bool:
    n = p.name.lower()
    return (n in _RESERVED or p.name.startswith(("_", "."))
            or ".bak." in p.name or n == "schema.yaml")


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except Exception:
        return {}
    return fm if isinstance(fm, dict) else {}


class IdIndex:
    """An in-memory `id -> path` map with aliases, tombstones, and collisions."""

    def __init__(self) -> None:
        self.by_id: dict[str, str] = {}        # id -> rel path (first live claimant)
        self.aliases: dict[str, str] = {}      # alias id -> rel path
        self.tombstoned: set[str] = set()
        self._collisions: dict[str, list[str]] = {}   # id -> [rel paths] (>1 live)

    def resolve(self, page_id: str) -> str | None:
        """The wiki-relative path for `page_id`, consulting `aliases:`. None if unknown."""
        return self.by_id.get(page_id) or self.aliases.get(page_id)

    def is_tombstoned(self, page_id: str) -> bool:
        return page_id in self.tombstoned

    def collisions(self) -> dict[str, list[str]]:
        """ids claimed by more than one LIVE page — surfaced for review, never
        auto-merged (slug ids collide by design; §5a)."""
        return dict(self._collisions)

    def _add(self, page_id: str, rel: str, fm: dict) -> None:
        if str(fm.get("status") or "").strip().lower() == "tombstoned":
            self.tombstoned.add(page_id)
            self.by_id.setdefault(page_id, rel)
            return
        if page_id in self.by_id and self.by_id[page_id] != rel:
            self._collisions.setdefault(page_id, [self.by_id[page_id]]).append(rel)
        else:
            self.by_id[page_id] = rel
        for a in fm.get("aliases") or []:
            self.aliases.setdefault(str(a), rel)

    def to_dict(self) -> dict:
        return {
            "norm_version": 1,
            "by_id": self.by_id,
            "aliases": self.aliases,
            "tombstoned": sorted(self.tombstoned),
            "collisions": self._collisions,
        }


def build(vault: Path = VAULT) -> IdIndex:
    """Walk wiki/ (recursively, sharded pages included) and index every page that
    carries an `id`. Pages without an `id` are skipped (backfill stamps them)."""
    idx = IdIndex()
    wiki = vault / "wiki"
    if not wiki.is_dir():
        return idx
    base = wiki.resolve()
    for p in sorted(wiki.rglob("*.md")):
        if _skip(p):
            continue
        try:
            fm = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        pid = fm.get("id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        try:
            rel = p.resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            continue
        idx._add(pid.strip(), rel, fm)
    return idx


def write_index(idx: IdIndex, path: Path = INDEX_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx.to_dict(), separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str]) -> int:
    idx = build()
    write_index(idx)
    cols = idx.collisions()
    print(f"id-index: {len(idx.by_id)} ids, {len(idx.aliases)} aliases, "
          f"{len(idx.tombstoned)} tombstoned, {len(cols)} collision(s) -> {INDEX_PATH}")
    for pid, paths in sorted(cols.items()):
        print(f"  COLLISION {pid}: {', '.join(paths)}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
