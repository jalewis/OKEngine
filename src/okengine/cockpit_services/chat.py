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


def _chat_enabled() -> bool:
    return bool(_AGENT_API and _AGENT_KEY)


def _budget_tripped() -> bool:
    """Return whether the shared model-budget actuator has paused spending."""
    try:
        return (VAULT / ".okengine" / "budget-paused").exists()
    except OSError:
        return False


async def api_chat(request: Request):
    """Relay an OpenAI-style chat turn to the Hermes agent and stream its SSE back. The upstream
    key is held server-side; the client only ever sees the token stream."""
    if not _chat_enabled():
        raise HTTPException(503, "agent chat not configured")
    if _budget_tripped():
        raise HTTPException(
            503,
            "agent chat paused — the deployment is over its model-token budget "
            "(budget-guard). It resumes when usage ages back under budget, or "
            "after `framework budget --resume`.",
        )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad request body")
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "messages required")
    # Sanitize: keep only user/assistant turns, bound count + size. The client may NOT set a
    # system message — grounding is server-controlled and prepended below.
    clean = []
    for m in raw[-_CHAT_MAX_MSGS:]:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), str(m.get("content") or "").strip()[:_CHAT_MAX_CHARS]
        if role in ("user", "assistant") and content:
            clean.append({"role": role, "content": content})
    if not clean:
        raise HTTPException(400, "no valid messages")

    messages = [{"role": "system", "content": _AGENT_SYSTEM}] + clean
    payload = json.dumps({"model": _AGENT_MODEL, "stream": True, "messages": messages}).encode()
    upstream = urllib.request.Request(
        f"{_AGENT_API}/chat/completions",
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {_AGENT_KEY}", "Content-Type": "application/json"},
    )

    def relay():
        try:
            with urllib.request.urlopen(upstream, timeout=300) as r:  # nosec B310 - validated http(s) agent config
                for chunk in r:  # passthrough raw SSE bytes
                    yield chunk
        except urllib.error.HTTPError as e:
            yield b"data: " + json.dumps({"error": f"agent error {e.code}"}).encode() + b"\n\n"
        except Exception:
            yield b"data: " + json.dumps({"error": "agent unreachable"}).encode() + b"\n\n"

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def favicon():
    return FileResponse(
        STATIC / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # Cache-bust app.js/style.css by content hash (ported from the reader): bare URLs let the
    # browser serve a stale UI from heuristic cache after a cockpit update. The ?v=<hash> only
    # changes when the asset changes, so unchanged assets still cache; changed ones are fetched
    # immediately.
    try:
        h = hashlib.sha1(usedforsecurity=False)
        for asset in ("style.css", "app.js"):
            p = STATIC / asset
            if p.is_file():
                h.update(p.read_bytes())
        v = h.hexdigest()[:8]
        html = html.replace("/static/app.js", f"/static/app.js?v={v}").replace(
            "/static/style.css", f"/static/style.css?v={v}"
        )
    except OSError:
        pass
    return html


def healthz():
    return {"ok": True, "vault": str(WIKI), "vault_present": WIKI.is_dir()}
