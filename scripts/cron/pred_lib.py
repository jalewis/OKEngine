"""Shared helpers for the prediction wake-gate selectors (okengine#36).

Generic over any OKF vault that defines a `prediction` type (status / subject /
resolves_by). No domain knowledge. Used by select_prediction_candidates.py,
select_predictions_for_grading.py, select_regrade_batch.py.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

OPEN = "open"   # the unresolved status; resolved = confirmed/refuted/partial/expired-ungraded
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
_RESERVED = re.compile(r"^(INDEX|index)([.-]|$)")


def vault() -> Path:
    return Path(os.environ.get("WIKI_PATH", "/opt/vault"))


def today_iso() -> str:
    return os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat()


def days_ago_iso(n: int) -> str:
    return (date.fromisoformat(today_iso()) - timedelta(days=n)).isoformat()


def read_fm(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = _FM.match(text)
    if not m:
        return {}
    try:
        import yaml
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def iter_pages(v: Path, ns: str):
    """Yield content page paths under wiki/<ns> (recursive; skips reserved/generated)."""
    d = v / "wiki" / ns
    if not d.is_dir():
        return
    for p in d.rglob("*.md"):
        if p.name.startswith(("_", ".")) or _RESERVED.match(p.name) or ".bak." in p.name:
            continue
        yield p


def predictions(v: Path) -> list[tuple[Path, dict]]:
    """(path, frontmatter) for every prediction page."""
    out = []
    for p in iter_pages(v, "predictions"):
        fm = read_fm(p)
        if str(fm.get("type", "")).strip() == "prediction":
            out.append((p, fm))
    return out


def is_open(fm: dict) -> bool:
    return str(fm.get("status", "")).strip().lower() == OPEN


def fm_date(fm: dict, *keys: str) -> str:
    """First present date field (YYYY-MM-DD), '' if none."""
    for k in keys:
        v = fm.get(k)
        if v:
            return str(v)[:10]
    return ""


def _slug(ref: str) -> str:
    """Final path segment of a ref, lowercased, .md stripped — the page's match key."""
    s = str(ref).strip().strip("/").split("/")[-1].lower()
    return s[:-3] if s.endswith(".md") else s


_DATE_IN_PATH = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")


def _date_from_path(p: Path) -> str:
    m = _DATE_IN_PATH.search(p.as_posix())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def recent_source_slugs(v: Path, cutoff: str) -> set[str]:
    """Slugs of `source` pages dated on/after `cutoff` (YYYY-MM-DD) — the genuine
    'something happened recently' signal, unlike `last_updated` which the token-free
    importers bump on every page. Date comes from published/created/date frontmatter,
    else the YYYY/MM/DD path segments (by-date source layout)."""
    out: set[str] = set()
    for p in iter_pages(v, "sources"):
        d = fm_date(read_fm(p), "published", "created", "date", "updated") or _date_from_path(p)
        if d and d >= cutoff:
            out.add(_slug(p.name))
    return out


def entity_source_slugs(fm: dict) -> set[str]:
    """Source slugs an entity cites, from its `sources:` list (all source refs) and any
    `sources/...` entries in `related:`. Importer-only stub pages cite no sources, so this
    is what distinguishes a feed-active entity from a freshly-imported catalog stub."""
    out: set[str] = set()
    srcs = fm.get("sources")
    for ref in (srcs if isinstance(srcs, list) else [srcs] if srcs else []):
        out.add(_slug(ref))
    rel = fm.get("related")
    for ref in (rel if isinstance(rel, list) else [rel] if rel else []):
        if str(ref).strip().startswith("sources/"):
            out.add(_slug(ref))
    out.discard("")
    return out


def subject_slugs(fm: dict) -> set[str]:
    """Entity slugs a prediction's `subject` points at — the last path segment of
    each `[[entity/...]]` wikilink (or bare value), lowercased. Used to tell which
    entities already have an open prediction."""
    subj = fm.get("subject")
    vals = subj if isinstance(subj, list) else [subj]
    out = set()
    for v in vals:
        if not v:
            continue
        m = re.search(r"\[\[([^\]|#]+)", str(v))
        s = (m.group(1) if m else str(v)).strip().strip("/")
        if s:
            out.add(s.split("/")[-1].lower())
    return out
