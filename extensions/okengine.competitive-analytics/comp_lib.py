"""Shared helpers for okengine.competitive-analytics (okengine#146).

The competitor watchlist + segment/axis definitions are PACK/OPERATOR config (WATCHLIST_PATH) —
the extension ships ZERO competitor seeds. Generic over any vault: a "segment" is a set of entity
slugs to compare on two axes; the quadrant/battle-card/acquirer-signal MATH is the public adoption
layer, the watchlist is the private edge. Acceptance: runs unchanged on any pack that supplies a
watchlist (okpack-fintech, …).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def watchlist_path() -> Path:
    """WATCHLIST_PATH env (operator/pack config) wins; else the pack default under the vault.
    The extension SHIPS NO watchlist — an absent file just means 'no segments to analyse'."""
    p = os.environ.get("WATCHLIST_PATH", "").strip()
    return Path(p) if p else (VAULT / "config" / "competitive-watchlist.yaml")


def read_watchlist() -> dict:
    p = watchlist_path()
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(errors="replace")) or {}
    except Exception:
        return {}


def _entity_file(slug: str) -> Path | None:
    stem = slug.split("/")[-1]
    direct = WIKI / "entities" / f"{slug}.md"
    if direct.is_file():
        return direct
    shard = WIKI / "entities" / stem[0:1] / f"{stem}.md"
    if shard.is_file():
        return shard
    edir = WIKI / "entities"
    if edir.is_dir():
        for p in edir.rglob(f"{stem}.md"):
            return p
    return None


def entity_summary(slug: str, max_activity: int = 3) -> dict:
    """Frontmatter + the first few activity bullets for a competitor entity — the data the agent
    positions on the quadrant. `found: False` when the watchlist names an entity not yet in the vault."""
    p = _entity_file(slug)
    if p is None:
        return {"slug": slug, "found": False}
    txt = p.read_text(errors="replace")
    fm: dict = {}
    m = _FM.search(txt)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
    activity = [a.strip()[:120] for a in re.findall(r"^[-*]\s+(.+)$", txt, re.M)][:max_activity]
    rel = p.relative_to(WIKI / "entities").with_suffix("").as_posix()
    return {
        "slug": rel, "found": True, "type": fm.get("type"),
        "title": fm.get("title", slug), "updated": fm.get("updated") or fm.get("last_updated"),
        "activity": activity,
    }
