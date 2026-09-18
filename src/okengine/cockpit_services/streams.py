from __future__ import annotations
# ruff: noqa: F821

import functools
import os
import re
import json
import glob
import hashlib
import hmac
import sys
import threading
import time
import datetime
from collections import Counter, defaultdict
import subprocess
import shutil
import tempfile
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any

import yaml
import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

_FM_SCAN_BYTES = 262_144


def _streams() -> dict:
    return cockpit_config()["streams_by_key"]


def _is_reserved_seg(seg: str) -> bool:
    """A reserved DIRECTORY segment: `_archive`/`_archived`/`.git`-style hidden dirs. A BARE `_` is NOT
    reserved — it's the engine's reshard SECOND-LETTER bucket for a slug whose 2nd char is non-alnum
    (entities/x/_/x-force.md; okf_migrate._second), a legitimate canonical location that must stay
    visible in every enumeration surface (batch-2 re-verify over-drop)."""
    return len(seg) > 1 and seg.startswith(("_", "."))


def _visible_page(p: Path, base: Path) -> bool:
    """A page under `base` visible at EVERY path depth — no reserved dir segment anywhere and no INDEX
    leaf. rglob recurses into reserved sub-dirs a non-recursive glob never entered, so a leaf-only
    check would surface `_archived/`/`_archive/` retired content as live. invariant-audit batch-2."""
    try:
        parts = p.relative_to(base).parts
    except ValueError:
        return False
    return not any(_is_reserved_seg(seg) for seg in parts) and not p.name.startswith("INDEX")


def _ns_dirs(p: Path) -> frozenset:
    """The DIRECTORY components of a page's wiki-relative path (filename dropped) — layout-agnostic,
    so a walk-up sub-domain's nested namespace (wiki/<subdomain>/<ns>/…) is matched, not just parts[0]."""
    try:
        return frozenset(p.relative_to(WIKI).parts[:-1])
    except ValueError:
        return frozenset()


def _reserved_seg(p: Path) -> bool:
    """True if any DIRECTORY segment of a page's wiki-relative path is a reserved (`_archive/`-style)
    dir a leaf-only check misses. Safe on ANY enumeration surface (drops only engine-hidden dirs,
    never a real namespace or the bare-`_` reshard bucket)."""
    return any(_is_reserved_seg(seg) for seg in _ns_dirs(p))


def _hidden_page(p: Path) -> bool:
    """Hidden from the BROWSE discovery surfaces (browse rail count + /api/dir ledger): a reserved
    sub-dir OR a schema-excluded namespace nested under a walk-up sub-domain. NOT for dataset tabs /
    observation aggregation, which read an explicitly-configured dir and must not be second-guessed by
    the browse `exclude:` set — those use _reserved_seg only. (batch-2 re-verify)"""
    nsd = _ns_dirs(p)
    return any(_is_reserved_seg(seg) for seg in nsd) or bool(nsd & _excluded_dirs())


def _stream_pages(cfg: dict) -> list[str]:
    """Pages feeding a stream — by frontmatter `type` (okengine-layout-aware), by
    filename `glob`, or (default) every *.md in the stream dir."""
    base = WIKI / cfg["dir"]
    if not base.is_dir():
        return []
    if cfg.get("type"):
        out = []
        for p in base.rglob("*.md"):
            if not _visible_page(
                p, base
            ):  # segment-level (drops _archived/ at ANY depth), not leaf-only
                continue
            try:
                fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if str(fm.get("type") or "").strip() == cfg["type"]:
                out.append(str(p))
        return out
    # rglob, NOT glob.glob — the latter is non-recursive, so a PARTITIONED stream dir (dates sharded
    # into sub-dirs) yields zero pages/dates (invariant-audit M-528). _visible_page excludes reserved
    # sub-dirs (_archived/…) at ANY depth — a leaf-only check would surface retired archived pages.
    pattern = cfg.get("glob", "*.md")
    return [str(p) for p in base.rglob(pattern) if _visible_page(p, base)]


def _stream_dates(key: str) -> list[str]:
    cfg = _streams().get(key)
    if not cfg:
        return []
    out = []
    for p in _stream_pages(cfg):
        d = _file_date(Path(p).name)
        if d:
            out.append(d)
    return sorted(set(out), reverse=True)


def api_streams():
    streams = []
    for key, cfg in _streams().items():
        dates = _stream_dates(key)
        streams.append(
            {
                "key": key,
                "label": cfg["label"],
                "dates": dates,
                "latest": dates[0] if dates else None,
                "has_pdf": bool(cfg.get("pdf")),
            }
        )
    return {"streams": streams}


def _doc_path(stream: str, date: str) -> str:
    cfg = _streams().get(stream)
    if not cfg:
        raise HTTPException(404, "unknown stream")
    if not _DATE_RE.fullmatch(date):
        raise HTTPException(400, "bad date")
    base = WIKI / cfg["dir"]
    for p in _stream_pages(cfg):
        if _file_date(Path(p).name) == date:
            return os.path.relpath(p, base)
    raise HTTPException(404, "doc not found")


def api_doc(stream: str = Query(...), date: str = Query(...)):
    cfg = _streams().get(stream)
    if not cfg:
        raise HTTPException(404, "unknown stream")
    rel = _doc_path(stream, date)
    raw = safe_read(WIKI / cfg["dir"], rel)
    fm, body = split_fm(raw)
    # str-wrap: yaml.safe_load type-infers a bare `title: 2026`/`2026-07-08`/list
    # to a non-str, and .strip() would 500 (matches _page_meta / _subject).
    title = str(fm.get("title") or "").strip()
    if not title:
        h1 = _H1_RE.search(body)
        title = h1.group(0).lstrip("# ").strip() if h1 else f"{cfg['label']} — {date}"
    return {
        "stream": stream,
        "date": date,
        "title": title,
        "generated_at": fm.get("generated_at") or fm.get("updated") or fm.get("created"),
        "html": render_md(body),
    }


def _render_deck_pdf(md: Path) -> "Path | None":
    """Render a marp `.md` deck to PDF on demand. Returns the cached pdf path, or None if marp is
    unavailable or the render fails (caller then 404s). The weekly-deck cron writes only the `.md`
    (the pinned gateway has no browser); the cockpit renders the PDF the stream serves."""
    if not _MARP or not md.is_file():
        return None
    try:
        _DECK_CACHE.mkdir(parents=True, exist_ok=True)
        out = _DECK_CACHE / f"{md.stem}.{int(md.stat().st_mtime)}.pdf"
        if out.is_file() and out.stat().st_size > 0:
            return out
        # glob-ok: ephemeral deck-render cache, not a vault namespace
        for stale in _DECK_CACHE.glob(f"{md.stem}.*.pdf"):
            stale.unlink(missing_ok=True)
        # marp/puppeteer write intermediate files under HOME/TMPDIR/XDG_*; the container often runs as
        # a home-less vault uid, so point them all at the writable cache or the render EACCES-fails.
        env = {
            **os.environ,
            "HOME": str(_DECK_CACHE),
            "TMPDIR": str(_DECK_CACHE),
            "XDG_CACHE_HOME": str(_DECK_CACHE),
            "XDG_CONFIG_HOME": str(_DECK_CACHE),
        }
        subprocess.run(
            [_MARP, str(md), "--pdf", "--allow-local-files", "-o", str(out)],
            check=True,
            capture_output=True,
            timeout=120,
            cwd=str(_DECK_CACHE),
            env=env,
        )
        return out if (out.is_file() and out.stat().st_size > 0) else None
    except Exception:
        return None


def api_stream_pdf(stream: str = Query(...), date: str = Query(...)):
    """Serve a pdf-enabled stream's dated deck as PDF: a pre-rendered `<stem>.pdf` next to the `.md`
    if present, else render the marp `.md` on demand (cached). Generic: no fixed paths."""
    cfg = _streams().get(stream)
    if not cfg or not cfg.get("pdf"):
        raise HTTPException(404, "no pdf for stream")
    if not _DATE_RE.fullmatch(date):
        raise HTTPException(400, "bad date")
    base = (WIKI / cfg["dir"]).resolve()
    for p in _stream_pages(cfg):
        if _file_date(Path(p).name) == date:
            pdf = Path(p).with_suffix(".pdf").resolve()
            if str(pdf).startswith(str(base)) and pdf.is_file():
                return FileResponse(pdf, media_type="application/pdf")  # pre-rendered in the vault
            rendered = _render_deck_pdf(Path(p))  # else render the marp md
            if rendered:
                return FileResponse(rendered, media_type="application/pdf")
            break
    raise HTTPException(404, "deck pdf not found")


def _subject(fm: dict) -> str:
    s = fm.get("subject") or fm.get("entity") or ""
    if isinstance(s, list):
        s = s[0] if s else ""
    s = str(s)
    m = _WIKILINK.search(s)
    if m:
        s = m.group(3) or m.group(1) or ""
    return s.replace("entities/", "").replace("concepts/", "").strip()


def _trajectory(fm: dict) -> list[float]:
    ev = fm.get("evidence")
    pts: list[float] = []
    if isinstance(ev, list):
        # evidence is often stored newest-first; sort chronologically so the
        # trajectory reads left-to-right oldest→newest.
        items = [e for e in ev if isinstance(e, dict)]
        items.sort(key=lambda e: str(e.get("date") or ""))
        for i, e in enumerate(items):
            if i == 0 and isinstance(e.get("confidence_before"), (int, float)):
                pts.append(round(float(e["confidence_before"]), 3))
            if isinstance(e.get("confidence_after"), (int, float)):
                pts.append(round(float(e["confidence_after"]), 3))
    return pts


def _ev_bucket(raw: str) -> str | None:
    """Bucket a raw direction/tag: schema-sanctioned values pass through; legacy synonyms
    map (pre-backfill history only); anything else is None (surfaced as its raw `tag`,
    never silently absorbed into a bucket)."""
    if not raw:
        return None
    if raw in _ev_direction_enum():
        return raw
    return _EV_DIR_LEGACY.get(raw)


def _evidence_entries(fm: dict) -> list[dict]:
    """Normalize the `evidence` frontmatter into render-ready rows sorted oldest→newest:
    {date, direction (bucketed to reinforces/contradicts/partial/neutral), tag (raw),
    note, source, confidence_before, confidence_after}. Handles dict- AND string-shaped
    entries; drops anything with neither a date nor a note."""
    ev = fm.get("evidence")
    if not isinstance(ev, list):
        return []
    out: list[dict] = []
    for e in ev:
        src = None
        cb = ca = None
        if isinstance(e, dict):
            date = str(e.get("date") or e.get("on") or e.get("when") or "")[:10] or None
            raw = str(e.get("direction") or e.get("tag") or "").strip().lower()
            note = str(
                e.get("note") or e.get("text") or e.get("summary") or e.get("detail") or ""
            ).strip()
            src = e.get("source") or e.get("url") or e.get("ref") or e.get("link")
            b, a = e.get("confidence_before"), e.get("confidence_after")
            cb = round(float(b), 3) if isinstance(b, (int, float)) else None
            ca = round(float(a), 3) if isinstance(a, (int, float)) else None
            if ca is None and isinstance(e.get("confidence"), (int, float)):
                ca = round(float(e["confidence"]), 3)
        elif isinstance(e, str):
            m = _EV_PREFIX_RE.match(e)
            if m:
                date, raw, note = m.group(1), (m.group(2) or "").strip().lower(), m.group(3).strip()
            else:
                date, raw, note = None, "", e.strip()
        else:
            continue
        if not (date or note):
            continue
        out.append(
            {
                "date": date,
                "direction": _ev_bucket(raw),
                "tag": raw or None,
                "note": note,
                "source": str(src).strip() if src else None,
                "confidence_before": cb,
                "confidence_after": ca,
            }
        )
    out.sort(key=lambda r: r.get("date") or "")
    return out


def _prediction_confidence_scale() -> dict[str, float]:
    override = os.environ.get("PREDICTION_CONFIDENCE_SCALE")
    if override:
        try:
            value = json.loads(override)
            if isinstance(value, dict):
                return {str(key).strip().lower(): float(score) for key, score in value.items()}
        except (TypeError, ValueError):
            pass
    return PREDICTION_CONFIDENCE_SCALE


def _conf(fm: dict) -> float | None:
    c = fm.get("confidence")
    if isinstance(c, (int, float)):
        return round(float(c), 3)
    # qualitative alt-schema confidence
    if isinstance(c, str):
        return _prediction_confidence_scale().get(c.strip().lower())
    return None


def _claim(fm: dict, body: str) -> str:
    c = ""
    if fm.get("claim"):
        c = str(fm["claim"]).strip()
    elif fm.get("trigger"):
        c = str(fm["trigger"]).strip()
    else:
        b = _H1_RE.sub("", body, count=1)
        for para in re.split(r"\n\s*\n", b.strip()):
            line = re.sub(r"\s+", " ", para).strip(" #*->`")
            if len(line) > 20 and not line.lower().startswith(("## ", "status", "made on")):
                c = line[:300]
                break
    return re.sub(r"^\**\s*claim\s*:?\s*\**\s*", "", c, flags=re.I).strip()


def _prediction_files() -> list[str]:
    # RECURSIVE: the predictions extension writes into a resolution-quarter partition
    # (predictions/YYYY/qN/predict-*.md), so a flat `predictions/*.md` glob found ZERO and the
    # Open-predictions view went empty (operator report). `**` (recursive) matches both the flat
    # and the date-partitioned layouts.
    files: list[str] = []
    for sub in cockpit_config()["predictions_dirs"]:
        # glob-ok: recursive ** is already sharding-aware (matches forecasts at any shard depth)
        files += [
            f
            for f in glob.glob(  # glob-ok: explicitly recursive sharding-aware scan
                str(WIKI / sub / "**" / "*.md"), recursive=True)
            if not _reserved_seg(Path(f))
        ]  # skip predictions/_archive/… retired forecasts
    return files


def _load_predictions() -> list[dict]:
    today = TODAY()
    rows = []
    for p in _prediction_files():
        name = Path(p).name
        if name.startswith(("_", ".")) or ".bak." in name:
            continue
        try:
            raw = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, body = split_fm(raw)
        if str(fm.get("type") or "").strip().lower() != "prediction":
            continue
        rb = fm.get("resolves_by") or fm.get("target_date")
        rb_date = None
        if rb:
            m = _DATE_RE.search(str(rb))
            if m:
                try:
                    rb_date = datetime.date.fromisoformat(m.group(1))
                except ValueError:
                    rb_date = None
        status = str(fm.get("status") or "?").strip().lower()
        traj = _trajectory(fm)
        # evidence direction tally + idle detection (handles dict- AND string-shaped evidence;
        # the compact `[date tag]` regrade strings previously counted as zero → wrongly flagged idle)
        ev_entries = _evidence_entries(fm)
        ev_dir = {"reinforces": 0, "contradicts": 0, "partial": 0, "neutral": 0}
        for e in ev_entries:
            d = e.get("direction")
            if d in ev_dir:
                ev_dir[d] += 1
        made = _as_date(fm.get("made_on") or fm.get("created"))
        idle = (
            status in _OPEN_STATUS
            and not ev_entries
            and made is not None
            and (today - made).days > 60
        )
        # A stem is not an identity: quarter partitions can legitimately contain the same
        # filename. Keep the vault-relative logical key so ledger rows and detail requests remain
        # one-to-one across every configured prediction namespace.
        logical_id = Path(p).relative_to(WIKI).with_suffix("").as_posix()
        rows.append(
            {
                "id": logical_id,
                "status": status,
                "subject": _subject(fm),
                "claim": _strip_md(_claim(fm, body)),
                "claim_html": _inline_md(_claim(fm, body)),
                "confidence": _conf(fm),
                "horizon": str(fm.get("horizon") or fm.get("signal_class") or "").strip(),
                "made_on": str(fm.get("made_on") or fm.get("created") or "")[:10] or None,
                "updated": str(fm.get("updated") or "")[:10] or None,
                "resolves_by": rb_date.isoformat() if rb_date else None,
                "days_to_resolve": (rb_date - today).days if rb_date else None,
                "measurement_method": str(fm.get("measurement_method") or "").strip() or None,
                "forecast_set": str(fm.get("forecast_set") or "").strip() or None,
                "trajectory": traj,
                "last_move": round(traj[-1] - traj[-2], 3) if len(traj) >= 2 else None,
                "evidence_n": len(ev_entries),
                "ev_dir": ev_dir,
                "idle": idle,
            }
        )
    return rows


def api_predictions():
    rows = _load_predictions()
    summary: dict[str, int] = {}
    due_soon = 0
    idle = 0
    fsets: set[str] = set()
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
        d = r["days_to_resolve"]
        if r["status"] in _OPEN_STATUS and d is not None and 0 <= d <= 7:
            due_soon += 1
        if r["idle"]:
            idle += 1
        if r["forecast_set"]:
            fsets.add(r["forecast_set"])
    return {
        "total": len(rows),
        "summary": summary,
        "due_soon": due_soon,
        "idle": idle,
        "forecast_sets": sorted(fsets),
        "rows": rows,
    }


def api_prediction(id: str = Query(...)):
    if not id or id.startswith("/") or ".." in Path(id).parts or Path(id).suffix:
        raise HTTPException(400, "bad id")
    allowed = {
        Path(p).relative_to(WIKI).with_suffix("").as_posix(): Path(p) for p in _prediction_files()
    }
    p = allowed.get(id)
    if p and p.is_file():
        fm, body = split_fm(p.read_text(encoding="utf-8", errors="replace"))
        return {
            "id": id,
            "fm": {k: str(v) for k, v in fm.items() if k != "evidence"},
            "trajectory": _trajectory(fm),
            "evidence": _evidence_entries(fm),
            "claim": _strip_md(_claim(fm, body)),
            "claim_html": _inline_md(_claim(fm, body)),
            "html": render_md(body),
        }
    raise HTTPException(404, "prediction not found")


def _esc(s: Any) -> str:
    return (
        ("" if s is None else str(s))
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _as_date(v: Any) -> "datetime.date | None":
    if not v:
        return None
    m = _DATE_RE.search(str(v))
    if not m:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None
