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

# Trust enforcement (okengine#90 P4a): a PRIVATE vault must never be served unauthenticated when
# publicly exposed. `OKENGINE_TRUST` is the deployment's pack trust (derived from pack.yaml `trust`);
# `OKENGINE_BIND` is the intended host exposure. Default trust=private (the engine's pack default) →
# fail-safe: refuse to start rather than expose a private vault to the network without a password.
_TRUST = os.environ.get("OKENGINE_TRUST", "private").strip().lower()
_BIND_HOST = os.environ.get("OKENGINE_BIND", "127.0.0.1").strip()
if _TRUST == "private" and _BIND_HOST not in ("", "127.0.0.1", "localhost", "::1") and not _READER_PASSWORD:
    raise SystemExit(
        f"okengine-reader REFUSED to start: PRIVATE vault exposed on {_BIND_HOST!r} with no "
        f"OKENGINE_READER_PASSWORD. Set a reader password, bind to loopback "
        f"(OKENGINE_BIND=127.0.0.1), or declare the pack `trust: public`. (okengine#90 P4a)")


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


_LINK_TITLE_CACHE: dict = {}


def _link_title(target: str) -> str | None:
    """The target page's human title (frontmatter `title`/`name`) for friendlier wikilink text —
    so a citation reads 'EvilTokens: a phishing attack…' instead of the raw slug
    `eviltokens-a-phishing-attack…`. Resolves a flat-form target to its sharded page (basename).
    Returns None when there's no real title (caller falls back to the slug). Cached for the
    process lifetime (titles are stable; the reader restarts on deploy)."""
    target = (target or "").strip()
    if not target or "://" in target:
        return None
    if target in _LINK_TITLE_CACHE:
        return _LINK_TITLE_CACHE[target]
    title = None
    key = target[:-3] if target.endswith(".md") else target
    try:
        cand = (WIKI / (key + ".md")).resolve()
        hit = cand if (cand.is_file() and _within(WIKI, cand)) else None
        if hit is None:
            hit = _resolve_basename(Path(key).name + ".md")
        if hit is not None:
            fm, _ = split_fm(_read_head(hit))
            title = (str(fm.get("title") or fm.get("name") or "").strip()) or None
    except OSError:
        pass
    _LINK_TITLE_CACHE[target] = title
    return title


def _wl_display(m) -> str:
    """Display text for a wikilink: alias, else the target page's title, else its last segment."""
    alias = (m.group(3) or "").strip()
    if alias:
        return alias
    target = (m.group(1) or "").strip()
    if target:
        return _link_title(target) or target.split("/")[-1]
    return (m.group(2) or "").strip()


def _delink(s: str) -> str:
    """Render a wikilink as plain display text (for portable markdown export)."""
    return _WIKILINK.sub(_wl_display, s)


# `[APT41](entities/a/apt41)` — an INTERNAL vault link (the agent's linked-title citations). Not an
# image (`!` excluded), not external (http/mailto/# excluded).
_MD_LOCAL_LINK = re.compile(r"(?<!\!)\[([^\]\n]+)\]\((?!https?://|mailto:|#)[^)\n]*\)")


def _deref_local_links(s: str) -> str:
    """Flatten internal markdown links to their text for portable export — `[APT41](entities/a/apt41)`
    -> `APT41`. External http(s)/mailto links are kept. Vault paths resolve only inside the reader,
    so an exported md/docx/pdf must not carry them as dead links."""
    return _MD_LOCAL_LINK.sub(r"\1", s)


_EMBED_PATH_CACHE: dict = {}


def _resolve_basename(name: str) -> "Path | None":
    """Resolve a bare basename (`slug.md`) to its CANONICAL page, exactly as _resolve_page does: skip
    generated/reserved files, then on a multi-hit DROP schema-excluded namespaces and PREFER the
    entities/ page (a multi-source entity also has observations/<src>/… copies with the same slug),
    then require uniqueness. Shared by the embed + link-title resolvers so all three agree — a naive
    len==1 gate rendered a multi-source entity as "missing" (invariant-audit #16 / L7)."""
    if not WIKI.is_dir():
        return None
    hits = [p for p in WIKI.rglob(name) if not _skip(p.name)]
    if len(hits) > 1:
        excl = _excluded_dirs()
        pref = [p for p in hits if not (_ns_dirs(p) & excl)] or hits
        ent = [p for p in pref if "entities" in _ns_dirs(p)]
        hits = ent or pref
    return hits[0] if len(hits) == 1 else None


def _embed_rglob(name: str) -> "Path | None":
    """Canonical vault match for a basename `name` (`…​.md`), memoized for the process lifetime
    (mirrors `_LINK_TITLE_CACHE`). Basename embeds ![[apt29]] are the norm on sharded OKF
    vaults, so without this each render re-walks the WHOLE tree once per unresolved embed."""
    if name in _EMBED_PATH_CACHE:
        return _EMBED_PATH_CACHE[name]
    hit = _resolve_basename(name)
    _EMBED_PATH_CACHE[name] = hit
    return hit


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
            cand = _embed_rglob(Path(target).name + ".md")
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
    # inline charts (okengine.viz panel-svg blocks): static SVG shapes/text ONLY —
    # no script/foreignObject/animate/use/href, so nothing here can execute or fetch.
    "svg", "rect", "line", "circle", "text",
}
_SVG_PRESENTATION = {"fill", "stroke", "stroke-width", "stroke-dasharray",
                     "font-size", "font-weight", "font-style", "text-anchor", "opacity"}
_ALLOWED_ATTRS = {
    "a": {"href", "title", "class", "data-page", "target"},
    "img": {"src", "alt", "title"},
    "td": {"align"},
    "th": {"align"},
    "code": {"class"},
    "span": {"class"},
    "svg": {"xmlns", "viewBox", "width", "style"},
    "rect": {"x", "y", "width", "height"} | _SVG_PRESENTATION,
    "line": {"x1", "y1", "x2", "y2"} | _SVG_PRESENTATION,
    "circle": {"cx", "cy", "r"} | _SVG_PRESENTATION,
    "text": {"x", "y", "transform"} | _SVG_PRESENTATION,
}


# okengine.viz panel-svg blocks must bypass MARKDOWN (not the sanitizer): the nl2br
# extension injects <br/> between the shape lines, and <br> is an HTML5 foreign-content
# BREAKOUT tag — a spec-following sanitizer parser (nh3/ammonia >= 0.3.6) closes the
# <svg> at the first <br> and every shape after it is silently dropped. The blocks are
# stashed before markdown and re-inserted BEFORE nh3.clean, so the svg still gets the
# full _ALLOWED_TAGS/_ALLOWED_ATTRS pass (script tags / event handlers stripped as ever).
_PANEL_SVG_RE = re.compile(r"<!--\s*panel-svg\b.*?<!--\s*/panel-svg\s*-->", re.DOTALL)


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
    stash: list[str] = []

    def _stash(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"OKENGINEPANELSVG{len(stash) - 1}MARKER"

    body = _PANEL_SVG_RE.sub(_stash, body)
    body = _uncode_wikilinks(body)
    body = _linkify(body)
    html = md.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    for i, blk in enumerate(stash):
        html = html.replace(f"OKENGINEPANELSVG{i}MARKER", blk)
    html = _link_originals(html)
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)


_SRC_WL = re.compile(r'<a class="wl" data-page="(sources/[^"]+)"[^>]*>([^<]*)</a>')


def _link_originals(html: str) -> str:
    """A cited source page carries the ORIGINAL article's `url:` in its frontmatter. Promote the
    citation so its TITLE links STRAIGHT to that article — one click reaches the primary
    reporting, instead of the title pointing at the internal source stub with the real url
    demoted to a small glyph. Falls back to the internal wikilink only when the source has no
    http(s) `url:`. Runs BEFORE nh3.clean: the anchor uses allowlisted attrs (href/target/class)
    and nh3 stamps rel=noopener itself. Mirrors the cockpit's treatment — both UIs, same
    affordance."""
    def _add(m):
        whole, rel, text = m.group(0), m.group(1), m.group(2)
        try:
            fp = (WIKI / (rel + ".md")).resolve()
            if not str(fp).startswith(str(WIKI.resolve())) or not fp.is_file():
                return whole
            fm_m = re.match(r"\A---\s*\n(.*?\n)---", fp.read_text(encoding="utf-8", errors="replace"), re.S)
            u = ""
            if fm_m:
                um = re.search(r"^url:\s*(\S+)", fm_m.group(1), re.M)
                if um:
                    u = um.group(1).strip("'\"")
        except OSError:
            return whole
        if u.startswith(("http://", "https://")):
            return (f'<a class="ext" href="{u}" target="_blank" '
                    f'title="original article">{text}</a>')
        return whole
    return _SRC_WL.sub(_add, html)


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


def _ns_dirs(p: "Path") -> frozenset:
    """The directory components of a page's vault-relative path (filename dropped). In a flat vault
    the namespace is parts[0]; in a WALK-UP co-installed vault it is nested under a sub-domain
    container (wiki/<subdomain>/<namespace>/…, okengine#173), so a parts[0]-only check missed the
    excluded-namespace drop + entities preference and a multi-source entity in a sub-domain 409'd /
    vanished (invariant-audit HIGH). Matching ALL dir components is layout-agnostic — namespace names
    (entities, observations, …) never collide with shard letters or slugs."""
    try:
        return frozenset(p.relative_to(WIKI).parts[:-1])
    except ValueError:
        return frozenset()


def _is_reserved_seg(seg: str) -> bool:
    """A reserved DIRECTORY segment (`_archive`/`.git`-style). A BARE `_` is NOT reserved — it's the
    engine's reshard second-letter bucket for a non-alnum slug (entities/x/_/x-force.md), a legitimate
    canonical location that must stay visible everywhere (batch-2 re-verify over-drop)."""
    return len(seg) > 1 and seg.startswith(("_", "."))


def _reserved_seg(p: "Path") -> bool:
    """True if any DIRECTORY segment under wiki/ is a reserved (`_archive/`-style) dir that leaf-only
    `_skip(p.name)` misses. The discovery surfaces (browse count + `_scan_dir` ledger + observations)
    must hide these so they AGREE with search's ripgrep `!_*` pruning (invariant-audit batch-2)."""
    return any(_is_reserved_seg(seg) for seg in _ns_dirs(p))


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


_BL_DROP_CACHE: tuple = (0.0, None)


def _backlink_drop_dirs() -> frozenset[str]:
    """Namespaces dropped from the backlink graph in BOTH directions — schema.yaml
    `backlink_drop:` when present (pack knob; `[]` re-includes sources), else the default
    {'sources'}. MIRRORS backlink_lib._BACKLINK_DROPPED / excluded_top_dirs (keep in sync).
    Cached (vault :ro)."""
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
    # Engine default (okengine briefings-by-default): briefings/ is the first-class brief
    # namespace, so pin a "Briefs" rail section when the pack declares no rail_top_section.
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
            "updated": _disp_ts(fm.get("last_updated") or fm.get("updated") or fm.get("created"))}


def _disp_ts(v) -> str:
    """Display an OKF envelope date/timestamp: an ISO-8601 timestamp renders date + time
    (`2026-06-28 14:30:00`, T->space, trailing Z dropped); a bare date stays as-is. Empty -> ''.
    Prefers `last_updated` (the engine's actual update field) over `updated`/`created`."""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else s


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
    excluded = _excluded_dirs()
    if base.is_dir() and _within(WIKI, base):
        for p in base.rglob("*.md"):
            # drop reserved leaves, reserved sub-dirs (_archive/…), AND pages crossing an excluded
            # namespace nested under a walk-up sub-domain — so this served ledger AGREES with the
            # api_tree count AND search's `!_*`/`!**/{ns}/**` pruning (M-1310 + batch-2 re-verify).
            if _skip(p.name) or _reserved_seg(p) or (_ns_dirs(p) & excluded):
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
    # Deployment PURPOSE + composition (okengine#177-class ask: "what is this wiki,
    # what's installed?"). All DERIVED from state files that install-domain /
    # extensions-enable already maintain — nothing here is hand-written for About:
    #   description/mission  pack.yaml (declared; validate WARNs when absent)
    #   installed_domains    the '## Installed domain:' markers in the deployment
    #                        CLAUDE.md (the installer's provenance convention)
    #   sub_domains          walk-up subtrees (wiki/*/schema.yaml)
    #   extensions           enabled ids (.okengine/extensions.yaml)
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
    # Prefer the deploy-stamped runtime marker (the ACTUAL engine/Hermes running, written by
    # ensure-runtime) over the pack's DECLARED engine.version pins, which can be stale/wrong vs
    # the deployed engine — a pack pinned to an older engine still deploys on a newer one, and
    # its hermes_pin then reports the wrong runtime (okengine#119). Fall back to the declared pin.
    rt = _yaml(VAULT / ".hermes-data" / "engine-runtime.yaml")
    info["engine_version"] = str(rt.get("engine_release") or ev.get("version") or "")
    info["hermes_pin"] = str(rt.get("hermes_pin") or ev.get("hermes_pin") or "")
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


def _budget_tripped() -> bool:
    """True while budget_guard has tripped the model-token budget. The guard's only actuator was
    cron-plus pause; chat relayed to the gateway api_server unbounded (chat sessions DO count against
    the budget). The guard now drops a marker into the SHARED vault (the reader mounts it, not the
    gateway /opt/data), so /api/chat honors the same trip that pauses the crons (invariant-audit #37)."""
    try:
        return (VAULT / ".okengine" / "budget-paused").exists()
    except OSError:
        return False


@app.post("/api/chat")
async def api_chat(request: Request):
    """Relay an OpenAI-style chat turn to the Hermes agent and stream its SSE back. The
    upstream key is held server-side; the client only ever sees the token stream."""
    if not _chat_enabled():
        raise HTTPException(503, "agent chat not configured")
    if _budget_tripped():
        raise HTTPException(503, "agent chat paused — the deployment is over its model-token budget "
                                 "(budget-guard). It resumes when usage ages back under budget, or "
                                 "after `framework budget --resume`.")
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
            # Drop reserved sub-dirs (_archive/…) AND excluded namespaces nested under a walk-up
            # sub-domain, so the browse count matches the _scan_dir ledger + search (M-1310 + re-verify).
            mds = [p for p in d.rglob("*.md")
                   if not _skip(p.name) and not _reserved_seg(p) and not (_ns_dirs(p) & excluded)]
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
    return {"dir": dir, "about": _ns_about(dir), "pages": _scan_dir(dir)}


def _ns_about(dir: str) -> str:
    """Rendered HTML of an optional ``wiki/<dir>/_about.md`` — a namespace description card
    shown above the ledger (what this namespace/extension is, why it's here, what its pages
    contain). Lets a less-known extension like okengine.lacuna explain itself in the UI with
    zero per-extension reader code. Empty when absent. ``_about.md`` is ``_``-prefixed so
    _skip() already keeps it out of the ledger/render."""
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
            pref = [p for p in hits if not (_ns_dirs(p) & excl)] or hits   # walk-up-aware (invariant-audit HIGH)
            ent = [p for p in pref if "entities" in _ns_dirs(p)]
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
# they don't bury the intel. Domain-agnostic (OKF envelope + assembler provenance). Includes
# COMPOSITION provenance — `maintained_by` (which pack(s) have written the page) + `discovered_by`
# (first attributor), stamped by the write path from OKENGINE_PACK (okengine#90 P3) — rendered with
# clean "Maintained by" / "Discovered by" labels; most useful in a composed multi-pack vault.
_META_SECONDARY = {"tlp", "created", "updated", "last_updated", "last_seen", "first_seen",
                   "assembled_from", "tier", "tlp_caveat",
                   "maintained_by", "discovered_by", "created_by", "last_modified_by"}
# `conflicts` + `needs_review` get a dedicated provenance view (api_page), not a raw row.


# ── multi-source conflict / provenance view (okengine#42) ────────────────────
_SRC_REL_CACHE: tuple[float, dict] = (float("-inf"), {})   # -inf, not 0.0 — see the note at _DIR_TTL
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
    conflicts = fm.get("conflicts")
    if not isinstance(conflicts, list):     # `conflicts: 42` -> `for c in 42` TypeError (M28)
        conflicts = []
    for c in conflicts:
        if not isinstance(c, dict):
            continue
        headline = c.get("headline")
        vals: list[dict] = []
        values = c.get("values")
        # Guard BOTH the container and each entry: `values: 42` is a non-iterable scalar (TypeError
        # in the loop header), `values: [high, medium]` / `values: high` are scalar entries (.get()
        # AttributeError). `conflicts` is in _OKF_ALWAYS so the write path shape-checks none of it —
        # a malformed value reaches the page view unguarded and 500s it (invariant-audit M28).
        if not isinstance(values, list):
            values = []
        for v in values:
            if not isinstance(v, dict):
                continue
            v_sources = v.get("sources")            # third container: `sources: 42` -> for s in 42 (M28)
            if not isinstance(v_sources, list):
                v_sources = []
            srcs = [{"name": str(s), "reliability": rel.get(str(s), "")} for s in v_sources]
            rank = max((_REL_RANK.get(str(s["reliability"]).upper()[:1], -1) for s in srcs),
                       default=-1)
            vals.append({"value": _val_text(v.get("value")), "sources": srcs, "rank": rank,
                         "is_headline": v.get("value") == headline})
        out.append({"field": str(c.get("field") or ""), "headline": _val_text(headline),
                    "values": vals})
    return out


_OBS_INDEX_CACHE: tuple[float, dict] = (float("-inf"), {})   # -inf, not 0.0 — see the note at _DIR_TTL


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
            if _skip(p.name) or _reserved_seg(p):   # skip _archive/ retired observations (batch-2 twin of cockpit)
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


def _ref_target(s: str) -> str | None:
    """If `s` is a wiki-relative path (`<namespace>/.../<slug>`) that resolves to a vault page,
    return its canonical wiki-relative key (no `.md`) for an internal link; else None. This
    linkifies the plain-path reference values (see_also / field_mapped / prediction_candidate /
    …) the normalized write path now stores — so the fact-sheet's refs are clickable, not text.
    Path-shaped only (must contain '/'), and the basename fallback resolves a flat-form ref to a
    sharded page (e.g. entities/foo -> entities/f/foo)."""
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
    """One frontmatter value -> display chips. http(s) scalars and list items carrying a
    url/href (e.g. refs/rels) become external links; a value that resolves to a vault page
    becomes an internal page link (`page`); dicts compact to k=v."""
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


# ── reader UI extension panels (okengine#160) ────────────────────────────────
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
            "meta": m["primary"], "meta_aux": m["secondary"], "panel": _panel_for(fm, body),
            "provenance": _provenance(fm, body),
            "conflicts": _shape_conflicts(fm), "needs_review": bool(fm.get("needs_review")),
            "observations": _observations_by_canonical().get(slug, [])}


def _provenance(fm: dict, body: str) -> dict:
    """Trust signals surfaced as a per-page strip (okengine#70): how grounded the page is (cited
    source PAGES vs total), the Tier-2 grounding-check tally (supported / unsupported claims), and
    whether a human has signed off. All from data the trust lanes already write."""
    srcs = fm.get("sources")
    srcs = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
    page_srcs = sum(1 for s in srcs if "/" in str(s) or str(s).lower().endswith(".md"))
    grounding = None
    g = re.search(r"##\s+Grounding check(.*?)(?:\n##\s|\Z)", body, re.S | re.I)
    if g:
        seg = g.group(1)
        grounding = {"supported": len(re.findall(r"\*\*\s*supported", seg, re.I)),
                     "unsupported": len(re.findall(r"\*\*\s*(?:unsupported|not[- ]found|contradict)", seg, re.I))}
    return {"sources": len(srcs), "source_pages": page_srcs,
            "reviewed_by": fm.get("reviewed_by"), "reviewed_on": fm.get("reviewed_on"),
            "needs_review": bool(fm.get("needs_review")), "grounding": grounding}


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
    # str-wrap: yaml SafeLoader type-infers a bare `title: 2024`/`2026-07-08`/list
    # to a non-str, and .strip() would 500 the export (matches api_page's str-wrap).
    t = str(title or fm.get("title") or fm.get("name") or "").strip()
    if t and not body.lstrip().startswith("# "):
        body = f"# {t}\n\n{body}"
    return body.strip() + "\n"


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
        # A non-empty title keeps standalone HTML/docx out of pandoc's "Defaulting
        # to 'in' as the title" fallback (the temp filename stem leaking through).
        cmd += ["--metadata", f"title={(title or '').strip() or 'OKEngine page'}"]
        if fmt == "docx":
            cmd += ["--standalone"]
        if fmt == "pdf":
            cmd += ["--pdf-engine=weasyprint"]
            hdr = Path(td) / "style.html"
            hdr.write_text(f"<style>{_PDF_CSS}</style>", encoding="utf-8")
            cmd += ["--standalone", "-H", str(hdr)]
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
    title = str(fm.get("title") or fm.get("name") or Path(path).stem).strip()
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
    # `!_?*` (underscore + ≥1 char), NOT `!_*` — the latter also prunes the engine reshard
    # second-letter bucket `entities/x/_/x-force.md` (a bare `_` dir), making a legit resharded entity
    # browsable-but-unfindable. Mirrors _is_reserved_seg's bare-`_` exemption (batch-2 gate).
    ignore = ["!*.bak.*", "!_?*", "!INDEX.md", "!index.md", "!INDEX-*", "!index-*"]
    # `!**/{d}/**` (any depth), NOT `!{d}/**` (root-anchored): in a WALK-UP co-installed vault the
    # excluded namespace lives at <subdomain>/observations/… and a root-anchored glob leaks it into
    # search results (invariant-audit M-1310). The leading **/ still matches the root case.
    ignore += [f"!**/{d}/**" for d in _excluded_dirs()]
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


# ── backlinks (knowledge-graph: "what links here") ─────────────────────────
# The cron-precomputed wiki/.backlinks.json (below) is served directly. The live FALLBACK builds
# the graph by scanning [[wikilinks]] over the vault directly (okengine#179) — it no longer shells
# iwe. Invert forward-refs into a {target: [referrers]} map, cache with a TTL. Read-only mount.
_BACKLINKS: dict = {"map": None, "ts": 0.0}
_BACKLINKS_TTL = limits.intenv("OKENGINE_BACKLINKS_TTL", 86400, lo=60)  # 24h default — the fallback
# scan is cheap, but backlinks change over days, so a day-stale "what links here" is fine.
_BL_LOCK = threading.Lock()

# Cron-precomputed graph (okengine#168): the `backlinks-refresh` engine cron
# writes the inverted+filtered+titled map to wiki/.backlinks.json once per
# deployment per day (scripts/cron/backlink_lib.py is the canonical filter
# logic). When that artifact is present and fresh we serve it directly and
# never run iwe in this container; the live build below survives ONLY as the
# fallback for a missing/stale artifact (deployment without the cron, or a
# stopped cron). Ceiling default 48h = two missed daily runs.
_BL_ARTIFACT_MAX_AGE = limits.intenv("OKENGINE_BACKLINKS_MAX_AGE", 172800, lo=3600)
_BL_ARTIFACT: dict = {"map": None, "mtime": None}

# Generated root artifacts _skip() doesn't catch (they live at the vault root, not as
# INDEX*/underscore files): the agent's HOT set and the write log. Excluded as backlink
# SOURCES so their markdown links don't pollute "what links here".
_RESERVED_BL_NAMES = frozenset({"HOT.md", "log.md"})
def _skip_backlink_src(key: str) -> bool:
    """True if a backlink *source* document is generated/operational machinery that must
    not contribute "what links here" edges. IWE indexes the whole vault and doesn't apply
    our filters, so we re-apply the same exclusions the rail uses: `_skip()` (INDEX*/
    `_*`/backups), an `exclude:`-ed namespace (e.g. operational/), and the root artifacts
    HOT.md/log.md, plus generated dashboards/ (surfaced for READING in #117 but its digests
    aren't real edges) and the raw-ingest sources/ tree (_BACKLINK_DROPPED_NS). entities,
    concepts, briefings, … are kept."""
    name = key.split("/")[-1]
    if not name.endswith(".md"):
        name += ".md"
    if _skip(name) or name in _RESERVED_BL_NAMES:
        return True
    parts = key.split("/")
    # A reserved sub-dir (_archive/…) at ANY depth: a leaf-only _skip lets an archived page contribute
    # "what links here" edges that browse + search hide — the discovery surfaces must agree (batch-2
    # re-verify). The engine guards the DIRECTORY (`'_archive' in p.parts`).
    if any(_is_reserved_seg(seg) for seg in parts[:-1]):
        return True
    # Browse-visibility and backlink-skip differ: dashboards/ is SURFACED for READING (okengine#117)
    # but its auto-generated digests aren't meaningful edges, so skip surfaced-derived dirs as backlink
    # SOURCES too. The backlink-drop set (sources/ by default; pack `backlink_drop:`) is dropped both
    # ways. Match at ANY depth so a walk-up <subdomain>/<ns>/ is caught, not just parts[0].
    drop = _excluded_dirs() | _SURFACED_DERIVED | _backlink_drop_dirs()
    return any(seg in drop for seg in parts[:-1])


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


# okengine#179: build the graph by scanning markdown links directly instead of shelling the
# heavy `iwe find -f json -l 0` full-graph dump (~4GB RSS / ~550s on a 52k-file vault). This
# MIRRORS scripts/cron/backlink_lib.scan_forward_refs (the cron builds the served artifact;
# this is only the missing/stale fallback) — keep the two in sync. Semantics: [[key]]/[[key|
# label]] and [text](path.md) are edges (label may wrap lines); frontmatter/code-span/external
# links are not; targets resolve by basename to the real key (exact wins; collision -> alpha-
# first; dangling kept).
_BL_FM = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)
_BL_WIKI = re.compile(r"\[\[([^\]]+?)\]\]", re.DOTALL)
_BL_MD = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?)\)")
_BL_FENCE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[^\n]*$", re.DOTALL | re.MULTILINE)
_BL_INLINE = re.compile(r"(`+)[^\n]*?\1")


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
    """Forward-reference scan over WIKI (iwe-parity). Mirrors backlink_lib.scan_forward_refs."""
    paths = list(WIKI.rglob("*.md"))
    keys = [p.relative_to(WIKI).as_posix()[:-3] for p in paths]
    keyset = set(keys)
    by_base: dict[str, list] = {}
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
    docs = _scan_forward_refs()
    bl: dict[str, list] = {}
    for d in docs:
        src = d.get("key")
        if not src or _skip_backlink_src(src):
            continue
        title = _backlink_title(src)
        for ref in d.get("references") or []:
            tgt = ref.get("key")
            # drop excluded namespaces (sources/, dashboards/, …) as TARGETS too, mirroring
            # backlink_lib.invert — so a raw-ingest page never accumulates a backlink list.
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
        h = hashlib.sha1(usedforsecurity=False)   # cache-bust asset digest, not a security hash
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
