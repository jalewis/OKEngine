#!/usr/bin/env python3
"""okengine-cockpit — standalone, read-only "intelligence cockpit" reader for an OKF vault.

A function-oriented companion to okengine-reader: instead of a generic browse rail,
it presents a 3-zone cockpit (a STREAM rail of dated briefings + function TABS:
briefings / dashboards / predictions, plus an OPTIONAL competitors / watchlist
tracker). It is DOMAIN-AGNOSTIC: every domain-specific surface (the streams, the
display title, the watchlist's tracked entity types / field names / labels, the
curated dashboard index, the competitor views) is driven by an OPTIONAL `cockpit:`
block in the pack's `<vault>/schema.yaml`. On any OKF vault with no `cockpit:`
block it falls back to generic defaults (a "Recent briefings" stream + the
briefings / predictions / dashboards tabs); the watchlist + competitors tabs stay
hidden until a `watchlist:` config lights them up.

Deliberately SEPARATE from the Hermes agent/console: imports no hermes modules,
makes no calls to the gateway or dashboard, reads `<vault>/schema.yaml` directly
(yaml only), and serves only from a READ-ONLY mount of the vault. It keeps working
even if the entire Hermes stack is down.

Env:
  VAULT_DIR   read-only vault root (default /vault); wiki at VAULT_DIR/wiki
  PORT        listen port (default 9200)
  OKENGINE_READER_PASSWORD  if set, require HTTP Basic auth (shared with the reader —
              one credential protects both UIs; see _BasicAuth). OKENGINE_READER_USER
              overrides the username (default `okengine`).
  OKENGINE_TRUST / OKENGINE_BIND  a PRIVATE vault (trust=private, the default) REFUSES to
              start when exposed off-loopback with no password — the same fail-safe the
              reader enforces (okengine#90 P4a). The cockpit is a SUPERSET of the reader
              (adds an agent-Chat relay), so it must not be laxer than the reader.
"""
from __future__ import annotations

import os
import re
import json
import glob
import base64
import hashlib
import hmac
import threading
import time
import datetime
from collections import Counter
import subprocess
import shutil
import tempfile
import urllib.request
import urllib.error
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any

import yaml
import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

VAULT = Path(os.environ.get("VAULT_DIR", "/vault"))
WIKI = VAULT / "wiki"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="OKEngine · cockpit", docs_url=None, redoc_url=None)


# ── optional HTTP Basic auth ─────────────────────────────────────────────────
# The cockpit is a SUPERSET of okengine-reader (same vault, plus an agent-Chat relay) and is
# published on the same OKENGINE_BIND, so it MUST enforce at least the reader's auth/trust guard —
# otherwise exposing the reader safely (with a password) would silently expose the richer cockpit
# unauthenticated. Ported verbatim from okengine-reader/app.py so the two surfaces stay in lockstep;
# the credential is shared (OKENGINE_READER_PASSWORD) so one setting protects both UIs.
class _BasicAuth:
    """ASGI middleware: require HTTP Basic auth when OKENGINE_READER_PASSWORD is set. `/healthz`
    stays open so container health checks don't need creds. Browser-native (the browser prompts
    and resends the header on every request), unlike a bearer token."""

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
                    (b"www-authenticate", b'Basic realm="okengine-cockpit"'),
                    (b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


_READER_PASSWORD = os.environ.get("OKENGINE_READER_PASSWORD", "")
if _READER_PASSWORD:
    app.add_middleware(_BasicAuth, user=os.environ.get("OKENGINE_READER_USER", "okengine"),
                       password=_READER_PASSWORD)

# Trust enforcement (okengine#90 P4a): a PRIVATE vault must never be served unauthenticated when
# publicly exposed. Mirrors okengine-reader exactly — default trust=private → fail-safe: refuse to
# start rather than expose a private vault to the network without a password.
_TRUST = os.environ.get("OKENGINE_TRUST", "private").strip().lower()
_BIND_HOST = os.environ.get("OKENGINE_BIND", "127.0.0.1").strip()
if _TRUST == "private" and _BIND_HOST not in ("", "127.0.0.1", "localhost", "::1") and not _READER_PASSWORD:
    raise SystemExit(
        f"okengine-cockpit REFUSED to start: PRIVATE vault exposed on {_BIND_HOST!r} with no "
        f"OKENGINE_READER_PASSWORD. Set a password, bind to loopback (OKENGINE_BIND=127.0.0.1), "
        f"or declare the pack `trust: public`. (okengine#90 P4a)")


# ── markdown / frontmatter helpers ──────────────────────────────────────────
_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_WIKILINK = re.compile(r"\[\[\s*([^\]|#\n\\]*)(?:#([^\]|\n]+))?(?:\\?\|\s*([^\]\n]+?))?\s*\]\]")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_H1_RE = re.compile(r"^#\s+.*$", re.MULTILINE)
TODAY = datetime.date.today  # callable; never cached so dates stay live

# Prediction "open" vocabulary — a cross-surface contract. config/base-schema.yaml
# `tier.namespaces.predictions.open_values: [open, active]` is the source of truth, mirrored by
# pred_lib.OPEN_VALUES (extensions/okengine.predictions) and read config-driven by tier_lib /
# build_hot_set / select_daily_brief. The cockpit is a FOURTH consumer: it must count `active`
# predictions as open too (predictions routinely carry status:active — migrated/drained sets), or
# the home 'Open predictions' section and due-soon tally silently undercount (invariant-audit M11).
# Same env override knob pred_lib uses, so a pack with a different vocabulary stays consistent.
# tests/test_cockpit_panels.py pins this set to pred_lib.OPEN_VALUES.
_OPEN_STATUS = {s.strip().lower()
                for s in os.environ.get("OKENGINE_PREDICTION_OPEN_VALUES", "open,active").split(",")
                if s.strip()}


def split_fm(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


# ── cockpit config (the ONLY place domain knowledge enters — from the pack) ──
# Generic defaults so the cockpit works zero-config on any OKF vault; a pack
# overrides via an optional `cockpit:` block in <vault>/schema.yaml. See README.
_DEFAULT_TABS = ["home", "briefings", "predictions", "dashboards"]
_TRACKER_TABS = ("watchlist", "competitors")  # require a `watchlist:` config to show

# Common initialisms a naive .title() mangles when humanizing a slug/key for display (a vault dir
# name, a watchlist tab key). GENERIC computing/universal acronyms only — domain-specific ones
# (CVE, IOC, OT/ICS, …) belong in the pack that emits the content, not the engine display layer.
# Mixed-case forms (IoT, SaaS) are spelled out.
_DISPLAY_ACRONYMS = {
    "ai": "AI", "ml": "ML", "llm": "LLM", "api": "API", "apis": "APIs", "id": "ID", "ids": "IDs",
    "url": "URL", "urls": "URLs", "uri": "URI", "ip": "IP", "dns": "DNS", "os": "OS", "ui": "UI",
    "ux": "UX", "http": "HTTP", "https": "HTTPS", "html": "HTML", "css": "CSS", "json": "JSON",
    "yaml": "YAML", "xml": "XML", "csv": "CSV", "pdf": "PDF", "sql": "SQL", "cpu": "CPU", "gpu": "GPU",
    "iot": "IoT", "saas": "SaaS", "faq": "FAQ", "kpi": "KPI", "kpis": "KPIs", "roi": "ROI",
}


def _humanize(s: str) -> str:
    """Slug/key -> display title, preserving common initialisms a naive `.title()` mangles
    (`ai-research` -> "AI Research", not "Ai Research"; `iot` -> "IoT"). Generic acronym set only —
    domain-specific acronyms live in the pack that generates the content."""
    words = re.sub(r"[-_]+", " ", str(s)).split()
    return " ".join(_DISPLAY_ACRONYMS.get(w.lower(), w.capitalize()) for w in words)


def load_cockpit_config(vault: Path) -> dict:
    """Parse the OPTIONAL `cockpit:` block from <vault>/schema.yaml into a normalized
    config with generic defaults. PURE (no caching / no globals) so it is importable
    and unit-testable. Reads schema.yaml directly — no hermes import."""
    raw: dict = {}
    sp = vault / "schema.yaml"
    if sp.is_file():
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            ck = sch.get("cockpit")
            if isinstance(ck, dict):
                raw = ck
        except Exception:
            raw = {}

    # title — default: humanized vault dir name (acronym-aware, so "ai-research" -> "AI Research")
    title = str(raw.get("title") or "").strip()
    if not title:
        title = _humanize(vault.resolve().name or "vault") or "Vault"

    # streams (the rail) — default: one "Recent briefings" stream over briefings/
    streams: list[dict] = []
    rs = raw.get("streams")
    if isinstance(rs, list):
        for i, s in enumerate(rs):
            if not isinstance(s, dict):
                continue
            d = str(s.get("dir") or "").strip().strip("/")
            if not d:
                continue
            key = str(s.get("key") or d or f"s{i}").strip()
            cfg = {"key": key,
                   "label": str(s.get("label") or key).strip() or key,
                   "dir": d,
                   "pdf": bool(s.get("pdf"))}
            if s.get("type"):
                cfg["type"] = str(s["type"]).strip()
            if s.get("glob"):
                cfg["glob"] = str(s["glob"]).strip()
            streams.append(cfg)
    if not streams:
        streams = [{"key": "briefings", "label": "Recent briefings",
                    "dir": "briefings", "pdf": False}]
    streams_by_key = {s["key"]: s for s in streams}

    # watchlist — OPTIONAL tracker tab; absent => watchlist + competitors hidden
    watchlist: dict | None = None
    rw = raw.get("watchlist")
    if isinstance(rw, dict):
        lbl = rw.get("labels") if isinstance(rw.get("labels"), dict) else {}
        ets = [str(t).strip() for t in (rw.get("entity_types") or []) if str(t).strip()]
        watchlist = {
            "entity_dir": str(rw.get("entity_dir") or "entities").strip().strip("/"),
            "entity_types": ets,                                   # empty => all types
            "tier_field": str(rw.get("tier_field") or "tier").strip(),
            "rating_field": str(rw.get("rating_field") or "").strip() or None,
            "moved_field": str(rw.get("moved_field") or "updated").strip(),
            "acquirer_field": str(rw.get("acquirer_field") or "").strip() or None,
            "labels": {
                "section": str(lbl.get("section") or "Watchlist").strip(),
                "entity": str(lbl.get("entity") or "Entity").strip(),
                "tier": str(lbl.get("tier") or "Tier").strip(),
                "rating": str(lbl.get("rating") or "Rating").strip(),
                "acquirers": str(lbl.get("acquirers") or "Acquirer candidates").strip(),
            },
        }
        # optional concept-trend sub-tracker (default ON; set `trends: false` to disable)
        rt = rw.get("trends")
        if rt is not False:
            rt = rt if isinstance(rt, dict) else {}
            watchlist["trends"] = {
                "concept_dir": str(rt.get("concept_dir") or "concepts").strip().strip("/"),
                "type": str(rt.get("type") or "trend").strip(),
            }

    # competitor dashboards rendered in the competitors tab
    comps: list[dict] = []
    for c in (raw.get("competitors") or []):
        if isinstance(c, dict) and c.get("path"):
            comps.append({"key": str(c.get("key") or c["path"]).strip(),
                          "path": str(c["path"]).strip().strip("/")})

    # predictions source dirs (default: predictions/)
    pdirs = [str(d).strip().strip("/") for d in (raw.get("predictions") or []) if str(d).strip()]
    if not pdirs:
        pdirs = ["predictions"]

    # tabs — default [briefings, predictions, dashboards]; the tracker tabs
    # (watchlist/competitors) are dropped unless a watchlist config exists.
    tabs = [str(t).strip() for t in (raw.get("tabs") or []) if str(t).strip()]
    if not tabs:
        tabs = list(_DEFAULT_TABS)
    if watchlist is None:
        tabs = [t for t in tabs if t not in _TRACKER_TABS]

    # dashboards grid (optional curated reading order); None => auto-list dashboards/
    dashboards = raw.get("dashboards") if isinstance(raw.get("dashboards"), list) else None

    # declarative dataset tabs — the pack defines a tab as a set of DATASET BOXES over the
    # vault (each box = one dataset, one view). The engine ships the renderer; the pack
    # supplies the policy (which datasets, which fields, which labels). A key in `tabs`
    # that matches a tab_defs entry renders through /api/tab/<key>.
    tab_defs: dict[str, dict] = {}
    td = raw.get("tab_defs")
    if isinstance(td, dict):
        for k, v in td.items():
            if isinstance(v, dict) and isinstance(v.get("boxes"), list):
                tab_defs[str(k).strip()] = {
                    "label": str(v.get("label") or _humanize(k)).strip(),
                    "boxes": [b for b in v["boxes"] if isinstance(b, dict)],
                }

    # per-type fact-panel field ORDER (okengine — type-aware profile). A pack declares
    # `profiles: {<type>: [field, field, …]}`; the page overlay then renders that type's fact panel
    # in this order (declared fields first, the rest in frontmatter order) so an actor/vuln/… page
    # reads as a curated profile, not raw frontmatter. Domain-agnostic: the engine ships the ordering
    # mechanism, the pack supplies the field priority.
    profiles: dict[str, list] = {}
    rp = raw.get("profiles")
    if isinstance(rp, dict):
        for t, order in rp.items():
            if isinstance(order, list):
                fields = [str(f).strip() for f in order if str(f).strip()]
                if fields:
                    profiles[str(t).strip()] = fields

    return {
        "title": title,
        "streams": streams,
        "streams_by_key": streams_by_key,
        "watchlist": watchlist,
        "competitors": comps,
        "predictions_dirs": pdirs,
        "tabs": tabs,
        "dashboards": dashboards,
        "tab_defs": tab_defs,
        "profiles": profiles,
    }


_CFG_CACHE: tuple[float, dict | None] = (float("-inf"), None)
_CFG_TTL = 120.0  # vault is :ro and cron-refreshed; brief config staleness is fine


def cockpit_config() -> dict:
    """Cached cockpit config for the request path."""
    global _CFG_CACHE
    now = time.monotonic()
    if _CFG_CACHE[1] is not None and now - _CFG_CACHE[0] < _CFG_TTL:
        return _CFG_CACHE[1]
    cfg = load_cockpit_config(VAULT)
    _CFG_CACHE = (now, cfg)
    return cfg


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
    """Render a wikilink as plain display text (this reader has no entity pages)."""
    return _WIKILINK.sub(_wl_display, s)


# `[APT41](entities/a/apt41)` — an INTERNAL vault link (the agent's linked-title citations). Not an
# image (`!` excluded), not external (http/mailto/# excluded).
_MD_LOCAL_LINK = re.compile(r"(?<!\!)\[([^\]\n]+)\]\((?!https?://|mailto:|#)[^)\n]*\)")


def _deref_local_links(s: str) -> str:
    """Flatten internal markdown links to their text for portable export — `[APT41](entities/a/apt41)`
    -> `APT41`. External http(s)/mailto links are kept. Vault paths resolve only inside the reader,
    so an exported md/docx/pdf must not carry them as dead links."""
    return _MD_LOCAL_LINK.sub(r"\1", s)


_EMBED = re.compile(r"!\[\[\s*([^\]\n#|]+?)\s*(?:#[^\]\n|]+)?(?:\|[^\]\n]+)?\s*\]\]")
# generic OKF/Obsidian namespaces an embed/page basename might resolve under; pure
# resolution fallback (no domain knowledge — just common wiki folder names).
_EMBED_DIRS = ("operational", "dashboards", "marketing", "dailies", "briefings",
               "predictions", "reports")
_EMBED_PATH_CACHE: dict = {}


def _embed_rglob(name: str) -> "Path | None":
    """First match for basename `name` under the generic embed dirs, memoized for the process
    lifetime. Basename embeds are the norm on sharded OKF vaults, so without this each render
    re-walks every `_EMBED_DIRS` subtree once per unresolved embed."""
    if name in _EMBED_PATH_CACHE:
        return _EMBED_PATH_CACHE[name]
    hit = None
    for d in _EMBED_DIRS:
        base = WIKI / d
        hits = list(base.rglob(name)) if base.is_dir() else []
        if hits:
            hit = hits[0]
            break
    _EMBED_PATH_CACHE[name] = hit
    return hit


def _resolve_embeds(text: str, depth: int = 0) -> str:
    """Inline Obsidian embeds ![[target]] with the target file's body, recursively
    (depth-limited). The vault's `latest-*` dashboards are one-line embed pointers;
    without this they render as a raw `!target` reference instead of the content."""
    if depth > 3:
        return text

    def repl(mo: "re.Match") -> str:
        target = mo.group(1).strip()
        if re.search(r"\.(png|jpe?g|gif|svg|webp|pdf)$", target, re.I):
            return f"_[embedded asset: {target}]_"
        cand = WIKI / (target + ".md")
        if not cand.is_file():
            cand = _embed_rglob(Path(target).name + ".md")
        if not cand:
            return f"_[missing embed: {target}]_"
        try:
            cp = cand.resolve()
            if not str(cp).startswith(str(WIKI.resolve())):
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
    # drop dangling "[[" from truncated wikilinks (source cells cut with …/(+N))
    return re.sub(r"\[\[(?![^\]\n]*\]\])", "", s)


def _strip_md(s: str) -> str:
    """Claim text as clean plain prose (for search + truncated cells)."""
    s = _delink(s or "")
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)   # [text](url) -> text
    return s.replace("**", "").replace("__", "").replace("`", "").strip()


def _inline_md(s: str) -> str:
    """Render claim INLINE: bold/code/links + clickable [[wikilinks]]. Trusted vault
    content, but HTML-escaped before adding our own tags."""
    s = (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _linkify(s)                                                       # [[wl]] -> <a class=wl>
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


_SRC_LINK = re.compile(r'<a class="wl" data-page="(sources/[^"]+)">([^<]*)</a>')


def _link_originals(html: str) -> str:
    """A cited source page carries the ORIGINAL article's `url:` in its frontmatter. Promote the
    citation so its TITLE links STRAIGHT to that article: swap the slug text for the page's real
    title and point it at the original reporting — the analyst reaches the primary source in one
    click, instead of the title pointing at the internal source stub with the real url demoted to
    a small glyph. Falls back to the internal wikilink only when the source has no http(s) url."""
    def _enrich(m):
        rel, text = m.group(1), m.group(2)
        title, url = "", ""
        try:
            fm, _b = split_fm(safe_read(WIKI, rel + ".md"))
            title = str(fm.get("title") or fm.get("name") or "").strip()
            url = str(fm.get("url") or "").strip()
        except Exception:
            pass
        label = _esc(title or text)
        if url.startswith(("http://", "https://")):
            return (f'<a class="ext" href="{_esc(url)}" target="_blank" rel="noopener noreferrer"'
                    f' title="original article">{label}</a>')
        return f'<a class="wl" data-page="{_esc(rel)}">{label}</a>'
    return _SRC_LINK.sub(_enrich, html)


# Agents across lanes "highlight" a wikilink by wrapping it in backticks (`[[x]]`). That makes
# _linkify inject the <a> INSIDE an inline-code span, so markdown escapes it to visible `<a …>` text
# in the UI. Strip the backticks around a bare wikilink first — the author meant a link, not code.
_UNCODE_WIKILINK = re.compile(r"`(\[\[[^`]+?\]\])`")


def _uncode_wikilinks(s: str) -> str:
    return _UNCODE_WIKILINK.sub(r"\1", s)


def render_md(body: str) -> str:
    body = _resolve_embeds(body)
    body = re.sub(r"```dataview(js)?\n.*?\n```",
                  "_[Dataview view — open in Obsidian to compute]_", body, flags=re.DOTALL)
    body = _uncode_wikilinks(body)
    body = _linkify(body)
    return _link_originals(md.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "nl2br"]))


def safe_read(base: Path, rel: str) -> str:
    """Read a file strictly under `base` (path-traversal guard). Read-only."""
    p = (base / rel).resolve()
    if not str(p).startswith(str(base.resolve())) or not p.is_file():
        raise HTTPException(404, "not found")
    return p.read_text(encoding="utf-8", errors="replace")


def _file_date(name: str) -> str | None:
    m = _DATE_RE.search(name)
    return m.group(1) if m else None


# ── config surface (frontend reads title + which tabs to show) ──────────────
@app.get("/api/config")
def api_config():
    cfg = cockpit_config()
    # Ops is an engine-level operational surface (health/audit pages every OKF vault produces),
    # so it's auto-appended when that content exists — no per-pack schema authoring needed. A pack
    # that lists "ops" in its own `tabs:` controls its position; otherwise it trails the nav.
    tabs = list(cfg["tabs"])
    # Ops is the engine-level operational surface, auto-appended when that content exists — inserted
    # BEFORE `browse` so browse stays at the tail next to Chat (the pack lists browse last).
    if "ops" not in tabs and _ops_available():
        if "browse" in tabs:
            tabs.insert(tabs.index("browse"), "ops")
        else:
            tabs.append("ops")
    return {"title": cfg["title"], "tabs": tabs,
            "watchlist": cfg["watchlist"] is not None,
            # labels for pack-defined dataset tabs (the frontend builds their panes dynamically)
            "tab_labels": {k: v["label"] for k, v in (cfg.get("tab_defs") or {}).items()},
            "chat_enabled": _chat_enabled()}      # gate the Chat tab on a configured agent


# ── briefings (read mode) — streams are pack-config-driven ──────────────────
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
            if not _visible_page(p, base):   # segment-level (drops _archived/ at ANY depth), not leaf-only
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


@app.get("/api/streams")
def api_streams():
    streams = []
    for key, cfg in _streams().items():
        dates = _stream_dates(key)
        streams.append({
            "key": key, "label": cfg["label"], "dates": dates,
            "latest": dates[0] if dates else None,
            "has_pdf": bool(cfg.get("pdf")),
        })
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


@app.get("/api/doc")
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
        "stream": stream, "date": date, "title": title,
        "generated_at": fm.get("generated_at") or fm.get("updated") or fm.get("created"),
        "html": render_md(body),
    }


_MARP = shutil.which("marp")
# The vault is mounted read-only, so on-demand deck renders cache under a writable dir, keyed by the
# source .md's mtime (a regenerated deck re-renders; stale renders are pruned).
_DECK_CACHE = Path(os.environ.get("OKENGINE_DECK_CACHE", "/tmp/okengine-deck-cache"))


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
        for stale in _DECK_CACHE.glob(f"{md.stem}.*.pdf"):   # drop renders of an older md version
            stale.unlink(missing_ok=True)
        # marp/puppeteer write intermediate files under HOME/TMPDIR/XDG_*; the container often runs as
        # a home-less vault uid, so point them all at the writable cache or the render EACCES-fails.
        env = {**os.environ, "HOME": str(_DECK_CACHE), "TMPDIR": str(_DECK_CACHE),
               "XDG_CACHE_HOME": str(_DECK_CACHE), "XDG_CONFIG_HOME": str(_DECK_CACHE)}
        subprocess.run([_MARP, str(md), "--pdf", "--allow-local-files", "-o", str(out)],
                       check=True, capture_output=True, timeout=120, cwd=str(_DECK_CACHE), env=env)
        return out if (out.is_file() and out.stat().st_size > 0) else None
    except Exception:
        return None


@app.get("/api/stream.pdf")
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
                return FileResponse(pdf, media_type="application/pdf")   # pre-rendered in the vault
            rendered = _render_deck_pdf(Path(p))                          # else render the marp md
            if rendered:
                return FileResponse(rendered, media_type="application/pdf")
            break
    raise HTTPException(404, "deck pdf not found")


# ── predictions (track mode) ────────────────────────────────────────────────
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


# evidence entries arrive as dicts (`{date, direction, note, source, confidence_*}`) OR as
# `[YYYY-MM-DD tag] free text` strings (the regrade lanes stamp this compact form). Parse both
# into one render-ready shape so the ledger tally and the detail drilldown agree.
_EV_PREFIX_RE = re.compile(r"^\s*\[(\d{4}-\d{2}-\d{2})(?:\s+([^\]]+?))?\]\s*(.*)$", re.S)
_EV_DIR_SYNONYM = {
    "reinforces": "reinforces", "reinforce": "reinforces", "supports": "reinforces",
    "support": "reinforces", "confirms": "reinforces", "confirm": "reinforces", "up": "reinforces",
    "contradicts": "contradicts", "contradict": "contradicts", "refutes": "contradicts",
    "refute": "contradicts", "weakens": "contradicts", "down": "contradicts",
    "partial": "partial", "mixed": "partial",
    "neutral": "neutral", "regrade": "neutral", "note": "neutral", "context": "neutral",
}


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
            note = str(e.get("note") or e.get("text") or e.get("summary") or e.get("detail") or "").strip()
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
        out.append({
            "date": date,
            "direction": _EV_DIR_SYNONYM.get(raw) if raw else None,
            "tag": raw or None,
            "note": note,
            "source": str(src).strip() if src else None,
            "confidence_before": cb,
            "confidence_after": ca,
        })
    out.sort(key=lambda r: r.get("date") or "")
    return out


def _conf(fm: dict) -> float | None:
    c = fm.get("confidence")
    if isinstance(c, (int, float)):
        return round(float(c), 3)
    # qualitative alt-schema confidence
    qual = {"low": 0.3, "medium": 0.5, "medium-high": 0.65, "high": 0.8}
    if isinstance(c, str):
        return qual.get(c.strip().lower())
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
        files += [f for f in glob.glob(str(WIKI / sub / "**" / "*.md"), recursive=True)
                  if not _reserved_seg(Path(f))]   # skip predictions/_archive/… retired forecasts
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
        if str(fm.get("type") or "").strip() != "prediction":
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
        idle = status in _OPEN_STATUS and not ev_entries and made is not None and (today - made).days > 60
        rows.append({
            "id": Path(p).stem,
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
        })
    return rows


@app.get("/api/predictions")
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
    return {"total": len(rows), "summary": summary, "due_soon": due_soon,
            "idle": idle, "forecast_sets": sorted(fsets), "rows": rows}


@app.get("/api/prediction")
def api_prediction(id: str = Query(...)):
    if "/" in id or ".." in id:
        raise HTTPException(400, "bad id")
    for sub in cockpit_config()["predictions_dirs"]:
        # Rows are discovered RECURSIVELY (predictions/YYYY/qN/predict-*.md), so resolve the detail
        # the same way — a flat WIKI/<sub>/{id}.md missed every partitioned prediction (the row
        # appeared in the ledger, then 404'd on click). id is slash-free (validated above).
        for hp in sorted(glob.glob(str(WIKI / sub / "**" / f"{id}.md"), recursive=True)):
            p = Path(hp)
            if not p.is_file():
                continue
            fm, body = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            return {
                "id": id, "fm": {k: str(v) for k, v in fm.items() if k != "evidence"},
                "trajectory": _trajectory(fm),
                "evidence": _evidence_entries(fm),
                "claim": _strip_md(_claim(fm, body)),
                "claim_html": _inline_md(_claim(fm, body)),
                "html": render_md(body),
            }
    raise HTTPException(404, "prediction not found")


# ── frontmatter table helpers ───────────────────────────────────────────────
def _esc(s: Any) -> str:
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


_DIR_CACHE: dict[str, tuple[float, list[dict]]] = {}
_DIR_TTL = 120.0  # seconds; vault is :ro and refreshed by cron, brief staleness is fine
_DIR_LOCK = threading.Lock()
_DIR_REFRESHING: set[str] = set()


def _scan_dir_meta(sub: str) -> list[dict]:
    """Parse frontmatter of every page under wiki/<sub> (recursive, like Dataview FROM). The raw
    scan: on a 3k–6k-file namespace this is seconds of syscalls + YAML parses, so it must never run
    synchronously on a request — _load_dir keeps it behind the cache + a background refresh.
    (Distinct from the browse-layer _scan_dir below, which returns a lighter list-row shape.)"""
    out: list[dict] = []
    base = WIKI / sub
    if base.is_dir():
        for p in base.rglob("*.md"):
            name = p.name
            if name.startswith(("_", ".")) or ".bak." in name or _reserved_seg(p):
                continue
            try:
                fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            fm["_name"] = p.stem
            fm["_sub"] = sub
            # the TRUE path under wiki/<sub> (shards included) — page links must carry it:
            # basename resolution papers over it until two shards collide on a stem
            fm["_rel"] = p.relative_to(base).as_posix()[:-3]
            out.append(fm)
    return out


def _refresh_dir_async(sub: str) -> None:
    """Rescan wiki/<sub> in a daemon thread and swap the cache. In-flight guard so N concurrent
    requests to a stale dir spawn ONE rescan, not N."""
    with _DIR_LOCK:
        if sub in _DIR_REFRESHING:
            return
        _DIR_REFRESHING.add(sub)

    def _work():
        try:
            rows = _scan_dir_meta(sub)
            _DIR_CACHE[sub] = (time.monotonic(), rows)
        finally:
            with _DIR_LOCK:
                _DIR_REFRESHING.discard(sub)

    threading.Thread(target=_work, daemon=True).start()


def _load_dir(sub: str) -> list[dict]:
    """Frontmatter of every page under wiki/<sub>, cached, STALE-WHILE-REVALIDATE. A scan of a large
    namespace (entities/sources are thousands of pages) costs seconds; sorting a top-N table needs
    every row, so the scan is inherent. Keep it off the hot path: within _DIR_TTL serve the cache;
    once stale serve the stale copy immediately AND rescan in the background; only a cold miss (no
    cache at all) scans synchronously — and the startup warmer pre-populates the configured datasets
    so even the first request is warm. The vault is :ro and cron-refreshed, so bounded staleness is
    already the contract (previously every request that fell past the TTL blocked on the full scan)."""
    now = time.monotonic()
    hit = _DIR_CACHE.get(sub)
    if hit is not None:
        if now - hit[0] >= _DIR_TTL:
            _refresh_dir_async(sub)          # stale: refresh in the background, serve stale now
        return hit[1]
    rows = _scan_dir_meta(sub)               # cold miss (first ever load of this dir)
    _DIR_CACHE[sub] = (now, rows)
    return rows


def _warm_tab_datasets() -> None:
    """Pre-scan the namespaces the configured tabs/streams read, so the FIRST overview/tab request
    doesn't eat a cold multi-thousand-file scan (the 'overview slow to load' report)."""
    subs: set[str] = set()
    cfg = cockpit_config()
    for d in (cfg.get("tab_defs") or {}).values():
        for b in (d.get("boxes") or []):
            ds = b.get("dataset") or {}
            if ds.get("dir"):
                subs.add(str(ds["dir"]))
            if b.get("dir"):
                subs.add(str(b["dir"]))
    for s in cfg.get("streams") or []:
        if isinstance(s, dict) and s.get("dir"):
            subs.add(str(s["dir"]))
    for sub in subs:
        try:
            _DIR_CACHE[sub] = (time.monotonic(), _scan_dir_meta(sub))
        except Exception:
            pass


def _disp(fm: dict) -> str:
    return str(fm.get("title") or fm.get("name") or fm.get("_name") or "").strip()


def _page_link(fm: dict) -> str:
    rel = fm.get("_rel") or fm["_name"]          # true sharded path when _load_dir provided it
    return f'<a class="wl" data-page="{_esc(fm["_sub"])}/{_esc(rel)}">{_esc(_disp(fm))}</a>'


# A cell that is a bare date (YYYY-MM-DD), a number/percentage, or the em-dash placeholder is a
# structured value that must never wrap or break across lines — dates like `2026-09-30` were breaking
# mid-token when a long first column squeezed the table. Tag those cells `.num` (nowrap + right-align).
# Matches only PLAIN values, so a cell holding HTML (a page link, a chip) stays a normal wrapping cell.
_NUMISH_CELL = re.compile(r'\d{4}-\d{2}-\d{2}|-?\d[\d.,]*%?|[—-]')


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="empty" style="padding:10px;text-align:left">none</div>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    def _cell(c: str) -> str:
        cls = ' class="num"' if _NUMISH_CELL.fullmatch(str(c).strip()) else ''
        return f'<td{cls}>{c}</td>'
    body = "".join("<tr>" + "".join(_cell(c) for c in r) + "</tr>" for r in rows)
    return f'<table class="ledger"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "y", "1"))


# ── watchlist & trends (OPTIONAL tracker — pack-config-driven, no domain literals) ──
@app.get("/api/watchlist")
def api_watchlist():
    wl = cockpit_config()["watchlist"]
    if not wl:                                   # no watchlist config -> tab is hidden
        return {"sections": [], "counts": {}}
    today = TODAY()
    L = wl["labels"]
    tier_f, rate_f, moved_f = wl["tier_field"], wl["rating_field"], wl["moved_field"]
    sections: list[dict] = []

    ents = _load_dir(wl["entity_dir"])
    if wl["entity_types"]:
        ents = [e for e in ents if str(e.get("type") or "").strip() in wl["entity_types"]]
    tiered = [e for e in ents if e.get(tier_f)]

    # rating × tier matrix (only when a rating field is configured)
    if rate_f:
        matrix: dict[str, dict[str, int]] = {}
        for e in tiered:
            tier = str(e.get(tier_f)).strip()
            r = str(e.get(rate_f) or "").strip().lower()
            b = matrix.setdefault(tier, {"high": 0, "medium": 0, "low": 0, "total": 0})
            if r in b:
                b[r] += 1
            b["total"] += 1
        mrows = [[_esc(t), str(b["high"]), str(b["medium"]), str(b["low"]), str(b["total"])]
                 for t, b in sorted(matrix.items())]
        sections.append({"group": L["section"],
                         "title": f"{L['rating']} matrix by {L['tier'].lower()}",
                         "html": _html_table([L["tier"], "High", "Medium", "Low", "Total"], mrows)})

    def _move_row(e):
        mv = _as_date(e.get(moved_f))
        days = (today - mv).days if mv else None
        cells = [_page_link(e), _esc(e.get(tier_f))]
        if rate_f:
            cells.append(_esc(e.get(rate_f)))
        cells += [_esc(mv.isoformat() if mv else "—"),
                  (str(days) + "d" if days is not None else "—")]
        return (cells, mv, days)

    rate_hdr = [L["rating"]] if rate_f else []
    moved = [_move_row(e) for e in tiered]
    recent = sorted([r for r in moved if r[2] is not None and r[2] <= 30],
                    key=lambda r: r[1], reverse=True)[:25]
    sections.append({"group": L["section"], "title": "Recently moved (≤30d)",
                     "html": _html_table([L["entity"], L["tier"], *rate_hdr, "Last move", "Days ago"],
                                         [r[0] for r in recent])})
    quiet = sorted([r for r in moved if r[2] is not None and r[2] > 60], key=lambda r: r[1])[:15]
    sections.append({"group": L["section"], "title": "Gone quiet (>60d)",
                     "html": _html_table([L["entity"], L["tier"], *rate_hdr, "Last move", "Days quiet"],
                                         [r[0] for r in quiet])})

    if wl["acquirer_field"]:
        af = wl["acquirer_field"]
        acq = sorted([e for e in ents if _truthy(e.get(af))],
                     key=lambda e: str(_as_date(e.get(moved_f)) or ""), reverse=True)
        arows = []
        for e in acq:
            row = [_page_link(e), _esc(e.get(tier_f))]
            if rate_f:
                row.append(_esc(e.get(rate_f)))
            row.append(_esc(_as_date(e.get(moved_f)) or "—"))
            arows.append(row)
        sections.append({"group": L["section"], "title": L["acquirers"],
                         "html": _html_table([L["acquirers"], L["tier"], *rate_hdr, "Last move"], arows)})

    counts = {"tracked": len(tiered)}

    # --- trends (concepts of a configured type) ---
    tr = wl.get("trends")
    if tr:
        trends = [c for c in _load_dir(tr["concept_dir"])
                  if str(c.get("type") or "").strip() == tr["type"]]

        def _trend_row(c):
            anc = c.get("anchored_predictions")
            anc_n = len(anc) if isinstance(anc, list) else 0
            upd = _as_date(c.get("last_thesis_update"))
            thesis = str(c.get("thesis") or "").strip()
            return [_page_link(c), _esc(c.get("trend_status")), _esc(c.get("thesis_confidence")),
                    _esc(thesis[:120] + ("…" if len(thesis) > 120 else "")), str(anc_n),
                    _esc(upd.isoformat() if upd else "—")], upd

        thead = ["Trend", "Status", "Conf", "Thesis", "Anchored", "Updated"]
        closed_statuses = ("reversed", "dormant")
        active = [c for c in trends if c.get("trend_status")
                  and str(c.get("trend_status")).strip().lower() not in closed_statuses]
        active_rows = sorted([_trend_row(c) for c in active],
                             key=lambda r: str(r[0][2]), reverse=True)
        sections.append({"group": "Trends", "title": "Active trends",
                         "html": _html_table(thead, [r[0] for r in active_rows])})
        recent_t = sorted([_trend_row(c) for c in trends
                           if (lambda d: d is not None and (today - d).days <= 30)(_as_date(c.get("last_thesis_update")))],
                          key=lambda r: r[1] or datetime.date.min, reverse=True)
        sections.append({"group": "Trends", "title": "Recently updated (≤30d)",
                         "html": _html_table(thead, [r[0] for r in recent_t])})
        closed = [c for c in trends if str(c.get("trend_status") or "").strip().lower() in closed_statuses]
        sections.append({"group": "Trends", "title": "Closed (reversed / dormant)",
                         "html": _html_table(thead, [_trend_row(c)[0] for c in closed])})
        nostatus = [c for c in trends if not c.get("trend_status")]
        sections.append({"group": "Trends", "title": "Needs status (no trend_status)",
                         "html": _html_table(thead, [_trend_row(c)[0] for c in nostatus])})
        counts["trends"] = len(trends)

    return {"sections": sections, "counts": counts}


# ── competitors (track mode — render pack-configured generated dashboards) ───
@app.get("/api/competitors")
def api_competitors():
    out = []
    for view in cockpit_config()["competitors"]:
        rel = view["path"]
        rel = rel if rel.endswith(".md") else rel + ".md"
        p = (WIKI / rel)
        if not p.is_file():
            continue
        fm, body = split_fm(p.read_text(encoding="utf-8", errors="replace"))
        out.append({"key": view["key"], "title": fm.get("title") or view["key"],
                    "updated": str(fm.get("updated") or "")[:10], "html": render_md(body)})
    return {"views": out}


# ── dashboards grid (curated reading order, else auto-listed) ────────────────
@app.get("/api/home")
def api_home():
    """Analyst home — the daily flow composed from the vault's LIVE surfaces, in triage order:
    latest briefs → what moved (watchlist) → trends → open predictions → knowledge gaps →
    curated dashboards. Sections use the watchlist tab's render contract ({group,title,html});
    an empty/unconfigured surface is OMITTED, so the tab shows only what this deployment
    actually maintains (a raw all-page-links dashboard is a map; this is the route)."""
    cfg = cockpit_config()
    sections: list[dict] = []

    # 1. start here — latest issue of every stream (click → the briefings tab)
    srows = []
    for s in cfg["streams"]:
        dates = _stream_dates(s["key"])
        if dates:
            srows.append([f'<a class="wl" data-stream="{_esc(s["key"])}">{_esc(s["label"])}</a>',
                          _esc(dates[0]), str(len(dates))])
    if srows:
        sections.append({"group": "Start here", "title": "Latest briefings & digests",
                         "html": _html_table(["Stream", "Latest", "Issues"], srows)})

    # 2. what moved — cherry-picked from the watchlist (matrix + movement + active trends)
    if cfg.get("watchlist"):
        keep = ("matrix", "recently moved", "recently updated", "active")
        for s in api_watchlist().get("sections", []):
            t = s["title"].lower()
            if any(k in t for k in keep) and not s["html"].startswith('<div class="empty"'):
                sections.append({"group": "What moved", "title": f"{s['group']} — {s['title']}",
                                 "html": s["html"]})

    # 3. open predictions (top by nearest resolution)
    pr = api_predictions()
    if pr["total"]:
        top = sorted([r for r in pr["rows"] if r["status"] in _OPEN_STATUS],
                     key=lambda r: r["resolves_by"] or "9999")[:8]
        prows = [[f'<a class="wl" data-page="predictions/{_esc(r["id"])}">{_esc(r["subject"] or r["id"])}</a>',
                  _esc(str(r["confidence"] or "—")), _esc(r["resolves_by"] or "—")] for r in top]
        html = (f'<p class="home-note">{pr["total"]} tracked · {pr["due_soon"]} due ≤7d · '
                f'{pr["idle"]} idle</p>' + _html_table(["Prediction", "Conf", "Resolves by"], prows))
        sections.append({"group": "Predictions", "title": "Open predictions", "html": html})

    # 4. knowledge gaps — latest lacuna findings, when the extension maintains that namespace
    gaps = _load_dir("lacuna")
    if gaps:
        recent = sorted(gaps, key=lambda c: str(c.get("last_updated") or c.get("created") or ""),
                        reverse=True)[:8]
        grows = [[_page_link(c), _esc(str(c.get("created") or "")[:10] or "—")] for c in recent]
        sections.append({"group": "Knowledge gaps", "title": "Latest lacuna findings",
                         "html": _html_table(["Gap", "Found"], grows)})

    # 5. jump-offs — the pack's CURATED dashboards (not the raw all-pages list). Two config
    # shapes exist: a flat slug list (["top-actors", ...] → dashboards/<slug>) and the grouped
    # form ([{group, items: [{path, title?}]}] — paths already namespace-qualified).
    chips: list[tuple[str, str]] = []            # (page path, label)
    for d in cfg.get("dashboards") or []:
        if isinstance(d, dict):
            for it in d.get("items") or []:
                path = str((it or {}).get("path") or "").strip().strip("/")
                if path:
                    chips.append((path, str(it.get("title") or path.split("/")[-1].replace("-", " "))))
        else:
            slug = str(d).strip().strip("/")
            if slug:
                chips.append((f"dashboards/{slug}", slug.replace("-", " ")))
    if chips:
        links = "".join(f'<a class="wl home-chip" data-page="{_esc(p)}">{_esc(lbl)}</a>'
                        for p, lbl in chips)
        sections.append({"group": "Jump off", "title": "Curated dashboards",
                         "html": f'<div class="home-chips">{links}</div>'})

    return {"sections": sections}


# ── declarative dataset tabs ─────────────────────────────────────────────────
# The pack's cockpit config can define whole tabs as DATASET BOXES: each box names a
# dataset (a dir + optional type/where filters) and a view (table / bars / chips /
# bignums / cards / coverage / doc). The engine renders; the pack decides which datasets
# an analyst sees and how they're labeled — including value maps for opaque codes
# (e.g. NAICS sector numbers). A box whose dataset is EMPTY renders its `empty:` note
# when one is configured (pipeline state is information), otherwise it is omitted —
# never a wall of "none" placeholders. Design source: the okcti data-first redesign.

_TONES = ("crit", "warn", "ok", "info", "mut", "acc")

# Inline marker for a group_by value the pack's `labels:` map doesn't cover — the label falls
# back to the raw code, so flag it as degraded rather than let an opaque code masquerade as a
# curated label (okengine#188).
_UM_FLAG = (' <span class="um-flag" title="unmapped value — no label configured '
            '(okengine#188)">⚠</span>')


def _ds_rows(spec: dict) -> list[dict]:
    rows = _load_dir(str(spec.get("dir") or "").strip("/"))
    types = spec.get("types") or ([spec["type"]] if spec.get("type") else [])
    if types:
        ts = {str(t) for t in types}
        rows = [r for r in rows if str(r.get("type")) in ts]
    for f, v in (spec.get("where") or {}).items():
        rows = [r for r in rows if str(r.get(f)) == str(v)]
    for f in (spec.get("has") or []):        # field-presence filter (e.g. theme pages vs
        rows = [r for r in rows if r.get(f) not in (None, "", [])]   # shift docs in one dir)
    for f in (spec.get("missing") or []):    # field-ABSENCE filter (mirror of `has`) — e.g. unsourced actors
        rows = [r for r in rows if r.get(f) in (None, "", [])]
    tp = spec.get("today_prefix")            # e.g. published starts with today's date
    if tp:
        today = datetime.date.today().isoformat()
        rows = [r for r in rows if str(r.get(tp) or "").startswith(today)]
    return rows


def _ds_sorted(rows: list[dict], srt: dict) -> list[dict]:
    f = str(srt.get("field") or "")
    if not f:
        return rows
    if srt.get("require"):
        rows = [r for r in rows if r.get(f) not in (None, "", [])]

    # TWO buckets — numeric, then everything-else — each honoring the sort direction WITHIN itself.
    # Two live incidents shaped this:
    #   1. `reverse=bool(desc)` flipped the buckets too, so ONE page with a malformed value (an
    #      agent hand-set `recent_reports:` to a list of source paths) took the #1 slot of the
    #      Most-active table — junk in a NUMERIC sort must rank below every real number.
    #   2. The first fix sorted the non-numeric bucket ascending unconditionally — which broke every
    #      DATE-sorted box (ISO dates aren't floatable, so a date box lives entirely in this bucket):
    #      `sort: {field: created, desc: true}` showed OLDEST gaps first. Direction must apply
    #      within the bucket; ISO date strings/objects order correctly via str().
    desc = bool(srt.get("desc"))
    nums, others = [], []
    for r in rows:
        v = r.get(f)
        try:
            nums.append((float(v), r))
        except (TypeError, ValueError):
            others.append((str(v), r))
    nums.sort(key=lambda t: t[0], reverse=desc)
    others.sort(key=lambda t: t[0], reverse=desc)
    return [r for _, r in nums] + [r for _, r in others]


def _defang(v: str) -> str:
    return v.replace("http://", "hxxp://").replace("https://", "hxxps://").replace(".", "[.]")


def _ds_cell(r: dict, col: dict) -> str:
    if col.get("link"):
        return _page_link(r)
    v = r.get(str(col.get("field") or ""))
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v[: int(col.get("max") or 3)])
    v = "—" if v in (None, "", []) else str(v)
    if col.get("date"):
        v = v[:10]
    if col.get("defang"):                    # IOC hygiene: never render a live URL/domain
        return f"<code>{_esc(_defang(v))}</code>"
    tone = col.get("tone")
    classes = [f"t-{tone}"] if tone in _TONES else []
    # A structured single-token value — a date, a number, or an enum like "moderate-high" — must
    # never break mid-token when the column squeezes (dates broke at their hyphens, confidence
    # enums at theirs). `_html_table`'s .num heuristic can't see it here (the cell arrives as
    # ready HTML), so tag it nowrap directly. Multi-word prose (a thesis/summary column) has
    # internal whitespace and keeps its normal word-wrap.
    if col.get("date") or (v.strip() and " " not in v.strip()):
        classes.append("nw")
    cls = f' class="{" ".join(classes)}"' if classes else ""
    return f"<span{cls}>{_esc(v)}</span>"


def _drill_attrs(drill, *, value=None, item=None, page=None):
    """(class_suffix, attrs) that make an aggregate row/value navigable (okengine#189). A group_by
    bucket (value) or bignums item opens its filtered page LIST via /api/drill; a value_field bar
    (page) — already one page — opens that page directly. ('', '') when not navigable."""
    if page:                                          # value_field bar -> open the page itself
        return " drill", f' data-drill data-dpage="{_esc(page)}"'
    if not drill or (value is None and item is None):
        return "", ""
    tab, bi = drill
    sel = f' data-dval="{_esc(str(value))}"' if value is not None else f' data-ditem="{item}"'
    return " drill", f' data-drill data-dtab="{_esc(tab)}" data-dbox="{bi}"{sel}'


def _gb_values(v) -> list[str]:
    """The group_by buckets a row contributes: each element of a LIST field (so a page targeting
    ['government','finance'] counts toward both), or the single scalar. Empties dropped."""
    return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else ([str(v)] if v not in (None, "") else [])


def _ds_pairs(box: dict, rows: list[dict]) -> list:
    """(label, value, unmapped, key) tuples for bars/chips — a group_by count or explicit fields.
    `unmapped` is True only when a `labels:` map is configured but this grouped value is absent
    from it (okengine#188). `key` is the RAW group value (drives a group_by drilldown filter), or
    None for value_field pairs (which are already one page each)."""
    if box.get("group_by"):
        labels = {str(k): str(v) for k, v in (box.get("labels") or {}).items()}
        drop = {"", "None", "Unknown", "nan"}
        cnt: Counter = Counter()
        for r in rows:                        # list fields explode: each element is its own bucket
            for s in _gb_values(r.get(box["group_by"])):
                if s not in drop:
                    cnt[s] += 1
        return [(labels.get(k, k), v, bool(labels) and k not in labels, k)
                for k, v in cnt.most_common(int(box.get("limit") or 8))]
    vf = str(box.get("value_field") or "")
    lf = str(box.get("label_field") or "title")
    rs = [r for r in rows if r.get(vf) not in (None, "")]
    rs = _ds_sorted(rs, {"field": vf, "desc": True})[: int(box.get("limit") or 8)]
    # key = the bar's own page path — a value_field bar is one page, so it opens directly
    return [(str(r.get(lf) or r.get("name") or r.get("_name") or "?"), int(float(r.get(vf) or 0)),
             False, f'{r.get("_sub", "")}/{r.get("_rel") or r.get("_name", "")}'.strip("/"))
            for r in rs]


def _v_table(box: dict, rows: list[dict]) -> str:
    cols = [c for c in (box.get("columns") or []) if isinstance(c, dict)]
    rows = _ds_sorted(rows, box.get("sort") or {})[: int(box.get("limit") or 10)]
    if not rows or not cols:
        return ""
    return _html_table([str(c.get("label") or c.get("field") or "") for c in cols],
                       [[_ds_cell(r, c) for c in cols] for r in rows])


def _link_page_map(box: dict) -> dict:
    """For a group_by box with `link_page: {dir, by}`, map each bucket value -> the page path
    (<dir>/<rel>) of the page in <dir> whose <by> field equals the value. Lets an aggregate bar
    open the entity it NAMES (e.g. an ATT&CK technique id -> its technique page) instead of
    drilling to the members that share it. {} when not configured; values with no matching page
    keep the normal group_by drilldown."""
    lp = box.get("link_page")
    if not isinstance(lp, dict) or not lp.get("dir"):
        return {}
    by = str(lp.get("by") or "id")
    out: dict = {}
    for r in _load_dir(str(lp["dir"])):
        k = r.get(by)
        if k in (None, ""):
            continue
        out.setdefault(str(k), f'{r.get("_sub", "")}/{r.get("_rel") or r.get("_name", "")}'.strip("/"))
    return out


def _v_bars(box: dict, rows: list[dict], drill=None) -> str:
    pairs = _ds_pairs(box, rows)
    if not pairs:
        return ""
    mx = max(v for _, v, _, _ in pairs) or 1
    tone = box.get("tone") if box.get("tone") in _TONES else "acc"
    grp = bool(box.get("group_by"))
    lpm = _link_page_map(box) if grp else {}
    out = []
    for l, v, um, key in pairs:
        if grp:
            pg = lpm.get(str(key))
            dc, da = _drill_attrs(drill, page=pg) if pg else _drill_attrs(drill, value=key)
        else:
            dc, da = _drill_attrs(drill, page=key)
        out.append(f'<div class="brow{" um" if um else ""}{dc}"{da}>'
                   f'<span class="bl">{_esc(l)}{_UM_FLAG if um else ""}</span>'
                   f'<span class="btrk"><i class="bfill t-{tone}" style="width:{100 * v / mx:.0f}%"></i></span>'
                   f'<span class="bnum">{v:,}</span></div>')
    return "".join(out)


def _v_chips(box: dict, rows: list[dict], drill=None) -> str:
    pairs = _ds_pairs(box, rows)
    if not pairs:
        return ""
    grp = bool(box.get("group_by"))
    lpm = _link_page_map(box) if grp else {}
    out = []
    for l, v, um, key in pairs:
        if grp:
            pg = lpm.get(str(key))
            dc, da = _drill_attrs(drill, page=pg) if pg else _drill_attrs(drill, value=key)
        else:
            dc, da = _drill_attrs(drill, page=key)
        out.append(f'<span class="dchip{" um" if um else ""}{dc}"{da}>'
                   f'{_esc(l)}{_UM_FLAG if um else ""} <b>{v:,}</b></span>')
    return '<div class="dchips">' + "".join(out) + "</div>"


def _v_bignums(box: dict, rows: list[dict], drill=None) -> str:
    out = []
    for i, it in enumerate(box.get("items") or []):
        if not isinstance(it, dict):
            continue
        rs = _ds_rows(it["dataset"]) if it.get("dataset") else rows
        for f, v in (it.get("where") or {}).items():
            rs = [r for r in rs if str(r.get(f)) == str(v)]
        if it.get("stat") == "top" and it.get("group_by"):
            cnt = Counter(str(r.get(it["group_by"])) for r in rs if r.get(it["group_by"]))
            val = cnt.most_common(1)[0][0] if cnt else "—"
        else:
            val = f"{len(rs):,}"
        tone = it.get("tone")
        cls = f" t-{tone}" if tone in _TONES else ""
        dc, da = _drill_attrs(drill, item=i)
        out.append(f'<div class="bn-item{dc}"{da}><div class="bn-v{cls}">{_esc(val)}</div>'
                   f'<div class="bn-l">{_esc(str(it.get("label") or ""))}</div></div>')
    return f'<div class="bignums">{"".join(out)}</div>' if out else ""


def _v_cards(box: dict, rows: list[dict]) -> str:
    """Trend-style cards: name + direction glyph + status chip + per-bucket mini bars."""
    tf = str(box.get("title_field") or "title")
    df = str(box.get("dir_field") or "direction")
    sf = str(box.get("status_field") or "trend_status")
    series = str(box.get("series_field") or "count_by_year")
    # Trend vocab varies by generator (up/down/flat/emerging vs rising/falling/steady). Cover both so
    # a card shows a DIRECTION glyph, not the default → for every value (the glyph map only knew
    # rising/falling, but theme_trends writes up/down/flat/emerging — so all arrows were →).
    glyph = {"up": ("▲", "ok"), "rising": ("▲", "ok"),
             "down": ("▼", "crit"), "falling": ("▼", "crit"),
             "emerging": ("◆", "acc"), "flat": ("→", "mut"), "steady": ("→", "mut")}
    cards = []
    for r in rows[: int(box.get("limit") or 12)]:
        g, gc = glyph.get(str(r.get(df)), ("→", "mut"))
        counts = r.get(series) if isinstance(r.get(series), dict) else {}
        mini = ""
        if counts:
            try:
                mx = max(int(v) for v in counts.values()) or 1
                mini = '<div class="dmini">' + "".join(
                    f'<i style="height:{max(3, 26 * int(v) / mx):.0f}px" title="{_esc(str(y))}: {_esc(str(v))}"></i>'
                    for y, v in sorted(counts.items())) + "</div>"
            except (TypeError, ValueError):
                mini = ""
        name = str(r.get(tf) or r.get("name") or r.get("_name") or "?")
        cards.append(f'<div class="dcard"><div class="dc-n">{_page_link(r) if box.get("link") else _esc(name)}</div>'
                     f'<div class="dc-m"><span class="t-{gc}">{g} {_esc(str(r.get(df) or "—"))}</span>'
                     f'<span class="dchip">{_esc(str(r.get(sf) or "—"))}</span></div>{mini}</div>')
    return f'<div class="dcards">{"".join(cards)}</div>' if cards else ""


def _v_coverage(box: dict, rows: list[dict]) -> str:
    """Join coverage: this dataset's `list_field` values vs a `versus` dataset's key field,
    grouped by the versus dataset's group field (e.g. detections' covers_techniques vs
    techniques' attack_id, grouped by tactic) — covered/total ratio bars, health-toned."""
    lf = str(box.get("list_field") or "")
    vs = box.get("versus") or {}
    key_f = str(vs.get("key") or "")
    grp_f = str(vs.get("group_by") or "")
    if not (lf and key_f and grp_f):
        return ""
    covered = set()
    for r in rows:
        for t in (r.get(lf) or []):
            covered.add(str(t))
    cov: dict = {}
    for t in _ds_rows(vs):
        tid = str(t.get(key_f) or "")
        tac = t.get(grp_f)
        for x in (tac if isinstance(tac, list) else [tac]):
            if x:
                c = cov.setdefault(str(x), [0, 0])
                c[1] += 1
                if tid in covered:
                    c[0] += 1
    ranked = sorted(cov.items(), key=lambda kv: -kv[1][1])[: int(box.get("limit") or 10)]
    out = []
    for grp, (cvd, tot) in ranked:
        pct = 100 * cvd / tot if tot else 0
        tone = "ok" if pct >= 50 else "warn" if pct >= 30 else "crit"
        out.append(f'<div class="brow"><span class="bl">{_esc(grp)}</span>'
                   f'<span class="btrk"><i class="bfill t-{tone}" style="width:{pct:.0f}%"></i></span>'
                   f'<span class="bnum">{cvd}/{tot}</span></div>')
    return "".join(out)


def _v_doc(box: dict):
    """Render the LATEST matching document inline (dated filenames sort by name).
    Returns (html, meta)."""
    d = str(box.get("dir") or "").strip("/")
    pat = str(box.get("glob") or "*.md")
    if not pat.endswith(".md"):
        pat += ".md"
    base = WIKI / d
    # rglob so a PARTITIONED doc dir matches; read the winner by its path RELATIVE TO base — a sub-dir
    # hit passed as a bare basename to safe_read(base, name) 404s the ENTIRE tab (M-1513). Sort by the
    # filename DATE (then name), NOT the full path: a full-path sort ranks a flat/letter-leading page
    # above a YYYY/-sharded newer one (batch-2 re-verify). _visible_page drops reserved sub-dirs.
    cands = sorted((p for p in base.rglob(pat) if _visible_page(p, base)),
                   key=lambda p: (_file_date(p.name) or "", p.name), reverse=True) if base.is_dir() else []
    if not cands:
        return "", ""
    fm, body = split_fm(safe_read(base, str(cands[0].relative_to(base))))
    return f'<div class="ddoc">{render_md(body)}</div>', cands[0].stem


@app.get("/api/tab/{key}")
def api_tab(key: str):
    cfg = cockpit_config()
    d = (cfg.get("tab_defs") or {}).get(key)
    if not d:
        raise HTTPException(404, "no such tab")
    views = {"table": _v_table, "bars": _v_bars, "chips": _v_chips,
             "bignums": _v_bignums, "cards": _v_cards, "coverage": _v_coverage}
    drillable = {"bars", "chips", "bignums"}
    boxes = []
    for bi, b in enumerate(d["boxes"]):
        view = str(b.get("view") or "table")
        meta = str(b.get("meta") or "")
        unmapped: list = []
        if view == "doc":
            html, stem = _v_doc(b)
            meta = meta or stem
        else:
            rows = _ds_rows(b.get("dataset") or {})
            fn = views.get(view)
            if not fn:
                html = ""
            elif view in drillable:           # rows/values open a filtered list (okengine#189)
                html = fn(b, rows, (key, bi))
            else:
                html = fn(b, rows)
            if not meta and rows:
                meta = f"{len(rows):,} pages"
            if view in ("bars", "chips"):     # surface partial-labels-map drift (okengine#188)
                unmapped = [l for l, _v, um, _k in _ds_pairs(b, rows) if um]
        if not html and b.get("empty"):      # honest-empty: pipeline state is information
            html = f'<p class="dnote">{_esc(str(b["empty"]))}</p>'
            meta = meta or "awaiting first data"
        if html:
            box = {"title": str(b.get("title") or ""), "meta": meta,
                   "span": int(b.get("span") or 6), "html": html}
            if unmapped:                      # only present when the card is degraded
                box["unmapped"] = unmapped
            boxes.append(box)
    return {"label": d["label"], "boxes": boxes}


_DRILL_CAP = 300


def _row_page(r: dict) -> dict:
    """{path, title, type} for a dataset row — the shape the browse/list renderer consumes."""
    rel = r.get("_rel") or r.get("_name") or ""
    return {"path": f'{r.get("_sub", "")}/{rel}'.strip("/"),
            "title": _disp(r), "type": str(r.get("type") or "")}


@app.get("/api/drill/{tab}/{box}")
def api_drill(tab: str, box: int, value: str = Query(default=""), item: int = Query(default=-1)):
    """The pages behind one aggregate value — a bars/chips group_by bucket, or a bignums item
    (count / filtered-count / top-of-group). The dataset + filter are re-derived from the SAME
    tab config the widget rendered from; the client only names the box + the bucket, never a raw
    query (okengine#189). Returns browse-shaped pages so the UI reuses its list renderer."""
    cfg = cockpit_config()
    d = (cfg.get("tab_defs") or {}).get(tab)
    if not d or not (0 <= box < len(d.get("boxes") or [])):
        raise HTTPException(404, "no such box")
    b = d["boxes"][box]
    view = str(b.get("view") or "table")
    heading = str(b.get("title") or tab)
    if view == "bignums":
        items = b.get("items") or []
        if not (0 <= item < len(items)) or not isinstance(items[item], dict):
            raise HTTPException(404, "no such item")
        it = items[item]
        rows = _ds_rows(it["dataset"]) if it.get("dataset") else _ds_rows(b.get("dataset") or {})
        for f, v in (it.get("where") or {}).items():
            rows = [r for r in rows if str(r.get(f)) == str(v)]
        heading = str(it.get("label") or heading)
        if it.get("stat") == "top" and it.get("group_by"):     # drill the WINNING bucket
            cnt = Counter(s for r in rows for s in _gb_values(r.get(it["group_by"])))
            top = cnt.most_common(1)[0][0] if cnt else None
            rows = [r for r in rows if top in _gb_values(r.get(it["group_by"]))] if top else []
            heading = f"{heading}: {top}" if top else heading
    elif view in ("bars", "chips") and b.get("group_by"):
        gb = b["group_by"]
        rows = [r for r in _ds_rows(b.get("dataset") or {}) if value in _gb_values(r.get(gb))]
        lbl = {str(k): str(v) for k, v in (b.get("labels") or {}).items()}.get(value, value)
        heading = f"{heading}: {lbl}"
    else:
        raise HTTPException(400, "box is not drillable")
    rows = _ds_sorted(rows, {"field": "title"})[:_DRILL_CAP]
    return {"title": heading, "count": len(rows), "pages": [_row_page(r) for r in rows]}


@app.get("/api/dashboards")
def api_dashboards():
    groups = cockpit_config()["dashboards"]
    if groups:
        def _dmeta(path):
            p = WIKI / (path + ".md")
            try:
                fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return {}
            return fm if isinstance(fm, dict) else {}
        out, seen = [], set()
        for g in groups:
            if not isinstance(g, dict):
                continue
            items = []
            for it in (g.get("items") or []):
                if not (isinstance(it, dict) and it.get("path")):
                    continue
                path = str(it["path"]).strip().strip("/")
                fm = _dmeta(path)
                items.append({"path": path,
                              "title": str(it.get("title") or fm.get("title") or path.rsplit("/", 1)[-1]).strip(),
                              "desc": str(it.get("desc") or fm.get("summary") or fm.get("description") or "").strip()})
                seen.add(path)
            out.append({"group": str(g.get("group") or "").strip(), "items": items})
        # nothing hides: any dashboard not placed in a configured group lands in "Other"
        base = WIKI / "dashboards"
        extra = []
        if base.is_dir():
            # RECURSIVE, matching the default branch below: extensions write nested dashboards
            # (dashboards/<ns>/*.md); a flat *.md glob left an un-curated nested dashboard out of the
            # "Other" catch-all entirely, so it was invisible in the grid (invariant-audit M7).
            for p in sorted(base.rglob("*.md")):
                if not _visible_page(p, base):   # segment-level: drops dashboards/_archive/… (batch-2 re-verify)
                    continue
                rel = p.relative_to(base).with_suffix("").as_posix()
                path = f"dashboards/{rel}"
                if path in seen:
                    continue
                fm = _dmeta(path)
                extra.append({"path": path, "title": str(fm.get("title") or p.stem).strip(),
                              "desc": str(fm.get("summary") or fm.get("description") or "").strip()})
        if extra:
            out.append({"group": "Other", "items": extra})
        return {"groups": out}
    # default: auto-list every page under wiki/dashboards/
    base = WIKI / "dashboards"
    items = []
    if base.is_dir():
        # RECURSIVE: extensions write nested dashboards (dashboards/<ns>/*.md, e.g. competitive/);
        # a flat *.md glob left them invisible in the grid unless a pack curated them explicitly.
        for p in sorted(base.rglob("*.md")):
            if not _visible_page(p, base):   # segment-level: drops dashboards/_archive/… (batch-2 re-verify)
                continue
            try:
                fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            rel = p.relative_to(base).as_posix()[:-3]   # keep the sub-namespace in the path
            items.append({"path": f"dashboards/{rel}",
                          "title": str(fm.get("title") or p.stem).strip(),
                          "desc": str(fm.get("summary") or fm.get("description") or "").strip()})
    return {"groups": [{"group": "Dashboards", "items": items}] if items else []}


# Engine-generated operational/health artifacts, grouped for the Ops tab. These filenames are
# ENGINE outputs (produced by engine crons on any OKF vault) — not domain facts — so a curated
# map is legitimate here. Each group lists candidate page paths (without .md); only those that
# exist on disk are shown, and any remaining wiki/operational/*.md is swept into "Operational log"
# so a new artifact is never silently hidden.
_DATED_SERIES_RE = re.compile(r"^(.*)-(\d{4}-\d{2}-\d{2})$")   # `<series>-YYYY-MM-DD` daily snapshots
_OPS_GROUPS = [
    ("Health", ["dashboards/fleet-health", "HEALTH", "operational/kb-health-snapshots",
                "operational/page-quality-snapshots", "operational/page-quality-queue"]),
    ("Conformance", ["operational/schema-conformance", "dashboards/schema-drift",
                     "operational/schema-drift", "operational/deployment-validation",
                     "operational/field-loss-snapshots", "operational/bare-name-link-normalize"]),
    ("Review & grounding", ["_review-queue", "dashboards/source-grounding",
                            "dashboards/source-staleness", "operational/source-staleness"]),
    ("Operator", ["dashboards/operator"]),
]


def _ops_meta(path: str) -> dict:
    p = WIKI / (path + ".md")
    try:
        fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _ops_item(path: str) -> dict:
    fm = _ops_meta(path)
    return {"path": path,
            "title": str(fm.get("title") or path.rsplit("/", 1)[-1]).strip(),
            "desc": str(fm.get("summary") or fm.get("description") or "").strip(),
            "updated": str(fm.get("updated") or fm.get("generated_at") or fm.get("date") or "")[:10] or None}


def _ops_groups() -> list[dict]:
    """The operational/health page groups surfaced in the Ops tab. Only pages that exist are
    included; empty groups are dropped. Present on any OKF vault the engine crons have run."""
    out, seen = [], set()
    for label, paths in _OPS_GROUPS:
        items = []
        for path in paths:
            if path in seen or not (WIKI / (path + ".md")).is_file():
                continue
            seen.add(path)
            items.append(_ops_item(path))
        if items:
            out.append({"group": label, "items": items})
    # nothing hides: anything else under operational/ (new artifacts, per-day snapshots). A daily
    # series (`<series>-YYYY-MM-DD.md`) is collapsed to its NEWEST page so the log doesn't drown in
    # a page-per-day; the rolled-up `-snapshots` variants are already pinned in the groups above.
    base = WIKI / "operational"
    latest: dict[str, tuple[str, str]] = {}   # series-prefix -> (date, stem); "" key = non-dated (kept as-is)
    if base.is_dir():
        for p in sorted(base.glob("*.md")):
            if p.name.startswith(("_", ".")) or p.name == "INDEX.md":
                continue
            path = f"operational/{p.stem}"
            if path in seen:
                continue
            seen.add(path)
            md = _DATED_SERIES_RE.match(p.stem)
            if md:
                series, date = md.group(1), md.group(2)
                if series not in latest or date > latest[series][0]:
                    latest[series] = (date, p.stem)
            else:
                latest[p.stem] = ("", p.stem)   # non-dated: unique key, always kept
    extra = [_ops_item(f"operational/{stem}") for _, stem in sorted(latest.values(), key=lambda v: v[1])]
    if extra:
        out.append({"group": "Operational log", "items": extra})
    return out


def _ops_available() -> bool:
    return bool(_ops_groups())


@app.get("/api/ops")
def api_ops():
    return {"groups": _ops_groups()}


def _content_dirs() -> list:
    """Top-level content dirs ACTUALLY present under wiki/ — layout-agnostic basename resolution.
    Replaces a hardcoded 10-namespace tuple that 404'd pack-owned namespaces (detections/actor/cve/…)
    and walk-up sub-domain roots (invariant-audit M-1758). Fallback only: the direct wiki-relative
    path is tried first, and rglob under each dir reaches nested (partitioned/walk-up) pages."""
    try:
        return [d for d in WIKI.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]
    except OSError:
        return []


# ── UI extension panels (okengine#160, ported from the reader) ───────────────
_RPANELS_CACHE: list = [0.0, None]


def _reader_panels() -> dict:
    """Type-bound panel bindings staged by the deploy (okengine#160): VAULT/.okengine/
    reader-panels.json = {page_type: {kind, fields, ...}}. Cached briefly (refreshes on deploy)."""
    now = time.time()
    if _RPANELS_CACHE[1] is None or now - _RPANELS_CACHE[0] > 60:
        try:
            _RPANELS_CACHE[1] = json.loads((VAULT / ".okengine" / "reader-panels.json").read_text())
        except Exception:
            _RPANELS_CACHE[1] = {}
        _RPANELS_CACHE[0] = now
    return _RPANELS_CACHE[1] or {}


def _panel_for(fm: dict, body: str = "") -> dict | None:
    """The panel to render for a page. A GENERATED page self-declares `panel:` (e.g. viz's two-axis
    map, nodes included). Otherwise a type-bound `fields` panel is built from the staged bindings by
    pulling the declared frontmatter field values. Returns a render-ready dict or None."""
    p = fm.get("panel")
    if isinstance(p, dict) and p.get("kind"):
        # a body carrying the server-rendered chart (viz panel-svg block) supersedes the
        # client two-axis renderer — suppress to avoid a double chart. `fields` panels
        # have no embedded form and always render client-side.
        if p.get("kind") == "two-axis" and "<!-- panel-svg" in (body or ""):
            return None
        return p                                          # self-declared (carries its own data)
    b = _reader_panels().get(str(fm.get("type") or ""))
    if isinstance(b, dict) and b.get("kind") == "fields":
        items = [{"label": f, "value": fm.get(f)} for f in (b.get("fields") or []) if fm.get(f) is not None]
        return {"kind": "fields", "title": b.get("title") or "Details", "items": items} if items else None
    return None


def _provenance(fm: dict, body: str) -> dict:
    """Trust strip for the page overlay (ported from the reader's provenance view, extended). Answers
    "can I trust this?" from fields the trust lanes + write path already stamp: source coverage
    (cited source PAGES vs total refs), the Tier-2 grounding-check tally, human sign-off, handling
    markers (tlp/sensitivity), Admiralty grading (reliability/credibility), and composition
    provenance (maintained_by/discovered_by). Returns {} for a plain page with no trust signals so
    the overlay shows no empty strip."""
    srcs = fm.get("sources")
    srcs = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
    # a cited SOURCE PAGE (vault-internal) vs a bare external URL — a URL also contains "/", so
    # exclude an http(s) scheme (tighter than the reader's port, which double-counted URLs as pages).
    page_srcs = sum(1 for s in srcs if not str(s).lower().startswith(("http://", "https://"))
                    and ("/" in str(s) or str(s).lower().endswith(".md")))
    grounding = None
    g = re.search(r"##\s+Grounding check(.*?)(?:\n##\s|\Z)", body, re.S | re.I)
    if g:
        seg = g.group(1)
        grounding = {"supported": len(re.findall(r"\*\*\s*supported", seg, re.I)),
                     "unsupported": len(re.findall(r"\*\*\s*(?:unsupported|not[- ]found|contradict)", seg, re.I))}

    def _v(k):                                   # normalize a fm value to a display string, or None
        x = fm.get(k)
        if x in (None, "", [], {}):
            return None
        return ", ".join(str(i) for i in x) if isinstance(x, list) else str(x)

    prov = {
        "sources": len(srcs), "source_pages": page_srcs, "grounding": grounding,
        "needs_review": bool(fm.get("needs_review")),
        "reviewed_by": _v("reviewed_by"), "reviewed_on": _v("reviewed_on"),
        "tlp": _v("tlp"), "sensitivity": _v("sensitivity"),
        "reliability": _v("reliability"), "credibility": _v("credibility"),
        "maintained_by": _v("maintained_by"), "discovered_by": _v("discovered_by"),
    }
    has_signal = (prov["sources"] or grounding or prov["needs_review"] or prov["reviewed_by"]
                  or prov["tlp"] or prov["sensitivity"] or prov["reliability"] or prov["credibility"]
                  or prov["maintained_by"] or prov["discovered_by"])
    return prov if has_signal else {}


# ── page overlay: fact panel + multi-source conflict/observation view (ported from the reader) ──
# The reader treats a clicked page as a TYPED intel object: the surfaced frontmatter is its profile
# (fact panel), record-keeping is tucked away (record details), and the assembler's multi-source
# `conflicts:` + `observations/` records show "what each source says". All domain-agnostic — it
# renders whatever fields/conflicts/observations exist, in frontmatter order.
_META_PANEL_SKIP = {"title", "name", "type", "version", "raw", "needs_review", "sources"}   # needs_review -> badge/strip; sources -> the graded Evidence section (both drop from the fact panel)
_META_SECONDARY = {"tlp", "created", "updated", "last_updated", "last_seen", "first_seen",
                   "assembled_from", "tier", "tlp_caveat",
                   "maintained_by", "discovered_by", "created_by", "last_modified_by"}
_REL_RANK = {c: i for i, c in enumerate("FEDCBA")}    # A=5 (highest) … F=0; unknown -> -1
_SRC_REL_CACHE: tuple[float, dict] = (0.0, {})
_OBS_INDEX_CACHE: tuple[float, dict] = (0.0, {})


def _source_reliability() -> dict:
    """{source -> Admiralty reliability A–F} from the pack's schema.yaml `source_registry`, so the
    conflict view can label each claim. Domain-agnostic; cached (vault is :ro)."""
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


def _meta_compact_dict(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, "", [], {}))


def _val_text(v) -> str:
    return _meta_compact_dict(v) if isinstance(v, dict) else str(v)


def _url_label(url: str) -> str:
    """Friendly link text for a bare URL — its host minus 'www.' (e.g. attack.mitre.org)."""
    try:
        host = urlparse(url).netloc
    except Exception:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    return host or url


def _ref_target(s: str) -> str | None:
    """If `s` is a wiki-relative path that resolves to a vault page, return its canonical key (no
    `.md`) for an internal link; else None. Path-shaped only; basename fallback resolves a flat-form
    ref to a sharded page (entities/foo -> entities/f/foo)."""
    if not isinstance(s, str):
        return None
    key = s.strip()
    if "/" not in key or "://" in key or " " in key:
        return None
    key = key[:-3] if key.endswith(".md") else key
    if not WIKI.is_dir():
        return None
    try:
        cand = (WIKI / (key + ".md")).resolve()
        if cand.is_file() and _within(WIKI, cand):
            return key
        hits = [p for p in WIKI.rglob(Path(key).name + ".md") if not _skip(p.name)]
        if len(hits) == 1:
            return str(hits[0].resolve().relative_to(WIKI.resolve()))[:-3]
    except OSError:
        pass
    return None


def _meta_values(v) -> list[dict]:
    """One frontmatter value -> display chips. http(s) scalars + url/href list items become external
    links; a value resolving to a vault page becomes an internal page link; dicts compact to k=v."""
    out: list[dict] = []
    for el in (v if isinstance(v, list) else [v]):
        if isinstance(el, dict):
            url = el.get("url") or el.get("href")
            txt = (el.get("id") or el.get("value") or el.get("name") or el.get("std")
                   or (_url_label(url) if url else None) or _meta_compact_dict(el))
            out.append({"text": str(txt), "url": str(url)} if url else {"text": str(txt)})
        else:
            s = str(el)
            if s.startswith(("http://", "https://")):
                out.append({"text": _url_label(s), "url": s})
            else:
                tgt = _ref_target(s)
                out.append({"text": s, "page": tgt} if tgt else {"text": s})
    return out


def _meta_panel_items(fm: dict, order: list | None = None) -> dict:
    """Frontmatter split into `primary` (the page's intel — surfaced) and `secondary`
    (record-keeping — collapsed). Renders whatever fields exist. When the pack supplies a per-type
    `order`, that order IS the profile: only its fields are primary (in order); everything else
    (record-keeping — ids, urls, dates, provenance the pack didn't put in the profile) drops to
    secondary, so the top reads as a curated analyst card, not a field dump. Without an order, the
    split falls back to the `_META_SECONDARY` heuristic."""
    primary: list[dict] = []
    secondary: list[dict] = []
    if not isinstance(fm, dict):
        return {"primary": primary, "secondary": secondary}
    keys = list(fm.keys())
    rank = {f: i for i, f in enumerate(order)} if order else {}
    if order:
        keys.sort(key=lambda k: rank.get(k, len(order)))   # stable: declared first, rest keep fm order
    for k in keys:
        v = fm.get(k)
        if k in _META_PANEL_SKIP or v is None or v == "" or v == [] or v == {}:
            continue
        label = str(k).replace("_", " ").replace("-", " ").strip()
        item = {"label": label[:1].upper() + label[1:], "values": _meta_values(v)}
        if order:
            is_secondary = k not in rank                   # profiled: only declared fields are the profile
        else:
            is_secondary = k in _META_SECONDARY            # unprofiled: heuristic record-keeping set
        (secondary if is_secondary else primary).append(item)
    return {"primary": primary, "secondary": secondary}


def _shape_conflicts(fm: dict) -> list[dict]:
    """The assembler's `conflicts:` frontmatter -> per-field 'what each source says', each value
    tagged with its source(s) + Admiralty reliability + rank (for the ≥B filter), headline flagged."""
    rel = _source_reliability()
    out: list[dict] = []
    conflicts = fm.get("conflicts")
    if not isinstance(conflicts, list):     # `conflicts: 42` -> `for c in 42` TypeError (M28)
        conflicts = []
    for c in conflicts:
        if not isinstance(c, dict):
            continue
        headline = c.get("headline")
        vals: list[dict] = []
        values = c.get("values")            # guard the container (`values: 42` = non-iterable scalar)
        if not isinstance(values, list):    # AND each entry below — see reader's copy (invariant-audit M28)
            values = []
        for v in values:
            if not isinstance(v, dict):     # scalar entry -> .get() AttributeError 500s the page
                continue
            v_sources = v.get("sources")    # third container: `sources: 42` -> for s in 42 (M28)
            if not isinstance(v_sources, list):
                v_sources = []
            srcs = [{"name": str(s), "reliability": rel.get(str(s), "")} for s in v_sources]
            rank = max((_REL_RANK.get(str(s["reliability"]).upper()[:1], -1) for s in srcs), default=-1)
            vals.append({"value": _val_text(v.get("value")), "sources": srcs, "rank": rank,
                         "is_headline": v.get("value") == headline})
        out.append({"field": str(c.get("field") or ""), "headline": _val_text(headline), "values": vals})
    return out


def _evidence_sources(fm: dict) -> list[dict]:
    """A page's cited `sources:` as graded evidence rows: name, internal page (if it resolves),
    Admiralty reliability (from schema.yaml source_registry), and recency (the source page's date,
    when it's a page). Turns a bare source list into dated, graded citations. Reliability/date are
    "" when the deployment doesn't populate a registry or the source is a prose name."""
    srcs = fm.get("sources")
    srcs = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
    rel = _source_reliability()
    out: list[dict] = []
    for s in srcs:
        name = str(s).strip()
        if not name:
            continue
        page = _ref_target(name)
        date = ""
        if page:
            try:
                pfm, _ = split_fm(_read_head(WIKI / (page + ".md")))
                date = str(pfm.get("published") or pfm.get("date") or pfm.get("updated")
                           or pfm.get("last_updated") or "")[:10]
            except OSError:
                pass
        out.append({"name": name, "page": page, "reliability": rel.get(name, ""), "date": date})
    return out


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
            if _skip(p.name) or _reserved_seg(p):   # skip _archive/ retired observations (batch-2 re-verify)
                continue
            fm, _ = split_fm(_read_head(p))
            canon = str(fm.get("canonical") or "").strip().lower()
            if canon:
                key = str(p.resolve().relative_to(WIKI.resolve()))[:-3]
                idx.setdefault(canon, []).append({"source": str(fm.get("source") or ""), "key": key})
    _OBS_INDEX_CACHE = (now, idx)
    return idx


# ── page quality/status badges (okengine — generic page health) ─────────────────────────────────
# A problem-only badge row atop the overlay, computed from data already present. Nothing
# domain-specific: which fields a type REQUIRES comes from schema.yaml; the rest are envelope
# signals (sources/grounding/review/conflicts/recency/size). A clean page gets no row.
_STALE_DAYS = max(0, int(os.environ.get("OKENGINE_COCKPIT_STALE_DAYS", "90")))   # 0 disables the stale badge
_THIN_CHARS = 240
_TYPE_REQ_CACHE: tuple[float, dict] = (0.0, {})


def _type_required_fields() -> dict:
    """{type -> [required field names]} from schema.yaml `types`, so a page missing a field its type
    requires can be flagged. 'type' itself is always present -> dropped. Cached (vault :ro)."""
    global _TYPE_REQ_CACHE
    now = time.monotonic()
    if now - _TYPE_REQ_CACHE[0] < _DIR_TTL:
        return _TYPE_REQ_CACHE[1]
    out: dict = {}
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            types = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("types") or {}
            for k, v in (types.items() if isinstance(types, dict) else []):
                req = (v or {}).get("required") if isinstance(v, dict) else None
                if isinstance(req, list):
                    out[str(k)] = [str(f) for f in req if str(f) != "type"]
        except Exception:
            pass
    _TYPE_REQ_CACHE = (now, out)
    return out


def _quality_badges(fm: dict, body: str, ptype: str, prov: dict, conflicts: list) -> list[dict]:
    """Generic page-health badges from data already present — only PROBLEM signals surface (a clean
    page gets no row). level: bad (red) | warn (amber). Each carries a `title` tooltip."""
    b: list[dict] = []
    prov = prov or {}
    # required fields the schema declares for this type
    missing = [f for f in _type_required_fields().get(ptype or "", []) if fm.get(f) in (None, "", [], {})]
    if missing:
        b.append({"label": f"missing {', '.join(missing[:3])}", "level": "bad",
                  "title": f"required field(s) absent for type '{ptype}': {', '.join(missing)}"})
    # sourcing / grounding (an empty prov dict means no sources — the badge is correct)
    nsrc = prov.get("sources", 0)
    if not nsrc:
        b.append({"label": "no sources", "level": "bad", "title": "no sources cited"})
    elif not prov.get("source_pages"):
        b.append({"label": "ungrounded", "level": "warn",
                  "title": f"{nsrc} prose source(s) — none link to a source page"})
    g = prov.get("grounding")
    if g and g.get("unsupported"):
        n = g["unsupported"]
        b.append({"label": f"{n} unsupported claim{'s' if n != 1 else ''}", "level": "bad",
                  "title": "the Grounding check flagged unsupported claims"})
    if fm.get("needs_review"):
        b.append({"label": "needs review", "level": "warn", "title": "flagged for human review"})
    if conflicts:
        n = len(conflicts)
        b.append({"label": f"{n} conflicting field{'s' if n != 1 else ''}", "level": "warn",
                  "title": "sources disagree on one or more fields"})
    if _STALE_DAYS:
        d = _as_date(fm.get("updated") or fm.get("last_updated") or fm.get("last_seen"))
        if d is not None:
            age = (TODAY() - d).days
            if age > _STALE_DAYS:
                b.append({"label": f"stale {age}d", "level": "warn",
                          "title": f"last updated {age} days ago (> {_STALE_DAYS}d)"})
    prose = _strip_md(body or "").strip()
    nfields = sum(1 for k, v in fm.items() if k not in _META_PANEL_SKIP and v not in (None, "", [], {}))
    if len(prose) < _THIN_CHARS and nfields < 4:
        b.append({"label": "thin", "level": "warn", "title": f"sparse page (<{_THIN_CHARS} chars, few fields)"})
    return b


@app.get("/api/page")
def api_page(path: str = Query(...)):
    """Render any wiki page (entity/source/concept/...) for click-through navigation."""
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
    fm, body = split_fm(cp.read_text(encoding="utf-8", errors="replace"))
    title = fm.get("title") or fm.get("name") or Path(path).name
    ptype = str(fm.get("type") or "")
    profiles = cockpit_config().get("profiles", {})
    m = _meta_panel_items(fm, profiles.get(ptype))
    slug = cp.stem.lower()
    prov = _provenance(fm, body)
    conflicts = _shape_conflicts(fm)
    return {"path": path, "title": str(title), "type": ptype,
            "rel": str(cp.relative_to(WIKI.resolve())), "html": render_md(body),
            # a type the pack gives a `profiles:` order to splits its fields into a primary fact
            # panel (`meta`) vs secondary Record details (`meta_aux`). The body always leads the
            # page; the fact panel follows it (see openPage in static/app.js).
            "profiled": ptype in profiles,
            "panel": _panel_for(fm, body), "provenance": prov,
            "meta": m["primary"], "meta_aux": m["secondary"],
            "conflicts": conflicts, "needs_review": bool(fm.get("needs_review")),
            "observations": _observations_by_canonical().get(slug, []),
            "citations": _evidence_sources(fm),
            "quality": _quality_badges(fm, body, ptype, prov, conflicts)}


@app.get("/api/rollup")
def api_rollup(stream: str = Query(...), days: int = Query(7)):
    """Stack the latest N days of a briefing stream into one scrollable review."""
    cfg = _streams().get(stream)
    if not cfg:
        raise HTTPException(404, "unknown stream")
    n = max(1, min(int(days), 31))
    parts = []
    for dt in _stream_dates(stream)[:n]:
        try:
            d = api_doc(stream=stream, date=dt)
        except HTTPException:
            continue
        parts.append(f'<section class="rollup-day"><h2 class="rday">{dt}</h2>{d["html"]}</section>')
    return {"title": f"{cfg['label']} — past {len(parts)} days",
            "count": len(parts), "html": "".join(parts)}


# ── downloads (md / docx / pdf via pandoc) ─────────────────────────────────
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


# Print stylesheet for the weasyprint PDF path. Pandoc ships no CSS, so a wide markdown table or a
# long unbreakable token (URL, hash, IOC) runs off the right edge of the page. Constrain everything
# to the page box: real margins, fixed-layout full-width tables, and word-breaking in every cell.
_PDF_CSS = (
    "@page{size:A4;margin:1.8cm 1.7cm}"
    "html{font-family:'DejaVu Serif',serif;font-size:10.5pt;line-height:1.42}"
    "body{max-width:100%}"
    "h1{font-size:18pt;margin:0 0 .3em}h2{font-size:13.5pt;margin:1.1em 0 .3em}"
    "h3{font-size:11.5pt;margin:.9em 0 .2em}"
    "p,li{overflow-wrap:break-word;word-wrap:break-word}"
    "pre,code{white-space:pre-wrap;word-break:break-word;font-size:9pt}"
    "pre{background:#f5f5f5;padding:6px 8px;border-radius:4px}"
    "table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:8.6pt;margin:.6em 0}"
    "th,td{border:1px solid #bbb;padding:3px 5px;vertical-align:top;"
    "overflow-wrap:break-word;word-break:break-word}"
    "th{background:#f0f0f0;text-align:left}"
    "img{max-width:100%}a{color:inherit;text-decoration:none}"
)


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
            raise HTTPException(500, f"convert failed: {e.stderr.decode('utf-8','replace')[:300]}")
        return out.read_bytes()


_DL_MIME = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}


@app.get("/api/download")
def api_download(fmt: str, stream: str | None = None, date: str | None = None,
                 path: str | None = None):
    if fmt not in _DL_MIME:
        raise HTTPException(400, "fmt must be md|docx|pdf")
    raw, base, title = _resolve_source(stream, date, path)
    clean = _clean_markdown(raw, title)
    data = clean.encode("utf-8") if fmt == "md" else _pandoc(clean, fmt, title)
    fname = f"{base}.{fmt}"
    return Response(content=data, media_type=_DL_MIME[fmt],
                    headers={"Content-Disposition": f"attachment; filename=\"{quote(fname)}\""})


# A leading progress-narration line ("Checking the vault…", "Pulling the pages now", "Good leads").
# The contract asks the agent to keep these out of a report, but local models still emit them; we
# strip them from the EXPORTED report (they're fine as live feedback in the chat).
_NARRATION = re.compile(
    r"^\s*(checking|pulling|retrieving|searching|assessing|reviewing|looking|gathering|fetching|"
    r"scanning|querying|good\b|i (now|have|'ll|'ve)|let me|now (pulling|checking|retrieving|"
    r"searching))\b", re.I)


def _strip_report_preamble(md: str) -> str:
    """Drop a leading block of progress-narration IFF it's clearly delimited by a `---` / heading —
    conservative so real content is never removed."""
    lines = md.split("\n")
    n = len(lines)
    i = 0
    while i < n and not lines[i].strip():
        i += 1
    start = i
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if _NARRATION.match(s) or s.endswith("now") or s.endswith("now."):
            i += 1
            continue
        break
    if i == start:                                   # no leading narration
        return md
    j = i
    while j < n and not lines[j].strip():
        j += 1
    if j < n:
        b = lines[j].strip()
        if b in ("---", "***", "___"):               # narration → thematic break → report
            return "\n".join(lines[j + 1:]).lstrip("\n")
        if b.startswith(("#", "**")):                # narration → title/heading → report
            return "\n".join(lines[j:]).lstrip("\n")
    return md                                        # not clearly delimited — leave it untouched


def _clean_chat_markdown(md: str, title: str | None = None) -> str:
    """Portable markdown from a chat report: leading progress-narration stripped, internal vault
    links flattened to text (they resolve only in-app), wikilinks flattened, an optional title as H1."""
    body = _strip_report_preamble(_deref_local_links(_delink(md.strip())))
    t = str(title or "").strip()
    if t and not body.lstrip().startswith("# "):
        body = f"# {t}\n\n{body}"
    return body.strip() + "\n"


@app.post("/api/chat_export")
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
    return Response(content=blob, media_type=_DL_MIME[fmt],
                    headers={"Content-Disposition": f"attachment; filename=\"{quote(fname)}\""})


# ── global search (ripgrep across the vault) ───────────────────────────────
# Dir → rank (lower sorts first). Content pages rank above sources. Generic
# namespace names only (no domain knowledge); unlisted dirs get a mid rank.
_SEARCH_RANK = {"entities": 0, "concepts": 1, "predictions": 2,
                "weekly": 3, "dailies": 3, "briefings": 3, "marketing": 4, "questions": 4,
                "dashboards": 5, "reports": 6, "operational": 7, "sources": 9}


@app.get("/api/search")
def api_search(q: str = Query(...), limit: int = 40):
    q = q.strip()
    if len(q) < 2:
        return {"q": q, "results": []}
    # `!_?*` (underscore + ≥1 char), NOT `!_*` — the latter also prunes the bare-`_` reshard bucket
    # (entities/x/_/x-force.md), making a resharded entity browsable-but-unfindable. Mirrors
    # _is_reserved_seg's bare-`_` exemption so search agrees with browse (batch-2 gate).
    cmd = ["rg", "-i", "-F", "-m1", "--no-heading", "-n", "--no-messages",
           "--max-columns", "240", "-g", "*.md", "-g", "!*.bak.*", "-g", "!_?*",
           "--", q, str(WIKI)]
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
        rows.append({"path": rel, "dir": d, "title": Path(rel).name,
                     "snippet": re.sub(r"\s+", " ", text).strip()[:200]})
        if len(rows) >= 1500:
            break
    ql = q.lower()
    rows.sort(key=lambda r: (0 if ql in r["title"].lower() else 1,
                             _SEARCH_RANK.get(r["dir"], 8), r["path"]))
    return {"q": q, "total": len(rows), "results": rows[:max(1, min(limit, 100))]}


# ── backlinks (knowledge-graph: "what links here") ─────────────────────────
# The cron-precomputed wiki/.backlinks.json (below) is served directly. The live FALLBACK builds
# the graph by scanning [[wikilinks]] over the vault directly (okengine#179) — it used to shell
# the heavy `iwe find -l 0` full-graph dump (~4GB/~550s on a big vault, and UNFILTERED). The scan
# MIRRORS scripts/cron/backlink_lib (keep in sync). Invert forward-refs into a {target:[referrers]}
# map, filter + curate titles, cache with a TTL. Read-only, so it works on the :ro mount.
_BACKLINKS: dict = {"map": None, "ts": 0.0}
_BACKLINKS_TTL = max(60, int(os.environ.get("OKENGINE_BACKLINKS_TTL", "86400")))  # 24h default —
# the fallback scan is cheap now, but backlinks change over days, so a day-stale "what links here"
# is fine. Tune per-deployment via the env var.
_BL_LOCK = threading.Lock()

# Cron-precomputed graph (okengine#168): the `backlinks-refresh` engine cron
# writes the inverted+filtered+titled map to wiki/.backlinks.json once per
# deployment per day (scripts/cron/backlink_lib.py is the canonical logic —
# it also applies the generated-source filter and curated titles this app's
# live build never had). When present and fresh we serve it directly and never
# run iwe in this container; the live build below is only the fallback for a
# missing/stale artifact. Ceiling default 48h = two missed daily runs.
_BL_ARTIFACT_MAX_AGE = max(3600, int(os.environ.get("OKENGINE_BACKLINKS_MAX_AGE", "172800")))
_BL_ARTIFACT: dict = {"map": None, "mtime": None}


def _artifact_backlinks() -> dict | None:
    """The cron-precomputed backlink map (wiki/.backlinks.json), or None when
    absent/stale/corrupt — callers then fall back to the live iwe build.
    Freshness is judged by file mtime (the cron's atomic rename stamps it at
    build time); the parsed map is cached and only re-read when the mtime
    changes, so the steady-state cost per request is one stat()."""
    p = WIKI / ".backlinks.json"
    try:
        st = p.stat()
    except OSError:
        return None
    if time.time() - st.st_mtime > _BL_ARTIFACT_MAX_AGE:
        return None
    if _BL_ARTIFACT["mtime"] == st.st_mtime and _BL_ARTIFACT["map"] is not None:
        return _BL_ARTIFACT["map"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    m = data.get("backlinks")
    if not isinstance(m, dict):
        return None
    _BL_ARTIFACT["map"] = m
    _BL_ARTIFACT["mtime"] = st.st_mtime
    return m


# Backlink filter + scanner — MIRRORS scripts/cron/backlink_lib (keep in sync so this fallback
# doesn't drift from the served artifact). See the reader for the annotated originals.
_RESERVED_BL_NAMES = frozenset({"HOT.md", "log.md"})
_BL_DROP_CACHE: tuple = (0.0, None)
_H1_BL = re.compile(r"^# (.+)$", re.MULTILINE)
_BL_FM = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)
_BL_WIKI = re.compile(r"\[\[([^\]]+?)\]\]", re.DOTALL)
_BL_MD = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?)\)")
_BL_FENCE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[^\n]*$", re.DOTALL | re.MULTILINE)
_BL_INLINE = re.compile(r"(`+)[^\n]*?\1")


def _bl_skip_name(name: str) -> bool:
    return (name.startswith(("_", ".")) or ".bak." in name
            or name in ("INDEX.md", "index.md") or name.startswith(("INDEX-", "index-")))


def _backlink_drop_dirs() -> frozenset:
    """schema.yaml `backlink_drop:` (default {'sources'}; pack knob). MIRRORS backlink_lib."""
    global _BL_DROP_CACHE
    now = time.monotonic()
    if _BL_DROP_CACHE[1] is not None and now - _BL_DROP_CACHE[0] < _DIR_TTL:
        return _BL_DROP_CACHE[1]
    drop = {"sources"}
    sp = VAULT / "schema.yaml"
    if sp.is_file():
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            if "backlink_drop" in sch:
                drop = set()
                for e in (sch.get("backlink_drop") or []):
                    seg = str(e).strip().strip("/")
                    if seg.startswith("wiki/"):
                        seg = seg[len("wiki/"):]
                    seg = seg.strip("/").split("/")[0]
                    if seg:
                        drop.add(seg)
        except Exception:
            pass
    _BL_DROP_CACHE = (now, frozenset(drop))
    return _BL_DROP_CACHE[1]


def _skip_backlink_src(key: str) -> bool:
    name = key.split("/")[-1]
    if not name.endswith(".md"):
        name += ".md"
    if _bl_skip_name(name) or name in _RESERVED_BL_NAMES:
        return True
    parts = key.split("/")
    # reserved sub-dir (_archive/…) at ANY depth, AND an excluded/surfaced/drop namespace at any depth
    # (walk-up sub-domain nests them) — a leaf + top-level-only check let archived/excluded pages
    # contribute "what links here" edges that browse + search hide (batch-2 completeness re-verify).
    if any(_is_reserved_seg(seg) for seg in parts[:-1]):
        return True
    drop = _excluded_dirs() | _SURFACED_DERIVED | _backlink_drop_dirs()
    return any(seg in drop for seg in parts[:-1])


def _backlink_title(src: str) -> str:
    """Curated label: frontmatter title/name → # H1 → de-slugged basename (MIRRORS
    backlink_lib.page_title) — replaces iwe's raw first-heading title."""
    try:
        text = (WIKI / f"{src}.md").open("rb").read(8192).decode("utf-8", "replace")
    except OSError:
        text = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            try:
                fm = yaml.safe_load(text[3:end]) or {}
                t = str(fm.get("title") or fm.get("name") or "").strip()
                if t:
                    return t
            except Exception:
                pass
            text = text[end + 4:]
    h1 = _H1_BL.search(text)
    return h1.group(1).strip() if h1 else (src.split("/")[-1].replace("-", " ").strip() or src)


def _bl_strip(text: str) -> str:
    m = _BL_FM.match(text)
    if m:
        text = text[m.end():]
    return _BL_INLINE.sub(" ", _BL_FENCE.sub("\n", text))


def _bl_wikikey(inner: str):
    k = inner.split("|", 1)[0].split("\n", 1)[0].split("#", 1)[0].strip()
    if not k or k.startswith(("http://", "https://", "mailto:")):
        return None
    return k[:-3] if k.endswith(".md") else k


def _bl_mdkey(url: str, doc_dir: str):
    u = url.split("#", 1)[0].strip()
    if not u or u.startswith(("http://", "https://", "mailto:", "#")) or not u.endswith(".md"):
        return None
    rel = os.path.normpath(os.path.join(doc_dir, u))
    return None if rel.startswith("..") else rel[:-3]


def _scan_forward_refs() -> list:
    """Forward-reference scan over WIKI (iwe-parity). MIRRORS backlink_lib.scan_forward_refs."""
    paths = list(WIKI.rglob("*.md"))
    keys = [p.relative_to(WIKI).as_posix()[:-3] for p in paths]
    keyset = set(keys)
    by_base: dict = {}
    for k in keys:
        by_base.setdefault(k.rsplit("/", 1)[-1], []).append(k)
    for lst in by_base.values():
        lst.sort()

    def resolve(raw: str) -> str:
        if raw in keyset:
            return raw
        cands = by_base.get(raw.rsplit("/", 1)[-1])
        return cands[0] if cands else raw

    docs = []
    for p, key in zip(paths, keys):
        if _skip_backlink_src(key):
            continue
        try:
            body = _bl_strip(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        doc_dir = key.rsplit("/", 1)[0] if "/" in key else ""
        refs, seen = [], set()
        for rx, kf in ((_BL_WIKI, lambda m: _bl_wikikey(m.group(1))),
                       (_BL_MD, lambda m: _bl_mdkey(m.group(1), doc_dir))):
            for m in rx.finditer(body):
                k = kf(m)
                if k:
                    k = resolve(k)
                    if k != key and k not in seen:
                        seen.add(k)
                        refs.append({"key": k})
        docs.append({"key": key, "references": refs})
    return docs


def _build_backlinks() -> dict:
    bl: dict[str, list] = {}
    for d in _scan_forward_refs():
        src = d.get("key")
        if not src or _skip_backlink_src(src):
            continue
        title = _backlink_title(src)
        for ref in d.get("references") or []:
            tgt = ref.get("key")
            if not tgt or tgt == src or _skip_backlink_src(tgt):
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
    m = _artifact_backlinks()
    if m is not None:                     # precomputed artifact wins — no iwe here
        return m
    now = time.monotonic()
    if _BACKLINKS["map"] is not None and now - _BACKLINKS["ts"] <= _BACKLINKS_TTL:
        return _BACKLINKS["map"]
    if not blocking:
        # NEVER block a request on the heavy IWE build (minutes / ~2GB on a large vault):
        # refresh in the background and serve the current (possibly stale / empty) map now.
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
    """Build the backlink graph AND pre-scan the tab datasets in the background at startup so the
    first user request doesn't block on the build or a cold multi-thousand-file namespace scan."""
    threading.Thread(target=_load_backlinks, daemon=True).start()
    threading.Thread(target=_warm_tab_datasets, daemon=True).start()


_BL_GROUP_CAP = 12   # items shown per Related-rail type group (the rest collapse to "+N more")


@app.get("/api/backlinks")
def api_backlinks(path: str = Query(...), limit: int = 100):
    """Docs that reference `path` via the IWE wikilink graph. `path` is the
    wiki-relative key without .md (e.g. 'concepts/<name>')."""
    key = path[:-3] if path.endswith(".md") else path
    refs = _load_backlinks(blocking=False).get(key, [])   # never block the UI on the IWE build
    # Typed "Related" rail: group referrers by their namespace (the first path segment == the OKF
    # type bucket — predictions/, findings/, entities/, dashboards/, …). Counts are over ALL
    # referrers; items are capped per group. Ordered most-connected first — generic, no domain
    # priority baked into the engine. (sources/ is already dropped from the graph upstream.)
    groups: dict[str, list] = {}
    for r in refs:
        rk = str(r.get("key") or "")
        ns = rk.split("/", 1)[0] if "/" in rk else "(root)"
        groups.setdefault(ns, []).append(r)
    grouped = [{"ns": ns, "label": _humanize(ns), "count": len(items), "items": items[:_BL_GROUP_CAP]}
               for ns, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
    return {"path": key, "count": len(refs), "groups": grouped,
            "backlinks": refs[:max(1, min(limit, 500))]}   # flat list kept for back-compat


# ── generic browse (namespaces → pages, ported from okengine-reader) ─────────
# A function-agnostic explorer alongside the cockpit's curated tabs: the wiki/
# directory tree with per-namespace page lists + pack-declared "by kind" groups,
# discovered from the vault at runtime (ships no domain knowledge).
def _skip(name: str) -> bool:
    """Reserved / generated files the browse rail never lists or renders (underscore/dot
    reserved, backups, the generated per-directory INDEX pages)."""
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


_BROWSE_TTL = 120.0  # vault is :ro and cron-refreshed; brief staleness is fine
# init ts = -inf so the first call always misses (monotonic() can be < TTL on a fresh boot).
_EXCLUDE_CACHE: tuple[float, frozenset] = (float("-inf"), frozenset())
_GROUPS_CACHE: tuple[float, list] = (float("-inf"), [])
_RAILTOP_CACHE: tuple[float, tuple] = (float("-inf"), ("", ()))
_BROWSE_CACHE: dict[str, tuple[float, list[dict]]] = {}
# `dashboards/` is generated but MEANT to be read (the payoff of the vault); schema `exclude:`
# scopes CONFORMANCE, not reader visibility, so surface it (flagged `derived`) rather than hide it.
_SURFACED_DERIVED = frozenset({"dashboards"})
_DERIVED_TYPES = {"dashboard"}      # generated artifacts vs curated knowledge
_FM_SCAN_BYTES = 16384              # a listing needs only frontmatter + first H1 (file head)


def _excluded_dirs() -> frozenset:
    """Top-level wiki/ dir names hidden from browse: the pack's schema.yaml `exclude:` set
    MINUS the surfaced synthesized namespaces (dashboards/). Cached (vault :ro)."""
    global _EXCLUDE_CACHE
    now = time.monotonic()
    if now - _EXCLUDE_CACHE[0] < _BROWSE_TTL:
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


def _display_groups() -> list[tuple[str, frozenset]]:
    """Optional `display_groups:` (label -> [types]) from schema.yaml — browse pages BY KIND
    across namespaces. Domain-agnostic: the pack supplies the labels. Order preserved."""
    global _GROUPS_CACHE
    now = time.monotonic()
    if now - _GROUPS_CACHE[0] < _BROWSE_TTL:
        return _GROUPS_CACHE[1]
    groups: list[tuple[str, frozenset]] = []
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


def _rail_top_section() -> tuple[str, tuple]:
    """Optional `rail_top_section:` {label, namespaces} from schema.yaml — synthesized-output
    namespaces pinned to the top of the browse rail. Defaults to a Briefs section when
    briefings/ exists and the pack declares none."""
    global _RAILTOP_CACHE
    now = time.monotonic()
    if now - _RAILTOP_CACHE[0] < _BROWSE_TTL:
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
    if not label and (WIKI / "briefings").is_dir():
        label, ns = "Briefs", ("briefings",)
    _RAILTOP_CACHE = (now, (label, ns))
    return _RAILTOP_CACHE[1]


def _top_dirs() -> list[str]:
    """Non-excluded top-level wiki/ directory names."""
    if not WIKI.is_dir():
        return []
    ex = _excluded_dirs()
    return [d.name for d in WIKI.iterdir()
            if d.is_dir() and not _skip(d.name) and d.name not in ex]


def _read_head(p: Path, limit: int = _FM_SCAN_BYTES) -> str:
    """Read up to `limit` bytes (frontmatter + first H1) so a full scan doesn't read big bodies."""
    try:
        with p.open("rb") as f:
            return f.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _disp_ts(v) -> str:
    """Display an OKF date/timestamp: ISO timestamp -> date + time; bare date stays; empty -> ''."""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else s


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
            "updated": _disp_ts(fm.get("last_updated") or fm.get("updated") or fm.get("created"))}


def _scan_dir(sub: str) -> list[dict]:
    """Page metadata for every page under wiki/<sub> (recursive). Cached for _BROWSE_TTL."""
    if sub in _excluded_dirs():
        return []
    now = time.monotonic()
    hit = _BROWSE_CACHE.get(sub)
    if hit and now - hit[0] < _BROWSE_TTL:
        return hit[1]
    base = (WIKI / sub).resolve()
    out: list[dict] = []
    if base.is_dir() and _within(WIKI, base):
        for p in base.rglob("*.md"):
            if _skip(p.name) or _hidden_page(p):   # reserved sub-dirs + walk-up excluded (batch-2 re-verify)
                continue
            out.append(_page_meta(p.resolve()))
    out.sort(key=lambda r: (r["title"].lower(), r["path"]))
    _BROWSE_CACHE[sub] = (now, out)
    return out


def _pages_of_types(types: frozenset) -> list[dict]:
    """Every page across all non-excluded namespaces whose `type` is in `types`."""
    out = [pg for sub in _top_dirs() for pg in _scan_dir(sub)
           if pg["type"].lower() in types]
    out.sort(key=lambda r: (r["title"].lower(), r["path"]))
    return out


def _dir_is_derived(md_paths: list[Path]) -> bool:
    """A namespace is 'derived' when its pages are generated artifacts (type: dashboard) rather
    than curated knowledge. Decided by sampling a few pages' frontmatter `type`."""
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


def _ns_about(dir: str) -> str:
    """Rendered HTML of an optional wiki/<dir>/_about.md — a namespace description card shown
    above the page list. Empty when absent (`_`-prefixed, so _skip() keeps it out of the list)."""
    if not dir:
        return ""
    p = (WIKI / dir / "_about.md").resolve()
    if not (p.is_file() and _within(WIKI, p)):
        return ""
    try:
        _, body = split_fm(p.read_text(encoding="utf-8", errors="ignore"))
        return render_md(body)
    except OSError:
        return ""


@app.get("/api/tree")
def api_tree():
    """Top-level directories under wiki/ with page counts — the browse rail. Each dir is
    flagged `derived` (generated content) vs curated knowledge."""
    dirs = []
    if WIKI.is_dir():
        excluded = _excluded_dirs()
        for d in sorted(WIKI.iterdir()):
            if not d.is_dir() or _skip(d.name) or d.name in excluded:
                continue
            mds = [p for p in d.rglob("*.md") if not _skip(p.name) and not _hidden_page(p)]
            if mds:
                dirs.append({"dir": d.name, "count": len(mds), "derived": _dir_is_derived(mds)})
    label, ns = _rail_top_section()
    present = {d["dir"] for d in dirs}
    top = [n for n in ns if n in present]
    return {"vault": str(WIKI), "dirs": dirs,
            "top_section": {"label": label, "namespaces": top}}


@app.get("/api/groups")
def api_groups():
    """Pack-declared display groups (label -> page count) — browse entities BY KIND across
    namespaces. Empty when the pack declares none."""
    return {"groups": [{"label": label, "count": len(_pages_of_types(types))}
                       for label, types in _display_groups()]}


@app.get("/api/pages")
def api_pages(dir: str = Query(default=""), group: str = Query(default="")):
    """Pages under a top-level directory, OR (with ?group=Label) every page whose `type` is in
    that display group, across namespaces."""
    if group:
        for label, types in _display_groups():
            if label == group:
                return {"group": group, "pages": _pages_of_types(types)}
        raise HTTPException(404, "unknown group")
    if "/" in dir or ".." in dir or dir.startswith((".", "/")):
        raise HTTPException(400, "bad dir")
    return {"dir": dir, "about": _ns_about(dir), "pages": _scan_dir(dir)}


# ── about (deployment identity, ported from okengine-reader) ─────────────────
def _about_info() -> dict:
    """Deployment identity for the About panel: vault name + version (pack.yaml) and the
    engine/Hermes pins. Read fresh — both files are tiny and About is cold."""
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
    # Deployment purpose + composition — derived from the state files the installer
    # and extensions-enable already maintain (mirrors okengine-reader/_about_info;
    # keep the two in sync).
    info["description"] = str(pk.get("description") or "")
    info["mission"] = str(pk.get("mission") or "")
    try:
        cm = (VAULT / "CLAUDE.md").read_text(encoding="utf-8") \
            if (VAULT / "CLAUDE.md").is_file() else ""
        info["installed_domains"] = [ln[len("## Installed domain:"):].strip()
                                     for ln in cm.splitlines()
                                     if ln.startswith("## Installed domain:")]
    except OSError:
        info["installed_domains"] = []
    try:
        info["sub_domains"] = sorted(d.name for d in WIKI.iterdir()
                                     if d.is_dir() and (d / "schema.yaml").is_file())
    except OSError:
        info["sub_domains"] = []
    # Prefer the GENERATED effective set (opt-ins + core default-ons, written by
    # the deploy's stage-plan) — the enabled-state file lists opt-ins only, which
    # under-reported core extensions (a fleet running 3 showed 1 in About).
    eff = _yaml(VAULT / ".okengine" / "extensions-effective.yaml")
    if isinstance(eff.get("effective"), list) and eff["effective"]:
        # entries are {id,name,description} (or legacy plain ids) — normalize to dicts
        exts = []
        for x in eff["effective"]:
            if isinstance(x, dict):
                exts.append({"id": str(x.get("id") or ""),
                             "name": str(x.get("name") or x.get("id") or ""),
                             "description": str(x.get("description") or "")})
            else:
                exts.append({"id": str(x), "name": str(x), "description": ""})
        info["extensions"] = sorted(exts, key=lambda e: e["id"])
    else:
        ext = _yaml(VAULT / ".okengine" / "extensions.yaml")
        ids = sorted((ext.get("enabled") or {}).keys()) \
            if isinstance(ext.get("enabled"), dict) else []
        info["extensions"] = [{"id": i, "name": i, "description": ""} for i in ids]
    ev = _yaml(VAULT / "engine.version")
    # Prefer the deploy-stamped runtime marker (the ACTUAL engine/Hermes running) over the pack's
    # DECLARED pins, which can be stale vs the deployed engine. Fall back to the declared pin.
    rt = _yaml(VAULT / ".hermes-data" / "engine-runtime.yaml")
    info["engine_version"] = str(rt.get("engine_release") or ev.get("version") or "")
    info["hermes_pin"] = str(rt.get("hermes_pin") or ev.get("hermes_pin") or "")
    info["project_url"] = os.environ.get("OKENGINE_PROJECT_URL") or str(pk.get("project_url") or "")
    return info


@app.get("/api/about")
def api_about():
    """Vault name + engine/Hermes versions for the About panel."""
    info = _about_info()
    info["chat_enabled"] = _chat_enabled()
    return info


# ── agent chat (relay to the Hermes OpenAI-compatible api_server) ────────────
# The cockpit runs NO model of its own. The Chat tab relays to THE agent (Hermes), which
# answers by NAVIGATING the OKF wiki via its graph tools — the wiki-as-memory demonstration,
# the deliberate counter to RAG. Configured by env so a deployment without an agent endpoint
# simply never shows the tab.
_AGENT_API = os.environ.get("OKENGINE_AGENT_API", "").rstrip("/")
_AGENT_KEY = os.environ.get("OKENGINE_AGENT_KEY", "")
_AGENT_MODEL = os.environ.get("OKENGINE_AGENT_MODEL", "OKEngine Agent")


def _intenv(name: str, default: int, lo: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, v)


_CHAT_MAX_MSGS = _intenv("OKENGINE_READER_CHAT_MAX_MSGS", 24, lo=2)
_CHAT_MAX_CHARS = _intenv("OKENGINE_READER_CHAT_MAX_CHARS", 8000, lo=200)

# Grounding contract for the chat session (server-controlled — the browser cannot override it).
# The vault is the agent's memory and the FIRST stop; external research is written BACK so the
# corpus compounds. This is the OKF/agent-wiki thesis, not RAG.
_AGENT_SYSTEM = os.environ.get("OKENGINE_AGENT_SYSTEM") or (
    "You are the OKEngine vault agent. This OKF knowledge vault is your long-term memory and "
    "the FIRST place you look for anything. Open EVERY reply with a one-line acknowledgement "
    "of what you're about to do (e.g. \"Checking the vault for Scattered Spider…\") before you "
    "call any tools, so the user gets immediate feedback. Keep it to that ONE line — do not narrate "
    "each search round (\"good leads\", \"pulling the pages now\", \"good data\"). Then:\n"
    "1. SEARCH THE VAULT FIRST — use your tools (search, then get_page / retrieve_context / "
    "find_references) and build your answer from those pages. Search is lexical, so it matches "
    "words not meanings: if the first query is thin, RETRY with synonyms and related terms "
    "before concluding the vault lacks it (e.g. health → medical / clinical / hospital / "
    "patient; actor → group / intrusion-set / threat-actor; ransomware → extortion). Prefer the "
    "most RECENT pages — the vault is fed continuously, so current-year material exists; lead with "
    "it and don't lean on old advisories when fresher reporting is present. Cite each page you use "
    "as a linked title — `[Page Title](path)`, e.g. `[Scattered Spider](entities/s/scattered-spider)` "
    "— never a bare file path.\n"
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
    "to confirm it.\n"
    "5. Speak as the vault's own analyst, never as software. Do NOT name or describe the machinery "
    "behind you: never mention Hermes, your model or model provider, or the tools/functions you use "
    "(search, web research, retrieve_context, write tools, and the like), and do not sign a reply "
    "or report off as any \"agent\". Referring to THE VAULT and citing your sources is expected — "
    "describe WHAT you found and WHERE (linked page titles, web sources), never the plumbing that "
    "fetched it.\n"
    "6. Be specific and disciplined. Surface the concrete detail the pages hold — dates, CVEs, "
    "IOCs, named techniques/TTPs — not generic advice; state the time window your assessment covers "
    "and say so plainly if the freshest evidence is old. Stay within the question's scope: if you "
    "raise an adjacent but DISTINCT threat (different actor class or motivation), label it as "
    "context, don't blend it into the main assessment.\n"
    "7. Only when asked for a REPORT, BRIEFING, or DECK (not a quick question): make it a "
    "SELF-CONTAINED document that BEGINS at its title / executive summary — your search-and-pull "
    "narration must NOT appear anywhere in it. Structure it: a short impact-framed executive "
    "summary, comparison TABLES where you contrast actors/options, and a specific "
    "detection/mitigation section drawn from the vault. Keep ordinary questions concise."
)


def _chat_enabled() -> bool:
    return bool(_AGENT_API and _AGENT_KEY)


@app.post("/api/chat")
async def api_chat(request: Request):
    """Relay an OpenAI-style chat turn to the Hermes agent and stream its SSE back. The upstream
    key is held server-side; the client only ever sees the token stream."""
    if not _chat_enabled():
        raise HTTPException(503, "agent chat not configured")
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


# ── shell ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # Cache-bust app.js/style.css by content hash (ported from the reader): bare URLs let the
    # browser serve a stale UI from heuristic cache after a cockpit update. The ?v=<hash> only
    # changes when the asset changes, so unchanged assets still cache; changed ones are fetched
    # immediately.
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
