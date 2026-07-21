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
import threading
from pathlib import Path

import yaml

try:                                            # id_lib is a sibling baked write-path lib
    import id_lib
except ImportError:                             # standalone import (cron/tests): add our own dir
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import id_lib

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
        # Normalized name/alias -> rel paths, for write_server._dedup_on_create's name<->alias match
        # (okengine#324). Unlike by_id these index LIVE entity pages EVEN WITHOUT an id (the dedup must
        # catch their duplicates too). Populated by _add_identity for entities/ pages only.
        self.name_to_rels: dict[str, list[str]] = {}
        self.alias_to_rels: dict[str, list[str]] = {}

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
        # `aliases` is a list field, but a page authored with a scalar (string
        # `aliases: A, B` or a bare YAML int/bool) must not crash the whole build
        # (okengine#196 — the write path coerces a scalar STRING, but a non-string
        # scalar reaches storage untouched). Mirror normalize_bare_name_links.py:
        # coerce to a list before iterating, then str() each member.
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        elif not isinstance(aliases, list):
            aliases = []
        for a in aliases:
            self.aliases.setdefault(str(a), rel)

    def _add_identity(self, rel: str, fm: dict) -> None:
        """Record a LIVE entity page's normalized primary-name + aliases (okengine#324). Caller has
        already excluded tombstoned pages. Matches _dedup_on_create's key derivation exactly: primary
        name = name|title|stem; aliases coerced from a scalar the same way (okengine#196)."""
        stem = rel.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        name = id_lib.normalize_key(str(fm.get("name") or fm.get("title") or stem))
        if name:
            bucket = self.name_to_rels.setdefault(name, [])
            if rel not in bucket:
                bucket.append(rel)
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        elif not isinstance(aliases, list):
            aliases = []
        for a in aliases:
            na = id_lib.normalize_key(str(a))
            if na:
                bucket = self.alias_to_rels.setdefault(na, [])
                if rel not in bucket:
                    bucket.append(rel)

    def to_dict(self) -> dict:
        return {
            "norm_version": 2,                  # v2 adds name_to_rels/alias_to_rels (okengine#324)
            "by_id": self.by_id,
            "aliases": self.aliases,
            "tombstoned": sorted(self.tombstoned),
            "collisions": self._collisions,
            "name_to_rels": self.name_to_rels,
            "alias_to_rels": self.alias_to_rels,
        }


def from_dict(d: dict) -> IdIndex:
    """Reconstruct an IdIndex from a persisted `to_dict()` payload. A pre-v2 artifact has no
    name_to_rels/alias_to_rels — they default empty, and write_server._dedup_on_create FALLS BACK to
    a live scan until the refresh cron rewrites a v2 artifact (okengine#324), so dedup is never blind."""
    idx = IdIndex()
    idx.by_id = dict(d.get("by_id") or {})
    idx.aliases = dict(d.get("aliases") or {})
    idx.tombstoned = set(d.get("tombstoned") or [])
    idx._collisions = dict(d.get("collisions") or {})
    idx.name_to_rels = {k: list(v) for k, v in (d.get("name_to_rels") or {}).items()}
    idx.alias_to_rels = {k: list(v) for k, v in (d.get("alias_to_rels") or {}).items()}
    return idx


def load(path: Path = INDEX_PATH) -> IdIndex | None:
    """Load the persisted id-index artifact; None if absent or unreadable."""
    try:
        return from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


_REFRESH_LOCK = threading.RLock()
_REFRESHING: set[str] = set()


def _scan(vault: Path) -> IdIndex:
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
        try:
            rel = p.resolve().relative_to(base).as_posix()
        except (ValueError, OSError):
            continue
        # Name/alias identity for entities/ dedup (okengine#324) — indexed for LIVE entity pages even
        # WITHOUT an id (the dedup must catch their duplicates), so this runs BEFORE the id skip below.
        if rel.split("/", 1)[0] == "entities" and \
                str(fm.get("status") or "").strip().lower() != "tombstoned":
            idx._add_identity(rel, fm)
        pid = fm.get("id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        idx._add(pid.strip(), rel, fm)
    return idx


def _refresh_into(idx: IdIndex, vault: Path, key: str) -> None:
    """Background: full-scan the vault, then update `idx` IN PLACE (so a holder's reference sees the
    fresh data) and re-persist the artifact. In-session ids the live index gained since load are
    unioned in so a write racing the rebuild isn't dropped."""
    try:
        fresh = _scan(vault)
        with _REFRESH_LOCK:
            for pid, rel in list(idx.by_id.items()):
                fresh.by_id.setdefault(pid, rel)
            idx.by_id, idx.aliases = fresh.by_id, fresh.aliases
            idx.tombstoned, idx._collisions = fresh.tombstoned, fresh._collisions
            idx.name_to_rels, idx.alias_to_rels = fresh.name_to_rels, fresh.alias_to_rels
        try:
            write_index(fresh)
        except OSError:
            pass
    finally:
        with _REFRESH_LOCK:
            _REFRESHING.discard(key)


def build(vault: Path = VAULT, *, force: bool = False) -> IdIndex:
    """Return a ready id-index for `vault`.

    `force=True` (the cron) full-scans wiki/ — 64k pages, read+parse each, tens of seconds.
    `force=False` (the write path) LOADS the persisted artifact instantly and kicks a one-shot
    background refresh so the huge scan never blocks a write. Falls back to a live scan only when no
    artifact exists yet (first deploy, before the refresh cron has run)."""
    if force:
        return _scan(vault)
    idx = load()
    if idx is None:
        return _scan(vault)                     # no artifact yet — scan once (first deploy)
    key = str(vault)
    with _REFRESH_LOCK:
        kick = key not in _REFRESHING
        if kick:
            _REFRESHING.add(key)
    if kick:
        threading.Thread(target=_refresh_into, args=(idx, vault, key), daemon=True).start()
    return idx


def write_index(idx: IdIndex, path: Path = INDEX_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx.to_dict(), separators=(",", ":")) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str]) -> int:
    idx = build(force=True)          # the cron always full-scans, then persists the artifact
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
