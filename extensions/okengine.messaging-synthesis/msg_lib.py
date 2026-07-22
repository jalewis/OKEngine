"""Shared helpers for okengine.messaging-synthesis (okengine#152).

The product anchor (what "we" are, what "we" can claim) is PACK/OPERATOR config
(PRODUCT_ANCHOR_PATH) — the extension ships ZERO product identity. Generic over any vault:
an anchor names an entity/concept page that is the source of truth for claimable capabilities,
plus which okengine.competitive-analytics watchlist segments to message against. Absent config
means every selector reports no anchor and stays silent — no fabricated vendor identity.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def anchor_path() -> Path:
    """PRODUCT_ANCHOR_PATH env (operator/pack config) wins; else the pack default under the
    vault. The extension SHIPS NO anchor — an absent file just means 'no product configured'."""
    p = os.environ.get(
        "PRODUCT_ANCHOR_PATH",
        os.environ.get("OKENGINE_MESSAGING_SYNTHESIS_PRODUCT_ANCHOR_PATH", ""),
    ).strip()
    return Path(p) if p else (VAULT / "config" / "product-anchor.yaml")


def read_anchor() -> dict:
    """{} when unconfigured (the universal "stay silent" signal every wake-gate checks).
    Configured shape (see README):
      product_name: str
      capability_pages: [entity/concept slugs — the claimable-capability source of truth]
      watchlist_segments: [keys into the competitive-analytics watchlist — who we message against]
      home_entity: str  # optional — our own entity page, if the vault tracks us as an entity too
    """
    p = anchor_path()
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(errors="replace")) or {}
    except Exception:
        return {}


def _page_file(slug: str) -> "Path | None":
    stem = slug.split("/")[-1]
    for base in ("entities", "concepts"):
        d = WIKI / base
        direct = d / f"{slug.split('/', 1)[-1] if slug.startswith(base + '/') else slug}.md"
        if direct.is_file():
            return direct
        shard = d / stem[0:1] / f"{stem}.md"
        if shard.is_file():
            return shard
        if d.is_dir():
            for p in d.rglob(f"{stem}.md"):
                return p
    return None


def page_summary(slug: str, max_activity: int = 5) -> dict:
    """Frontmatter + a few body bullets for a capability-anchor or competitor page.
    `found: False` when the config names a page not yet in the vault."""
    p = _page_file(slug)
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
    activity = [a.strip()[:160] for a in re.findall(r"^[-*]\s+(.+)$", txt, re.M)][:max_activity]
    return {
        "slug": slug, "found": True, "type": fm.get("type"), "path": str(p),
        "title": fm.get("title", slug), "updated": fm.get("updated") or fm.get("last_updated"),
        "activity": activity,
    }


def slug_mention_pattern(slug: str) -> "re.Pattern":
    """Compile a case-insensitive pattern matching a competitor SLUG's mention in PROSE.
    Slugs are hyphenated ('acme-rival'); prose refers to them with spaces/original casing
    ('Acme Rival') — a literal hyphen match against lowercased body text would miss nearly
    every real mention, so hyphens are treated as either a hyphen or whitespace."""
    stem = slug.split("/")[-1]
    parts = [re.escape(p) for p in stem.split("-") if p]
    return re.compile(r"\b" + r"[\s-]".join(parts) + r"\b", re.IGNORECASE)


def watchlist_segments(keys: "list[str] | None" = None) -> dict:
    """Read okengine.competitive-analytics's watchlist (if present) filtered to the segment
    keys this product anchor names as messaging targets. {} if the watchlist extension isn't
    configured either — messaging-synthesis degrades gracefully, it doesn't hard-require it."""
    wl_path = Path(os.environ.get("WATCHLIST_PATH", "") or (VAULT / "config" / "competitive-watchlist.yaml"))
    if not wl_path.is_file():
        return {}
    try:
        wl = yaml.safe_load(wl_path.read_text(errors="replace")) or {}
    except Exception:
        return {}
    segments = wl.get("segments") or {}
    if keys is None:
        return segments
    return {k: v for k, v in segments.items() if k in keys}
