#!/usr/bin/env python3
"""okengine-reader — standalone, read-only web reader for an OKEngine/OKF vault.

Domain-agnostic: it discovers the vault's structure at runtime (the directories
under `wiki/` and each page's `type`/`title` frontmatter) and ships no knowledge
of any particular pack. Browse by directory, render any page (markdown + embeds +
[[wikilinks]]), follow the IWE backlink graph ("what links here"), search the
whole vault (ripgrep), and export a page to md/docx/pdf (pandoc).

Deliberately SEPARATE from the Hermes agent/console: imports no hermes modules,
makes no calls to the gateway or dashboard, and serves only from a READ-ONLY
mount of the vault. It keeps working even if the entire Hermes stack is down.

Env:
  VAULT_DIR                 read-only vault root (default /vault)
  PORT                      listen port (default 9200)
  OKENGINE_READER_PASSWORD  if set, require HTTP Basic auth (see _BasicAuth)
  OKENGINE_READER_USER      Basic-auth username (default "okengine")
  IWE_BIN                   path to the IWE binary (default "iwe")
"""
from __future__ import annotations

import os
import re
import json
import base64
import hashlib
import hmac
import threading
import time
import subprocess
import tempfile
import urllib.request
import urllib.error
from urllib.parse import quote, urlparse
from pathlib import Path

import yaml
import markdown as md
import nh3
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import limits

VAULT = Path(os.environ.get("VAULT_DIR", "/vault"))
WIKI = VAULT / "wiki"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="OKEngine · vault reader", docs_url=None, redoc_url=None)


# ── optional HTTP Basic auth ─────────────────────────────────────────────────
class _BasicAuth:
    """ASGI middleware: require HTTP Basic auth when OKENGINE_READER_PASSWORD is
    set. A public reference deployment leaves it unset (open); a private vault
    sets it. `/healthz` stays open so container health checks don't need creds.
    Browser-native (the browser prompts and resends the header on every request,
    including the static shell), unlike a bearer token."""

    def __init__(self, app, user: str, password: str):
        self.app = app
        self._expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") != "/healthz":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            # constant-time compare — `!=` on str leaks length/prefix via timing.
            if not hmac.compare_digest(provided, self._expected):
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"www-authenticate", b'Basic realm="okengine-reader"'),
                    (b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


_READER_PASSWORD = os.environ.get("OKENGINE_READER_PASSWORD", "")
if _READER_PASSWORD:
    app.add_middleware(_BasicAuth, user=os.environ.get("OKENGINE_READER_USER", "okengine"),
                       password=_READER_PASSWORD)


# ── public-deployment hardening (expensive endpoints) ───────────────────────
# A reader exposed to the internet must not let unauthenticated clients spawn
# unbounded pandoc/IWE/ripgrep work. OKENGINE_READER_PUBLIC=1 flips on safe
# defaults; each knob is independently overridable. See okengine-reader/README.md.
_PUBLIC = limits.flag("OKENGINE_READER_PUBLIC", False)
# pandoc/WeasyPrint exports (docx/pdf): allowed locally, OFF in public mode unless
# explicitly enabled. `md` export is always allowed (no subprocess, cheap).
_EXPORTS_ENABLED = limits.flag("OKENGINE_READER_EXPORTS", not _PUBLIC)
# Concurrency caps: bound how many heavy subprocesses can run at once.
_EXPORT_SEM = threading.BoundedSemaphore(limits.intenv("OKENGINE_READER_MAX_EXPORT", 2, lo=1))
_SEARCH_SEM = threading.BoundedSemaphore(limits.intenv("OKENGINE_READER_MAX_SEARCH", 4, lo=1))
# Per-IP rate limit (req/min) on the expensive endpoints; 0 disables. Safe by default even
# off-public (a bounded cap, not unlimited — okengine#53): public is stricter (60), local is
# generous (300, ~5/s — a single user never hits it) but still bounds a runaway/abuse. Set
# OKENGINE_READER_RATE=0 to explicitly disable; a reverse proxy can layer more.
_RATE = limits.RateLimiter(limits.intenv("OKENGINE_READER_RATE", 60 if _PUBLIC else 300, lo=0))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _guard(request: Request, sem: threading.BoundedSemaphore):
    """Rate-limit + concurrency-cap an expensive endpoint. Returns the semaphore's
    release callable (caller MUST call it in a finally); raises 429/503 otherwise."""
    if not _RATE.allow(_client_ip(request)):
        raise HTTPException(429, "rate limit exceeded — slow down")
    if not sem.acquire(blocking=False):
        raise HTTPException(503, "server busy (too many concurrent requests) — retry shortly")
    return sem.release


# ── markdown / frontmatter helpers ──────────────────────────────────────────
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_WIKILINK = re.compile(r"\[\[\s*([^\]|#\n\\]*)(?:#([^\]|\n]+))?(?:\\?\|\s*([^\]\n]+?))?\s*\]\]")
_EMBED = re.compile(r"!\[\[\s*([^\]\n#|]+?)\s*(?:#[^\]\n|]+)?(?:\|[^\]\n]+)?\s*\]\]")
_H1_RE = re.compile(r"^#\s+.*$", re.MULTILINE)


def _skip(name: str) -> bool:
    """Reserved / non-content / generated files the reader never lists or renders
    (underscore/dot reserved, backups, and the generated per-directory index pages
    build_index_tree/rebuild_index emit — `INDEX.md` / `INDEX-pNN.md` / `index.md`)."""
    return (name.startswith(("_", ".")) or ".bak." in name
            or name in ("INDEX.md", "index.md")
            or name.startswith(("INDEX-", "index-")))


def _within(base: Path, p: Path) -> bool:
    """True iff `p` (already resolved) is inside `base` — path-traversal guard."""
    try:
        p.relative_to(base.resolve())
        return True
    except ValueError:
        return False


try:
    # libyaml — ~7x faster frontmatter parsing, which is the dominant cost of the
    # full-vault scan behind the BY KIND counts. Falls back to the pure-Python loader
    # (identical semantics) if the C extension isn't built into PyYAML.
    from yaml import CSafeLoader as _YAML_LOADER
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _YAML_LOADER


def split_fm(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.load(m.group(1), Loader=_YAML_LOADER) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _wl_display(m) -> str:
    """Display text for a wikilink: alias, else target's last segment, else heading."""
    alias = (m.group(3) or "").strip()
    if alias:
        return alias
    target = (m.group(1) or "").strip()
    if target:
        return target.split("/")[-1]
    return (m.group(2) or "").strip()


def _delink(s: str) -> str:
    """Render a wikilink as plain display text (for portable markdown export)."""
    return _WIKILINK.sub(_wl_display, s)


def _resolve_embeds(text: str, depth: int = 0) -> str:
    """Inline Obsidian embeds ![[target]] with the target file's body, recursively
    (depth-limited). Targets are resolved anywhere under the vault, so this works
    on any pack's layout."""
    if depth > 3:
        return text

    def repl(mo: "re.Match") -> str:
        target = mo.group(1).strip()
        if re.search(r"\.(png|jpe?g|gif|svg|webp|pdf)$", target, re.I):
            return f"_[embedded asset: {target}]_"
        cand = WIKI / (target + ".md")
        if not cand.is_file():
            name = Path(target).name + ".md"
            hits = list(WIKI.rglob(name)) if WIKI.is_dir() else []
            cand = hits[0] if hits else None
        if not cand:
            return f"_[missing embed: {target}]_"
        try:
            cp = cand.resolve()
            if not _within(WIKI, cp):
                return "_[blocked embed]_"
            _, body = split_fm(cp.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return f"_[unreadable embed: {target}]_"
        return _resolve_embeds(body, depth + 1)

    return _EMBED.sub(repl, text)


def _linkify(s: str) -> str:
    """Wikilinks -> clickable anchors (resolved client-side via /api/page)."""
    def repl(m: "re.Match") -> str:
        target = (m.group(1) or "").strip()
        disp = _wl_display(m)
        if not target:                       # same-page anchor [[#heading]] — no page
            return disp
        return f'<a class="wl" data-page="{target.replace(chr(34), "&quot;")}">{disp}</a>'
    s = _WIKILINK.sub(repl, s)
    # drop dangling "[[" from truncated wikilinks
    return re.sub(r"\[\[(?![^\]\n]*\]\])", "", s)


# Sanitizer allowlist for rendered markdown. Vault content is partly agent- and
# feed-derived, and the reader may run as a PUBLIC service, so the markdown→HTML
# output is scrubbed before it reaches the browser (it is injected via innerHTML).
# The set covers what our markdown extensions + `_linkify` emit; everything else
# (inline <script>, event handlers, javascript: URLs, …) is stripped by nh3.
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "em", "strong", "b", "i",
    "code", "pre", "blockquote", "ul", "ol", "li", "a", "img", "span", "del",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title", "class", "data-page", "target"},
    "img": {"src", "alt", "title"},
    "td": {"align"},
    "th": {"align"},
    "code": {"class"},
    "span": {"class"},
}


def render_md(body: str) -> str:
    body = _resolve_embeds(body)
    body = re.sub(r"```dataview(js)?\n.*?\n```",
                  "_[Dataview view — open in Obsidian to compute]_", body, flags=re.DOTALL)
    body = _linkify(body)
    html = md.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)


# ── browse: discover the vault structure at runtime ─────────────────────────
_DIR_CACHE: dict[str, tuple[float, list[dict]]] = {}
# Vault is :ro and cron-refreshed. The background warmer (_WARM_INTERVAL) recomputes
# cache entries well inside this TTL, so user requests never trigger the cold full-vault
# scan that the BY KIND counts require; the TTL is just the safety net.
_DIR_TTL = 900.0          # seconds
_WARM_INTERVAL = 600.0    # background cache-refresh cadence (< _DIR_TTL)
# Init timestamp = -inf (not 0.0): the freshness check is `monotonic() - ts < _DIR_TTL`, and
# monotonic() is seconds-since-boot — on a freshly-booted host (e.g. a CI runner or a just-started
# deployment) it can be < _DIR_TTL, which would make the EMPTY initial entry read as "fresh" and
# skip the first schema read for up to _DIR_TTL. -inf forces the first call to always miss + load.
_EXCLUDE_CACHE: tuple[float, frozenset[str]] = (float("-inf"), frozenset())
_GROUPS_CACHE: tuple[float, list[tuple[str, frozenset[str]]]] = (float("-inf"), [])
_RAILTOP_CACHE: tuple[float, tuple[str, tuple[str, ...]]] = (float("-inf"), ("", ()))


# `dashboards/` holds synthesized digests (the brief, HOT, kb-health, …) that are MEANT to be
# read — the payoff of the vault. schema.yaml `exclude:` scopes CONFORMANCE (don't validate
# generated pages), NOT reader visibility — so the reader SURFACES dashboards/ (flagged
# `derived` in the rail) and only hides operator-internal excludes like operational/ (okengine#117).
_SURFACED_DERIVED = frozenset({"dashboards"})


def _excluded_dirs() -> frozenset[str]:
    """Top-level wiki/ dir names the reader hides from the browse rail, page lists, and search:
    the pack's schema.yaml `exclude:` set MINUS the surfaced synthesized namespaces
    (`dashboards/`, see _SURFACED_DERIVED — generated but meant to be read). Cached (vault :ro)."""
    global _EXCLUDE_CACHE
    now = time.monotonic()
    if now - _EXCLUDE_CACHE[0] < _DIR_TTL:
        return _EXCLUDE_CACHE[1]
    out: set[str] = set()
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            for e in (sch.get("exclude") or []):
                seg = str(e).strip().strip("/")
                if seg.startswith("wiki/"):
                    seg = seg[len("wiki/"):]
                seg = seg.strip("/").split("/")[0]
                if seg:
                    out.add(seg)
        except Exception:
            pass
    _EXCLUDE_CACHE = (now, frozenset(out) - _SURFACED_DERIVED)
    return _EXCLUDE_CACHE[1]


def _display_groups() -> list[tuple[str, frozenset[str]]]:
    """Optional `display_groups:` from the pack's schema.yaml — a label -> [types]
    map that lets the reader browse pages BY KIND across namespaces (e.g. a pack
    might map one label to several related page types). Domain-agnostic: the reader
    hardcodes no labels; the pack supplies them. Order is preserved."""
    global _GROUPS_CACHE
    now = time.monotonic()
    if now - _GROUPS_CACHE[0] < _DIR_TTL:
        return _GROUPS_CACHE[1]
    groups: list[tuple[str, frozenset[str]]] = []
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            dg = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("display_groups") or {}
            if isinstance(dg, dict):
                for label, types in dg.items():
                    ts = frozenset(str(t).strip().lower() for t in (types or []) if str(t).strip())
                    if str(label).strip() and ts:
                        groups.append((str(label).strip(), ts))
        except Exception:
            pass
    _GROUPS_CACHE = (now, groups)
    return groups


def _rail_top_section() -> tuple[str, tuple[str, ...]]:
    """Optional `rail_top_section:` from the pack's schema.yaml — {label, namespaces}
    — a pack-declared group of synthesized-output namespaces pinned to the top of
    the browse rail, distinct from raw storage namespaces. Domain-agnostic: the
    pack names the section and lists its member namespaces."""
    global _RAILTOP_CACHE
    now = time.monotonic()
    if now - _RAILTOP_CACHE[0] < _DIR_TTL:
        return _RAILTOP_CACHE[1]
    label, ns = "", ()
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            d = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("rail_top_section") or {}
            if isinstance(d, dict):
                label = str(d.get("label") or "").strip()
                ns = tuple(str(x).strip() for x in (d.get("namespaces") or []) if str(x).strip())
        except Exception:
            pass
    _RAILTOP_CACHE = (now, (label, ns))
    return _RAILTOP_CACHE[1]


def _top_dirs() -> list[str]:
    """Non-excluded top-level wiki/ directory names."""
    if not WIKI.is_dir():
        return []
    ex = _excluded_dirs()
    return [d.name for d in WIKI.iterdir()
            if d.is_dir() and not _skip(d.name) and d.name not in ex]


def _pages_of_types(types: frozenset[str]) -> list[dict]:
    """Every page across all non-excluded namespaces whose `type` is in `types`."""
    out = [pg for sub in _top_dirs() for pg in _scan_dir(sub)
           if pg["type"].lower() in types]
    out.sort(key=lambda r: (r["title"].lower(), r["path"]))
    return out


# A listing needs only a page's frontmatter + its first H1, both of which live in the
# file's head. Reading the whole body for every page is the dominant cost of the cold
# full-vault scan, so cap the read.
_FM_SCAN_BYTES = 16384


def _read_head(p: Path, limit: int = _FM_SCAN_BYTES) -> str:
    """Read up to `limit` bytes of a page (frontmatter + first H1) instead of the whole
    file, so a full-vault metadata scan does not pay to read large page bodies."""
    try:
        with p.open("rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _page_meta(p: Path) -> dict:
    """{path, title, type, updated} for one page, from its frontmatter (head-only read)."""
    rel = str(p.relative_to(WIKI.resolve()))
    rel = rel[:-3] if rel.endswith(".md") else rel
    fm, body = split_fm(_read_head(p))
    title = str(fm.get("title") or fm.get("name") or "").strip()
    if not title:
        h1 = _H1_RE.search(body)
        title = h1.group(0).lstrip("# ").strip() if h1 else Path(rel).name
    return {"path": rel, "title": title, "type": str(fm.get("type") or "").strip(),
            "updated": str(fm.get("updated") or fm.get("created") or "")[:10]}


def _scan_dir(sub: str, force: bool = False) -> list[dict]:
    """Page metadata for every page under wiki/<sub> (recursive). Cached for _DIR_TTL to
    keep large-vault scans off the hot path; the background warmer refreshes it with
    force=True so user requests are served from a warm cache, never the cold scan."""
    if sub in _excluded_dirs():
        return []
    now = time.monotonic()
    hit = _DIR_CACHE.get(sub)
    if not force and hit and now - hit[0] < _DIR_TTL:
        return hit[1]
    base = (WIKI / sub).resolve()
    out: list[dict] = []
    if base.is_dir() and _within(WIKI, base):
        for p in base.rglob("*.md"):
            if _skip(p.name):
                continue
            out.append(_page_meta(p.resolve()))
    out.sort(key=lambda r: (r["title"].lower(), r["path"]))
    _DIR_CACHE[sub] = (now, out)
    return out


# ── background cache warmer ─────────────────────────────────────────────────
def _warm_cache() -> None:
    """Recompute the per-namespace page-metadata caches so no user request pays the cold
    full-vault scan (the BY KIND counts read every page's frontmatter)."""
    try:
        for sub in _top_dirs():
            _scan_dir(sub, force=True)
    except Exception:
        pass  # a transient FS error must not kill the warmer; the next tick retries


def _warm_loop() -> None:
    while True:
        _warm_cache()
        time.sleep(_WARM_INTERVAL)


def _start_warmer() -> None:
    """Start the background warmer once (idempotent): warm at boot, then every
    _WARM_INTERVAL, so the sidebar's BY KIND counts are always served warm."""
    if getattr(_start_warmer, "_started", False):
        return
    _start_warmer._started = True
    threading.Thread(target=_warm_loop, name="reader-cache-warmer", daemon=True).start()


@app.on_event("startup")
def _on_startup() -> None:
    _start_warmer()


def _about_info() -> dict:
    """Deployment identity for the About panel: the vault name + version (pack.yaml)
    and the engine/Hermes pins (engine.version). Read fresh — both files are tiny
    and About is cold."""
    info = {"vault": "", "vault_version": "", "engine_version": "", "hermes_pin": "",
            "project_url": ""}

    def _yaml(p: Path) -> dict:
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    pk = _yaml(VAULT / "pack.yaml")
    info["vault"] = str(pk.get("name") or "")
    info["vault_version"] = str(pk.get("version") or "")
    ev = _yaml(VAULT / "engine.version")
    info["engine_version"] = str(ev.get("version") or "")
    info["hermes_pin"] = str(ev.get("hermes_pin") or "")
    # The project/repo link for the About panel is deployment config — the engine ships no
    # hardcoded URL (stays publishable / no private host). Env wins; pack.yaml is fallback.
    info["project_url"] = os.environ.get("OKENGINE_PROJECT_URL") or str(pk.get("project_url") or "")
    return info


@app.get("/api/about")
def api_about():
    """Vault name + engine/Hermes versions for the reader's About panel."""
    info = _about_info()
    info["chat_enabled"] = _chat_enabled()      # gate the Chat tab on a configured agent
    return info


# ── agent chat (relay to the Hermes OpenAI-compatible api_server) ─────────────
# The reader runs NO model of its own. The Chat tab relays to THE agent (Hermes), which
# answers by NAVIGATING the OKF wiki via its graph tools — the wiki-as-memory demonstration,
# the deliberate counter to RAG (see docs/okf/guide-1 §3.1). Configured by env so a
# deployment without an agent endpoint simply never shows the tab.
_AGENT_API = os.environ.get("OKENGINE_AGENT_API", "").rstrip("/")
_AGENT_KEY = os.environ.get("OKENGINE_AGENT_KEY", "")
_AGENT_MODEL = os.environ.get("OKENGINE_AGENT_MODEL", "OKEngine Agent")
_CHAT_MAX_MSGS = limits.intenv("OKENGINE_READER_CHAT_MAX_MSGS", 24, lo=2)
_CHAT_MAX_CHARS = limits.intenv("OKENGINE_READER_CHAT_MAX_CHARS", 8000, lo=200)

# Grounding contract for the chat session (server-controlled — the browser cannot override
# it). The vault is the agent's memory and the FIRST stop; external research is written BACK
# so the corpus compounds. This is the OKF/agent-wiki thesis, not RAG.
_AGENT_SYSTEM = os.environ.get("OKENGINE_AGENT_SYSTEM") or (
    "You are the OKEngine vault agent. This OKF knowledge vault is your long-term memory and "
    "the FIRST place you look for anything. Open EVERY reply with a one-line acknowledgement "
    "of what you're about to do (e.g. \"Checking the vault for Scattered Spider…\") before you "
    "call any tools, so the user gets immediate feedback. Then:\n"
    "1. SEARCH THE VAULT FIRST — use your tools (search, then get_page / retrieve_context / "
    "find_references) and build your answer from those pages. Search is lexical, so it matches "
    "words not meanings: if the first query is thin, RETRY with synonyms and related terms "
    "before concluding the vault lacks it (e.g. health → medical / clinical / hospital / "
    "patient; actor → group / intrusion-set / threat-actor; ransomware → extortion). Cite each "
    "page you used inline as `path` (e.g. `entities/s/scattered-spider`).\n"
    "2. If the vault already covers it, answer ONLY from the vault — do not add outside or "
    "prior knowledge.\n"
    "3. If the vault is missing or thin on the topic, RESEARCH IT WITH YOUR WEB TOOLS — you "
    "have web search & scraping; use them to gather and verify facts from the open web — THEN "
    "write what you learn back into the vault with your write tools (create_entity / "
    "update_entity / append_to_section). Before writing a NEW page, first fetch an existing page "
    "of the SAME type and mirror its frontmatter field names exactly — reuse the established "
    "fields, do not invent new ones (e.g. use whatever attribution/status field that type "
    "already uses). Then tell the user which page you created or updated. The wiki must grow — "
    "every external fact you rely on gets captured so the next query finds it here.\n"
    "4. Never fabricate — and never claim you lack external access: you HAVE web search, so use "
    "it before giving up. Only call a fact unverifiable after a web search has actually failed "
    "to confirm it."
)


def _chat_enabled() -> bool:
    return bool(_AGENT_API and _AGENT_KEY)


@app.post("/api/chat")
async def api_chat(request: Request):
    """Relay an OpenAI-style chat turn to the Hermes agent and stream its SSE back. The
    upstream key is held server-side; the client only ever sees the token stream."""
    if not _chat_enabled():
        raise HTTPException(503, "agent chat not configured")
    if not _RATE.allow(_client_ip(request)):
        raise HTTPException(429, "rate limit exceeded — slow down")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "bad request body")
    raw = body.get("messages")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "messages required")
    # Sanitize: keep only user/assistant turns, bound count + size. The client may NOT set a
    # system message — grounding is server-controlled and prepended below (prevents a browser
    # from overriding the wiki-first contract).
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
        f"{_AGENT_API}/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {_AGENT_KEY}", "Content-Type": "application/json"})

    def relay():
        try:
            with urllib.request.urlopen(upstream, timeout=300) as r:
                for chunk in r:                          # passthrough raw SSE bytes
                    yield chunk
        except urllib.error.HTTPError as e:
            yield b"data: " + json.dumps({"error": f"agent error {e.code}"}).encode() + b"\n\n"
        except Exception:
            yield b"data: " + json.dumps({"error": "agent unreachable"}).encode() + b"\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Generated/derived page types (vs curated knowledge: source/entity/concept/…).
# `dashboard` is the OKF type for synthesized artifacts: briefings, HOT, indexes.
_DERIVED_TYPES = {"dashboard"}


def _dir_is_derived(md_paths: list[Path]) -> bool:
    """A namespace is 'derived' when its pages are generated artifacts (type:
    dashboard — briefings, dashboards, …) rather than curated knowledge. Decided by
    sampling a few pages' frontmatter `type` (cheap; namespaces are homogeneous)."""
    seen = derived = 0
    for p in md_paths[:8]:
        try:
            fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace")[:2000])
        except OSError:
            continue
        t = str(fm.get("type") or "").strip().lower()
        if t:
            seen += 1
            derived += t in _DERIVED_TYPES
    return seen > 0 and derived == seen


@app.get("/api/tree")
def api_tree():
    """Top-level directories under wiki/ with page counts — the browse rail. Each
    dir is flagged `derived` (generated content) vs curated knowledge."""
    dirs = []
    if WIKI.is_dir():
        excluded = _excluded_dirs()
        for d in sorted(WIKI.iterdir()):
            if not d.is_dir() or _skip(d.name) or d.name in excluded:
                continue
            mds = [p for p in d.rglob("*.md") if not _skip(p.name)]
            if mds:
                dirs.append({"dir": d.name, "count": len(mds), "derived": _dir_is_derived(mds)})
    label, ns = _rail_top_section()
    present = {d["dir"] for d in dirs}
    top = [n for n in ns if n in present]   # featured members that actually have pages
    return {"vault": str(WIKI), "dirs": dirs,
            "top_section": {"label": label, "namespaces": top}}


@app.get("/api/groups")
def api_groups():
    """Pack-declared display groups (label -> page count) — browse entities BY KIND
    (e.g. all pages of a few related types) across namespaces. Empty when the pack declares none."""
    return {"groups": [{"label": label, "count": len(_pages_of_types(types))}
                       for label, types in _display_groups()]}


@app.get("/api/pages")
def api_pages(dir: str = Query(default=""), group: str = Query(default="")):
    """Pages under a top-level directory, OR (with ?group=Label) every page whose
    `type` is in that display group, across namespaces."""
    if group:
        for label, types in _display_groups():
            if label == group:
                return {"group": group, "pages": _pages_of_types(types)}
        raise HTTPException(404, "unknown group")
    if "/" in dir or ".." in dir or dir.startswith((".", "/")):
        raise HTTPException(400, "bad dir")
    return {"dir": dir, "pages": _scan_dir(dir)}


# ── render any page ─────────────────────────────────────────────────────────
def _resolve_page(path: str) -> Path:
    """Resolve a wiki-relative page key (no .md) to a file, confined to the vault.
    Falls back to a basename search anywhere under the vault for bare names."""
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "bad path")
    cand = WIKI / (path + ".md")
    if not cand.is_file():
        name = Path(path).name + ".md"
        hits = [p for p in WIKI.rglob(name)
                if not _skip(p.name)] if WIKI.is_dir() else []
        if len(hits) > 1:
            # A wikilink resolves to the CANONICAL page, not a per-source observation copy with
            # the same slug under the excluded observations/ layer (else a multi-source entity —
            # which has entities/<l>/<slug> PLUS observations/<src>/<l>/<slug> — is "ambiguous"
            # and 404s). Drop excluded namespaces, then prefer the entity page.
            excl = _excluded_dirs()
            pref = [p for p in hits if p.relative_to(WIKI).parts[0] not in excl] or hits
            ent = [p for p in pref if p.relative_to(WIKI).parts[0] == "entities"]
            hits = ent or pref
        if len(hits) > 1:
            raise HTTPException(409, "ambiguous page basename; use the full wiki-relative path")
        cand = hits[0] if hits else None
    if not cand:
        raise HTTPException(404, "page not found")
    cp = cand.resolve()
    if not _within(WIKI, cp) or _skip(cp.name):
        raise HTTPException(403, "blocked")
    return cp


# Frontmatter keys not worth a panel row: shown in the page header (title/name/type) or
# pure OKF write-machinery (version counter, the raw-ingest dedupe path).
_META_PANEL_SKIP = {"title", "name", "type", "version", "raw"}
# Record-keeping / provenance fields — real but low-signal for a reader. SURFACED knowledge
# (aliases, origin, refs, …) stays visible; these get tucked into a collapsed disclosure so
# they don't bury the intel. Domain-agnostic (OKF envelope + assembler provenance).
_META_SECONDARY = {"tlp", "created", "updated", "last_updated", "last_seen", "first_seen",
                   "assembled_from", "tier", "tlp_caveat"}
# `conflicts` + `needs_review` get a dedicated provenance view (api_page), not a raw row.


# ── multi-source conflict / provenance view (okengine#42) ────────────────────
_SRC_REL_CACHE: tuple[float, dict] = (0.0, {})
_REL_RANK = {c: i for i, c in enumerate("FEDCBA")}    # A=5 (highest) … F=0; unknown -> -1


def _source_reliability() -> dict:
    """{source -> Admiralty reliability A–F} from the pack's schema.yaml `source_registry`,
    so the conflict view can label each claim. Domain-agnostic; cached (vault is :ro)."""
    global _SRC_REL_CACHE
    now = time.monotonic()
    if now - _SRC_REL_CACHE[0] < _DIR_TTL:
        return _SRC_REL_CACHE[1]
    out: dict = {}
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            reg = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("source_registry") or {}
            for k, v in (reg.items() if isinstance(reg, dict) else []):
                r = str((v or {}).get("reliability") or "").strip()
                if r:
                    out[str(k)] = r
        except Exception:
            pass
    _SRC_REL_CACHE = (now, out)
    return out


def _val_text(v) -> str:
    return _meta_compact_dict(v) if isinstance(v, dict) else str(v)


def _shape_conflicts(fm: dict) -> list[dict]:
    """Turn the assembler's `conflicts` frontmatter into a per-field 'what each source says'
    structure, each value tagged with its source(s) + Admiralty reliability + a rank for the
    >= B filter, and the headline (winning) value flagged."""
    rel = _source_reliability()
    out: list[dict] = []
    for c in (fm.get("conflicts") or []):
        if not isinstance(c, dict):
            continue
        headline = c.get("headline")
        vals: list[dict] = []
        for v in (c.get("values") or []):
            srcs = [{"name": str(s), "reliability": rel.get(str(s), "")}
                    for s in (v.get("sources") or [])]
            rank = max((_REL_RANK.get(str(s["reliability"]).upper()[:1], -1) for s in srcs),
                       default=-1)
            vals.append({"value": _val_text(v.get("value")), "sources": srcs, "rank": rank,
                         "is_headline": v.get("value") == headline})
        out.append({"field": str(c.get("field") or ""), "headline": _val_text(headline),
                    "values": vals})
    return out


_OBS_INDEX_CACHE: tuple[float, dict] = (0.0, {})


def _observations_by_canonical() -> dict:
    """{canonical-slug -> [{source, key}]} over `observations/`, for canonical→source drill-down.
    Cached for _DIR_TTL (head-read only)."""
    global _OBS_INDEX_CACHE
    now = time.monotonic()
    if now - _OBS_INDEX_CACHE[0] < _DIR_TTL:
        return _OBS_INDEX_CACHE[1]
    idx: dict = {}
    base = WIKI / "observations"
    if base.is_dir():
        for p in base.rglob("*.md"):
            if _skip(p.name):
                continue
            fm, _ = split_fm(_read_head(p))
            canon = str(fm.get("canonical") or "").strip().lower()
            if canon:
                key = str(p.resolve().relative_to(WIKI.resolve()))[:-3]
                idx.setdefault(canon, []).append({"source": str(fm.get("source") or ""), "key": key})
    _OBS_INDEX_CACHE = (now, idx)
    return idx


def _meta_compact_dict(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, "", [], {}))


def _url_label(url: str) -> str:
    """Friendly link text for a bare URL — its host minus 'www.' (e.g. attack.mitre.org),
    so a url-valued field reads as a tidy link, not a wall of query string."""
    try:
        host = urlparse(url).netloc
    except Exception:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    return host or url


def _meta_values(v) -> list[dict]:
    """One frontmatter value -> display chips. http(s) scalars and list items carrying a
    url/href (e.g. refs/rels) become links (friendly host label); dicts compact to k=v."""
    out: list[dict] = []
    for el in (v if isinstance(v, list) else [v]):
        if isinstance(el, dict):
            url = el.get("url") or el.get("href")
            txt = (el.get("id") or el.get("value") or el.get("name") or el.get("std")
                   or (_url_label(url) if url else None) or _meta_compact_dict(el))
            out.append({"text": str(txt), "url": str(url)} if url else {"text": str(txt)})
        else:
            s = str(el)
            out.append({"text": _url_label(s), "url": s} if s.startswith(("http://", "https://"))
                       else {"text": s})
    return out


def _meta_panel_items(fm: dict) -> dict:
    """Structured frontmatter split into `primary` (the page's intel — aliases, origin,
    refs, … — surfaced visibly) and `secondary` (record-keeping/provenance — collapsed).
    Domain-agnostic: renders whatever fields exist, in frontmatter order."""
    primary: list[dict] = []
    secondary: list[dict] = []
    if not isinstance(fm, dict):
        return {"primary": primary, "secondary": secondary}
    for k, v in fm.items():
        if k in _META_PANEL_SKIP or v is None or v == "" or v == [] or v == {}:
            continue
        label = str(k).replace("_", " ").replace("-", " ").strip()
        item = {"label": label[:1].upper() + label[1:], "values": _meta_values(v)}
        (secondary if k in _META_SECONDARY else primary).append(item)
    return {"primary": primary, "secondary": secondary}


@app.get("/api/page")
def api_page(path: str = Query(...)):
    """Render any wiki page for click-through navigation."""
    cp = _resolve_page(path)
    fm, body = split_fm(cp.read_text(encoding="utf-8", errors="replace"))
    title = fm.get("title") or fm.get("name") or Path(path).name
    m = _meta_panel_items(fm)
    slug = cp.stem.lower()
    return {"path": path, "title": str(title), "type": str(fm.get("type") or ""),
            "rel": str(cp.relative_to(WIKI.resolve()))[:-3], "html": render_md(body),
            "meta": m["primary"], "meta_aux": m["secondary"],
            "conflicts": _shape_conflicts(fm), "needs_review": bool(fm.get("needs_review")),
            "observations": _observations_by_canonical().get(slug, [])}


# ── downloads (md / docx / pdf via pandoc) ─────────────────────────────────
def _clean_markdown(raw: str, title: str | None = None) -> str:
    """Portable, readable markdown: frontmatter dropped, embeds inlined, dataview
    removed, wikilinks flattened to text, title as H1."""
    fm, body = split_fm(raw)
    body = _resolve_embeds(body)
    body = re.sub(r"```dataview(js)?\n.*?\n```", "", body, flags=re.DOTALL)
    body = _delink(body)
    t = (title or fm.get("title") or fm.get("name") or "").strip()
    if t and not body.lstrip().startswith("# "):
        body = f"# {t}\n\n{body}"
    return body.strip() + "\n"


def _pandoc(clean_md: str, fmt: str, title: str | None = None) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.md"
        out = Path(td) / f"out.{fmt}"
        src.write_text(clean_md, encoding="utf-8")
        cmd = ["pandoc", str(src), "-f", "markdown+pipe_tables", "-o", str(out)]
        # A non-empty title keeps standalone HTML/docx out of pandoc's "Defaulting
        # to 'in' as the title" fallback (the temp filename stem leaking through).
        cmd += ["--metadata", f"title={(title or '').strip() or 'OKEngine page'}"]
        if fmt == "docx":
            cmd += ["--standalone"]
        if fmt == "pdf":
            cmd += ["--pdf-engine=weasyprint"]
        try:
            # cwd must be writable: pandoc creates temp files in CWD, and /app is
            # root-owned (we run as the reader uid).
            subprocess.run(cmd, check=True, capture_output=True, timeout=90, cwd=td)
        except FileNotFoundError:
            raise HTTPException(503, "pandoc not installed")
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"convert failed: {e.stderr.decode('utf-8','replace')[:300]}")
        return out.read_bytes()


_DL_MIME = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


@app.get("/api/download")
def api_download(request: Request, fmt: str, path: str = Query(...)):
    if fmt not in _DL_MIME:
        raise HTTPException(400, "fmt must be md|docx|pdf")
    cp = _resolve_page(path)
    raw = cp.read_text(encoding="utf-8", errors="replace")
    fm, _ = split_fm(raw)
    title = (fm.get("title") or fm.get("name") or Path(path).stem).strip()
    clean = _clean_markdown(raw)
    if fmt == "md":                       # cheap: no subprocess, no gate
        data = clean.encode("utf-8")
    else:                                 # docx/pdf: pandoc/WeasyPrint — guarded
        if not _EXPORTS_ENABLED:
            raise HTTPException(403, "docx/pdf export is disabled on this deployment "
                                     "(use md, or set OKENGINE_READER_EXPORTS=1)")
        release = _guard(request, _EXPORT_SEM)
        try:
            data = _pandoc(clean, fmt, title=title)
        finally:
            release()
    fname = f"{Path(path).name}.{fmt}"
    # RFC 5987 filename* — encodes non-ASCII safely without header injection.
    return Response(content=data, media_type=_DL_MIME[fmt],
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


# ── global search (ripgrep across the vault) ───────────────────────────────
@app.get("/api/search")
def api_search(request: Request, q: str = Query(...), limit: int = 40):
    q = q.strip()
    if len(q) < 2:
        return {"q": q, "results": []}
    release = _guard(request, _SEARCH_SEM)   # rate-limit + bound concurrent ripgreps
    # Keep search consistent with the browse rail: drop backups/reserved, the
    # generated index pages, and any schema-excluded dir (#25).
    ignore = ["!*.bak.*", "!_*", "!INDEX.md", "!index.md", "!INDEX-*", "!index-*"]
    ignore += [f"!{d}/**" for d in _excluded_dirs()]
    glob_args = ["-g", "*.md"]
    for g in ignore:
        glob_args += ["-g", g]
    cmd = ["rg", "-i", "-F", "-m1", "--no-heading", "-n", "--no-messages",
           "--max-columns", "240", *glob_args, "--", q, str(WIKI)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=12, text=True)
    except FileNotFoundError:
        raise HTTPException(503, "ripgrep not installed")
    except subprocess.TimeoutExpired:
        return {"q": q, "results": [], "truncated": True}
    finally:
        release()
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
        rows.append({"path": rel, "dir": d, "title": Path(rel).name,
                     "snippet": re.sub(r"\s+", " ", text).strip()[:200]})
        if len(rows) >= 1500:
            break
    ql = q.lower()
    # title hits first, then group by directory name, then path — fully generic.
    rows.sort(key=lambda r: (0 if ql in r["title"].lower() else 1, r["dir"], r["path"]))
    return {"q": q, "total": len(rows), "results": rows[:max(1, min(limit, 100))]}


# ── IWE backlinks (knowledge-graph: "what links here") ─────────────────────
# IWE parses the vault's [[wikilinks]] into a reference graph. The CLI rebuilds
# the whole graph per call, so we run ONE no-query pass, invert forward-references
# into a {target: [referrers]} map, and cache it with a TTL. Per-page lookups are
# then instant dict hits. Read-only (no .iwe writes), so it works on the :ro mount.
IWE_BIN = os.environ.get("IWE_BIN", "iwe")
_BACKLINKS: dict = {"map": None, "ts": 0.0}
_BACKLINKS_TTL = 3600  # seconds — the graph build is heavy; backlinks change slowly.
_BL_LOCK = threading.Lock()

# Generated root artifacts _skip() doesn't catch (they live at the vault root, not as
# INDEX*/underscore files): the agent's HOT set and the write log. Excluded as backlink
# SOURCES so their markdown links don't pollute "what links here".
_RESERVED_BL_NAMES = frozenset({"HOT.md", "log.md"})


def _skip_backlink_src(key: str) -> bool:
    """True if a backlink *source* document is generated/operational machinery that must
    not contribute "what links here" edges. IWE indexes the whole vault and doesn't apply
    our filters, so we re-apply the same exclusions the rail uses: `_skip()` (INDEX*/
    `_*`/backups), an `exclude:`-ed namespace (e.g. operational/), and the root artifacts
    HOT.md/log.md. Keeps briefings/dashboards — those are real references."""
    name = key.split("/")[-1]
    if not name.endswith(".md"):
        name += ".md"
    if _skip(name) or name in _RESERVED_BL_NAMES:
        return True
    ns = key.split("/")[0] if "/" in key else ""
    return bool(ns) and ns in _excluded_dirs()


def _backlink_title(src: str) -> str:
    """Human label for a backlink source: its frontmatter `title`/`name`, else the
    a true `# H1`, else the de-slugged basename. Deliberately NOT IWE's title (the
    page's *first heading of any level*) — that makes every source page show its
    `## Summary` heading and every entity show its raw path, useless in a "what
    links here" list. Frontmatter `name` is the curated page name (e.g. 'Andariel',
    'FireEye Operation Saffron Rose 2013'); the H1 fallback recovers the real title
    of a page that has one but no `name` (e.g. a freshly-ingested source whose
    article headline is its `# H1`). `_H1_RE` matches `# ` only, so a section
    heading like `## Summary` is never picked up."""
    try:
        fm, body = split_fm(_read_head(WIKI / f"{src}.md"))
        t = str(fm.get("title") or fm.get("name") or "").strip()
        if t:
            return t
        h1 = _H1_RE.search(body)
        if h1:
            return h1.group(0).lstrip("# ").strip()
    except OSError:
        pass
    return src.split("/")[-1].replace("-", " ").strip() or src


def _build_backlinks() -> dict:
    cmd = [IWE_BIN, "find", "-f", "json", "-l", "0"]
    try:
        # The full-graph JSON dump over a large vault can take minutes on a couple
        # of cores; generous ceiling so it isn't truncated. Runs in the startup
        # prewarm thread, so this latency is off the request path.
        proc = subprocess.run(cmd, cwd=str(WIKI), capture_output=True,
                              text=True, timeout=420)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        docs = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    bl: dict[str, list] = {}
    for d in docs:
        src = d.get("key")
        if not src or _skip_backlink_src(src):
            continue
        title = _backlink_title(src)
        for ref in d.get("references") or []:
            tgt = ref.get("key")
            if not tgt or tgt == src:
                continue
            bl.setdefault(tgt, []).append({"key": src, "title": title})
    for tgt, lst in bl.items():
        seen, uniq = set(), []
        for r in lst:
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            uniq.append(r)
        uniq.sort(key=lambda r: r["title"].lower())
        bl[tgt] = uniq
    return bl


def _refresh_backlinks_async() -> None:
    """Kick at most one background graph rebuild (no-op if one is already running
    or the map is still fresh). Used by the request path so it never blocks."""
    if not _BL_LOCK.acquire(blocking=False):
        return                            # a build is already in progress
    try:
        stale = (_BACKLINKS["map"] is None
                 or time.monotonic() - _BACKLINKS["ts"] > _BACKLINKS_TTL)
    finally:
        _BL_LOCK.release()
    if stale:
        threading.Thread(target=lambda: _load_backlinks(blocking=True), daemon=True).start()


def _load_backlinks(blocking: bool = True) -> dict:
    now = time.monotonic()
    if _BACKLINKS["map"] is not None and now - _BACKLINKS["ts"] <= _BACKLINKS_TTL:
        return _BACKLINKS["map"]
    if not blocking:
        # NEVER block a request on the heavy IWE build: refresh in the background
        # and serve the current (possibly stale / empty) map immediately.
        _refresh_backlinks_async()
        return _BACKLINKS["map"] or {}
    # Single-flight: only one thread builds the graph; others wait.
    with _BL_LOCK:
        now = time.monotonic()
        if _BACKLINKS["map"] is None or now - _BACKLINKS["ts"] > _BACKLINKS_TTL:
            m = _build_backlinks()
            # keep a stale map on transient failure rather than wiping it
            if m or _BACKLINKS["map"] is None:
                _BACKLINKS["map"] = m
                _BACKLINKS["ts"] = now
    return _BACKLINKS["map"] or {}


@app.on_event("startup")
def _prewarm_backlinks() -> None:
    """Build the backlink graph in the background at startup so the first user
    request doesn't block on the build."""
    threading.Thread(target=_load_backlinks, daemon=True).start()


@app.get("/api/backlinks")
def api_backlinks(path: str = Query(...), limit: int = 100):
    """Pages that reference `path` via the IWE wikilink graph. `path` is the
    wiki-relative key without .md (e.g. 'concepts/example-topic')."""
    key = path[:-3] if path.endswith(".md") else path
    refs = _load_backlinks(blocking=False).get(key, [])    # never block on the build
    return {"path": key, "count": len(refs),
            "backlinks": refs[:max(1, min(limit, 500))]}


# ── shell ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # Cache-bust app.js/style.css by content hash: bare URLs let the browser serve a stale
    # UI from heuristic cache after a reader update. The ?v=<hash> only changes when the
    # asset changes, so unchanged assets still cache; changed ones are fetched immediately.
    try:
        h = hashlib.sha1()
        for asset in ("style.css", "app.js"):
            p = STATIC / asset
            if p.is_file():
                h.update(p.read_bytes())
        v = h.hexdigest()[:8]
        html = (html.replace("/static/app.js", f"/static/app.js?v={v}")
                    .replace("/static/style.css", f"/static/style.css?v={v}"))
    except OSError:
        pass
    return html


@app.get("/healthz")
def healthz():
    return {"ok": True, "vault": str(WIKI), "vault_present": WIKI.is_dir()}


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
