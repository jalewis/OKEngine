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


def _clean_markdown(raw: str, title: str | None = None) -> str:
    """Portable, readable markdown: frontmatter dropped, embeds inlined, dataview
    removed, wikilinks flattened to text, title as H1."""
    fm, body = split_fm(raw)
    body = _resolve_embeds(body)
    body = re.sub(r"```dataview(js)?\n.*?\n```", "", body, flags=re.DOTALL)
    body = _uncode_wikilinks(body)
    body = _delink(body)
    body = _deref_local_links(body)
    t = str(title or fm.get("title") or fm.get("name") or "").strip()
    if t and not body.lstrip().startswith("# "):
        body = f"# {t}\n\n{body}"
    return body.strip() + "\n"


def _resolve_source(stream: str | None, date: str | None, path: str | None):
    """Return (raw_text, base_filename, default_title) for a briefing or a page."""
    if stream and date:
        cfg = _streams().get(stream)
        if not cfg:
            raise HTTPException(404, "unknown stream")
        raw = safe_read(WIKI / cfg["dir"], _doc_path(stream, date))
        return raw, f"{stream}-{date}", f"{cfg['label']} — {date}"
    if path:
        if ".." in path or path.startswith("/"):
            raise HTTPException(400, "bad path")
        cand = WIKI / (path + ".md")
        if not cand.is_file():
            name = Path(path).name + ".md"
            hits = [h for d in _content_dirs() for h in d.rglob(name)]
            if len(hits) > 1:
                raise HTTPException(409, "ambiguous page basename; use the full wiki-relative path")
            cand = hits[0] if hits else None
        if not cand:
            raise HTTPException(404, "page not found")
        cp = cand.resolve()
        try:
            cp.relative_to(WIKI.resolve())
        except ValueError:
            raise HTTPException(403, "blocked")
        return cp.read_text(encoding="utf-8", errors="replace"), Path(path).name, None
    raise HTTPException(400, "need stream+date or path")


def _pandoc(clean_md: str, fmt: str, title: str | None = None) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.md"
        out = Path(td) / f"out.{fmt}"
        src.write_text(clean_md, encoding="utf-8")
        cmd = ["pandoc", str(src), "-f", "markdown+pipe_tables", "-o", str(out)]
        # a non-empty title keeps standalone docx/pdf out of pandoc's "Defaulting to 'in'" fallback
        cmd += ["--metadata", f"title={(title or '').strip() or 'OKEngine report'}"]
        if fmt == "docx":
            cmd += ["--standalone"]
        if fmt == "pdf":
            cmd += ["--pdf-engine=weasyprint"]
            hdr = Path(td) / "style.html"
            hdr.write_text(f"<style>{_PDF_CSS}</style>", encoding="utf-8")
            cmd += ["--standalone", "-H", str(hdr)]
        try:
            # cwd must be writable: pandoc/weasyprint create temp files in CWD,
            # and /app is root-owned (we run as the vault uid).
            subprocess.run(cmd, check=True, capture_output=True, timeout=90, cwd=td)
        except FileNotFoundError:
            raise HTTPException(503, "pandoc not installed")
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"convert failed: {e.stderr.decode('utf-8', 'replace')[:300]}")
        return out.read_bytes()


def api_download(
    fmt: str, stream: str | None = None, date: str | None = None, path: str | None = None
):
    if fmt not in _DL_MIME:
        raise HTTPException(400, "fmt must be md|docx|pdf")
    raw, base, title = _resolve_source(stream, date, path)
    clean = _clean_markdown(raw, title)
    data = clean.encode("utf-8") if fmt == "md" else _pandoc(clean, fmt, title)
    fname = f"{base}.{fmt}"
    return Response(
        content=data,
        media_type=_DL_MIME[fmt],
        headers={"Content-Disposition": f'attachment; filename="{quote(fname)}"'},
    )


def _strip_report_preamble(md: str) -> str:
    """Drop the agent's leading progress-narration from an EXPORTED report. The narration
    ('Checking the vault…', 'Found the page…', 'Based on the vault, here's what we know…') is useful
    LIVE feedback in the chat but noise in a saved document. Strip the leading RUN of narration lines
    and stop at the first line that is NOT narration — so a report whose first line is real content is
    returned untouched (never removes real content). A `---`/`***`/`___` break left between the
    narration and the body is dropped too."""
    lines = md.split("\n")
    n = len(lines)
    i = 0
    while i < n and not lines[i].strip():
        i += 1
    start = i
    while i < n:
        s = lines[i].strip()
        if not s:  # blanks inside the run are skipped
            i += 1
            continue
        if _NARRATION.match(s) or s.endswith(("now", "now.")):
            i += 1
            continue
        break  # first non-narration line -> the report body
    if i == start:  # nothing recognizably narration at the top
        return md
    if i < n and lines[i].strip() in ("---", "***", "___"):  # drop a leftover thematic break
        i += 1
        while i < n and not lines[i].strip():
            i += 1
    stripped = "\n".join(lines[i:]).lstrip("\n")
    return stripped or md


def _clean_chat_markdown(md: str, title: str | None = None) -> str:
    """Portable markdown from a chat report: leading progress-narration stripped, internal vault
    links flattened to text (they resolve only in-app), wikilinks flattened, an optional title as H1."""
    body = _strip_report_preamble(_deref_local_links(_delink(md.strip())))
    t = str(title or "").strip()
    if t and not body.lstrip().startswith("# "):
        body = f"# {t}\n\n{body}"
    return body.strip() + "\n"


async def api_chat_export(request: Request, fmt: str = Query(...)):
    """Export a chat report (the assistant markdown the browser POSTs) as md/docx/pdf through the
    same clean+pandoc pipeline as page downloads. Internal vault links are flattened to text so the
    file carries no dead paths — the citations only resolve inside the reader."""
    if fmt not in _DL_MIME:
        raise HTTPException(400, "fmt must be md|docx|pdf")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    content = str((data or {}).get("content") or "").strip()
    if not content:
        raise HTTPException(400, "no content")
    if len(content) > 200_000:
        raise HTTPException(413, "content too large")
    title = str((data or {}).get("title") or "").strip()[:200] or None
    clean = _clean_chat_markdown(content, title)
    blob = clean.encode("utf-8") if fmt == "md" else _pandoc(clean, fmt, title)
    fname = f"report-{datetime.date.today().isoformat()}.{fmt}"
    return Response(
        content=blob,
        media_type=_DL_MIME[fmt],
        headers={"Content-Disposition": f'attachment; filename="{quote(fname)}"'},
    )


def api_search(q: str = Query(...), limit: int = 40):
    q = q.strip()
    if len(q) < 2:
        return {"q": q, "results": []}
    # `!_?*` (underscore + ≥1 char), NOT `!_*` — the latter also prunes the bare-`_` reshard bucket
    # (entities/x/_/x-force.md), making a resharded entity browsable-but-unfindable. Mirrors
    # _is_reserved_seg's bare-`_` exemption so search agrees with browse (batch-2 gate).
    cmd = [
        "rg",
        "-i",
        "-F",
        "-m1",
        "--no-heading",
        "-n",
        "--no-messages",
        "--max-columns",
        "240",
        "-g",
        "*.md",
        "-g",
        "!*.bak.*",
        "-g",
        "!_?*",
        "--",
        q,
        str(WIKI),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=12, text=True)
    except FileNotFoundError:
        raise HTTPException(503, "ripgrep not installed")
    except subprocess.TimeoutExpired:
        return {"q": q, "results": [], "truncated": True}
    base = str(WIKI.resolve())
    seen, rows = set(), []
    for line in proc.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath, _ln, text = parts
        try:
            rel = str(Path(fpath).resolve().relative_to(base))
        except (ValueError, OSError):
            continue
        rel = rel[:-3] if rel.endswith(".md") else rel
        if rel in seen:
            continue
        seen.add(rel)
        d = rel.split("/", 1)[0]
        rows.append(
            {
                "path": rel,
                "dir": d,
                "title": Path(rel).name,
                "snippet": re.sub(r"\s+", " ", text).strip()[:200],
            }
        )
        if len(rows) >= 1500:
            break
    ql = q.lower()
    rows.sort(
        key=lambda r: (
            0 if ql in r["title"].lower() else 1,
            _SEARCH_RANK.get(r["dir"], 8),
            r["path"],
        )
    )
    return {"q": q, "total": len(rows), "results": rows[: max(1, min(limit, 100))]}
