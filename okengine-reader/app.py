#!/usr/bin/env python3
"""Standalone, domain-agnostic, read-only OKF vault reader."""
from __future__ import annotations

import os
import re
import hashlib
import threading
import time
import subprocess
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import limits
import about
import backlinks
import browse
import chat
import directory_index
import exports
import page_metadata
import rendering
import search
import settings
from auth import BasicAuth, exposure_refusal

VAULT = Path(os.environ.get("VAULT_DIR", "/vault"))
WIKI = VAULT / "wiki"

def _governing_schema_path(vault: Path = VAULT) -> Path:
    """Return the same schema authority enforced by the write path."""
    artifact = vault / ".okengine" / "composed-schema.yaml"
    return artifact if artifact.is_file() else vault / "schema.yaml"

STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def _lifespan(_app):
    """Start read-cache workers through FastAPI's supported lifespan contract."""
    _start_warmer()
    _prewarm_backlinks()
    yield

app = FastAPI(title="OKEngine · vault reader", docs_url=None, redoc_url=None,
              lifespan=_lifespan)

_BasicAuth = BasicAuth  # compatibility for service-local imports and tests

_READER_PASSWORD = os.environ.get("OKENGINE_READER_PASSWORD", "")
if _READER_PASSWORD:
    app.add_middleware(_BasicAuth, user=os.environ.get("OKENGINE_READER_USER", "okengine"),
                       password=_READER_PASSWORD)

_TRUST = os.environ.get("OKENGINE_TRUST", "private").strip().lower()
_BIND_HOST = os.environ.get("OKENGINE_BIND", "127.0.0.1").strip()
_EXPOSURE_REFUSAL = exposure_refusal(_TRUST, _BIND_HOST, _READER_PASSWORD)
if _EXPOSURE_REFUSAL:
    raise SystemExit(_EXPOSURE_REFUSAL)

_PUBLIC = settings.PUBLIC
_EXPORTS_ENABLED = settings.EXPORTS_ENABLED
_EXPORT_SEM = settings.EXPORT_SEMAPHORE
_SEARCH_SEM = settings.SEARCH_SEMAPHORE
_RATE = settings.RATE_LIMITER

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
_FM_RE = rendering._FM_RE
_WIKILINK = rendering._WIKILINK
_EMBED = rendering._EMBED
_H1_RE = rendering._H1_RE
_EMBED_PATH_CACHE = rendering._EMBED_PATH_CACHE
_LINK_TITLE_CACHE = rendering._LINK_TITLE_CACHE

def _renderer():
    rendering.configure(WIKI, _read_head, _excluded_dirs, _ns_dirs, _within)
    return rendering
def _skip(name: str) -> bool:
    return _renderer()._skip(name)
def _within(base: Path, path: Path) -> bool:
    try:
        path.relative_to(base.resolve())
        return True
    except ValueError:
        return False
def split_fm(text: str) -> tuple[dict, str]:
    return _renderer().split_fm(text)
def _link_title(target: str) -> str | None:
    return _renderer()._link_title(target)
def _wl_display(match) -> str:
    return _renderer()._wl_display(match)
def _delink(text: str) -> str:
    return _renderer()._delink(text)
def _deref_local_links(text: str) -> str:
    return _renderer()._deref_local_links(text)
def _resolve_basename(name: str) -> Path | None:
    return _renderer()._resolve_basename(name)
def _embed_rglob(name: str) -> Path | None:
    return _renderer()._embed_rglob(name)
def _resolve_embeds(text: str, depth: int = 0) -> str:
    return _renderer()._resolve_embeds(text, depth)
def _linkify(text: str) -> str:
    return _renderer()._linkify(text)
def _uncode_wikilinks(text: str) -> str:
    return _renderer()._uncode_wikilinks(text)
def render_md(body: str) -> str:
    return _renderer().render_md(body)
def _link_originals(html: str) -> str:
    return _renderer()._link_originals(html)
_DIR_CACHE: dict[str, tuple[float, list[dict]]] = {}
_DIR_TTL = 900.0          # seconds
_WARM_INTERVAL = 600.0    # background cache-refresh cadence (< _DIR_TTL)
_EXCLUDE_CACHE: tuple[float, frozenset[str]] = (float("-inf"), frozenset())
_GROUPS_CACHE: tuple[float, list[tuple[str, frozenset[str]]]] = (float("-inf"), [])
_RAILTOP_CACHE: tuple[float, tuple[str, tuple[str, ...]]] = (float("-inf"), ("", ()))

_SURFACED_DERIVED = frozenset({"dashboards"})

def _ns_dirs(p: "Path") -> frozenset:
    return directory_index.namespace_dirs(p, WIKI)

def _is_reserved_seg(seg: str) -> bool:
    return directory_index.is_reserved_segment(seg)

def _reserved_seg(p: "Path") -> bool:
    return directory_index.reserved_path(p, WIKI)

def _excluded_dirs() -> frozenset[str]:
    global _EXCLUDE_CACHE
    result, _EXCLUDE_CACHE = directory_index.excluded_namespaces(
        cache=_EXCLUDE_CACHE, ttl=_DIR_TTL, schema_path=_governing_schema_path,
        yaml_module=yaml, surfaced=_SURFACED_DERIVED,
    )
    return result

_BL_DROP_CACHE: tuple = (0.0, None)

def _backlink_drop_dirs() -> frozenset[str]:
    global _BL_DROP_CACHE
    result, _BL_DROP_CACHE = directory_index.backlink_drop_namespaces(
        cache=_BL_DROP_CACHE, ttl=_DIR_TTL, schema_path=_governing_schema_path,
        yaml_module=yaml,
    )
    return result

def _display_groups() -> list[tuple[str, frozenset[str]]]:
    global _GROUPS_CACHE
    result, _GROUPS_CACHE = directory_index.display_groups(
        cache=_GROUPS_CACHE, ttl=_DIR_TTL, schema_path=_governing_schema_path,
        yaml_module=yaml,
    )
    return result

def _rail_top_section() -> tuple[str, tuple[str, ...]]:
    global _RAILTOP_CACHE
    result, _RAILTOP_CACHE = directory_index.rail_top(
        cache=_RAILTOP_CACHE, ttl=_DIR_TTL, schema_path=_governing_schema_path,
        yaml_module=yaml, wiki=WIKI,
    )
    return result

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
           if pg["type"].lower() in types
           and pg.get("status", "").lower() != "tombstoned"]
    out.sort(key=lambda r: (r["title"].lower(), r["path"]))
    return out
_FM_SCAN_BYTES = 16384

def _read_head(p: Path, limit: int = _FM_SCAN_BYTES) -> str:
    return directory_index.read_head(p, limit)

def _page_meta(p: Path) -> dict:
    return directory_index.page_meta(
        p,
        wiki=WIKI,
        split_frontmatter=split_fm,
        read=_read_head,
        h1_re=_H1_RE,
        display_time=_disp_ts,
    )

def _disp_ts(v) -> str:
    return directory_index.display_timestamp(v)

def _scan_dir(sub: str, force: bool = False) -> list[dict]:
    return directory_index.scan_directory(
        sub,
        force=force,
        wiki=WIKI,
        cache=_DIR_CACHE,
        ttl=_DIR_TTL,
        excluded_dirs=_excluded_dirs,
        within=_within,
        skip=_skip,
        reserved=_reserved_seg,
        namespaces=_ns_dirs,
        metadata=_page_meta,
    )

# ── background cache warmer ─────────────────────────────────────────────────
def _warm_cache() -> None:
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
    if getattr(_start_warmer, "_started", False):
        return
    _start_warmer._started = True
    threading.Thread(target=_warm_loop, name="reader-cache-warmer", daemon=True).start()

def _about_info() -> dict:
    return about.about_info(vault=VAULT, wiki=WIKI, env=os.environ, yaml_module=yaml)

_EDITING = settings.editing_enabled()

@app.get("/api/about")
def api_about():
    """Vault name + engine/Hermes versions for the reader's About panel."""
    info = _about_info()
    info["chat_enabled"] = _chat_enabled()      # gate the Chat tab on a configured agent
    info["editing_enabled"] = _EDITING          # false -> Chat is read-only (no vault write-back)
    return info

_AGENT_API = settings.AGENT_API
_AGENT_KEY = settings.AGENT_KEY
_AGENT_MODEL = settings.AGENT_MODEL
_CHAT_MAX_MSGS = settings.CHAT_MAX_MESSAGES
_CHAT_MAX_CHARS = settings.CHAT_MAX_CHARACTERS

_AGENT_SYSTEM = settings.AGENT_SYSTEM

def _chat_enabled() -> bool:
    return bool(_AGENT_API and _AGENT_KEY)

def _budget_tripped() -> bool:
    return chat.budget_tripped(VAULT)

@app.post("/api/chat")
async def api_chat(request: Request):
    return await chat.relay_chat(
        request,
        enabled=_chat_enabled,
        budget_tripped=_budget_tripped,
        rate=_RATE,
        client_ip=_client_ip,
        agent_api=_AGENT_API,
        agent_key=_AGENT_KEY,
        agent_model=_AGENT_MODEL,
        agent_system=_AGENT_SYSTEM,
        max_messages=_CHAT_MAX_MSGS,
        max_characters=_CHAT_MAX_CHARS,
        urlopen=urllib.request.urlopen,
    )

_DERIVED_TYPES = {"dashboard"}

def _dir_is_derived(md_paths: list[Path]) -> bool:
    return browse.directory_is_derived(
        md_paths, split_frontmatter=split_fm, derived_types=_DERIVED_TYPES
    )

@app.get("/api/tree")
def api_tree():
    return browse.tree(
        wiki=WIKI, excluded_dirs=_excluded_dirs, skip=_skip, reserved=_reserved_seg,
        namespaces=_ns_dirs, is_derived=_dir_is_derived, rail_top=_rail_top_section,
    )

@app.get("/api/groups")
def api_groups():
    return browse.groups(display_groups=_display_groups, pages_of_types=_pages_of_types)

@app.get("/api/pages")
def api_pages(dir: str = Query(default=""), group: str = Query(default="")):
    return browse.pages(
        dir, group, display_groups=_display_groups, pages_of_types=_pages_of_types,
        namespace_about=_ns_about, scan=_scan_dir,
    )

_REVISION_CACHE: tuple[float, list[dict]] = (float("-inf"), [])

@app.get("/api/page-revisions")
def api_page_revisions():
    """Minimal cached full-vault revision inventory for standing detectors."""
    global _REVISION_CACHE
    response, _REVISION_CACHE = directory_index.revision_inventory(
        wiki=WIKI, cache=_REVISION_CACHE, ttl=_DIR_TTL, excluded_dirs=_excluded_dirs,
        skip=_skip, reserved=_reserved_seg, namespaces=_ns_dirs,
    )
    return response

def _ns_about(dir: str) -> str:
    return browse.namespace_about(
        dir, wiki=WIKI, within=_within, split_frontmatter=split_fm, render=render_md
    )

# ── render any page ─────────────────────────────────────────────────────────
def _resolve_page(path: str) -> Path:
    return browse.resolve_page(
        path, wiki=WIKI, skip=_skip, excluded_dirs=_excluded_dirs,
        namespaces=_ns_dirs, within=_within, split_frontmatter=split_fm,
    )

_META_PANEL_SKIP = {"title", "name", "type", "version", "raw"}
_META_SECONDARY = {"tlp", "created", "updated", "last_updated", "last_seen", "first_seen",
                   "assembled_from", "tier", "tlp_caveat",
                   "maintained_by", "discovered_by", "created_by", "last_modified_by"}
_SRC_REL_CACHE: tuple[float, dict] = (float("-inf"), {})   # -inf, not 0.0 — see the note at _DIR_TTL
_REL_RANK = {c: i for i, c in enumerate("FEDCBA")}    # A=5 (highest) … F=0; unknown -> -1

def _source_reliability() -> dict:
    global _SRC_REL_CACHE
    result, _SRC_REL_CACHE = page_metadata.source_reliability(
        cache=_SRC_REL_CACHE, ttl=_DIR_TTL,
        schema_path=_governing_schema_path, yaml_module=yaml,
    )
    return result

def _val_text(v) -> str:
    return page_metadata.value_text(v)

def _shape_conflicts(fm: dict) -> list[dict]:
    return page_metadata.shape_conflicts(
        fm, source_reliability=_source_reliability, reliability_rank=_REL_RANK
    )

_OBS_INDEX_CACHE: tuple[float, dict] = (float("-inf"), {})   # -inf, not 0.0 — see the note at _DIR_TTL

def _observations_by_canonical() -> dict:
    global _OBS_INDEX_CACHE
    result, _OBS_INDEX_CACHE = page_metadata.observations_by_canonical(
        cache=_OBS_INDEX_CACHE, ttl=_DIR_TTL, wiki=WIKI, skip=_skip,
        reserved=_reserved_seg, split_frontmatter=split_fm, read_head=_read_head,
    )
    return result

def _meta_compact_dict(d: dict) -> str:
    return page_metadata.compact_dict(d)

def _url_label(url: str) -> str:
    return page_metadata.url_label(url)

def _ref_target(s: str) -> str | None:
    return page_metadata.ref_target(s, wiki=WIKI, skip=_skip, within=_within)

def _meta_values(v) -> list[dict]:
    return page_metadata.meta_values(v, wiki=WIKI, skip=_skip, within=_within)

def _meta_panel_items(fm: dict) -> dict:
    return page_metadata.panel_items(
        fm,
        wiki=WIKI,
        skip=_skip,
        within=_within,
        panel_skip=_META_PANEL_SKIP,
        secondary_keys=_META_SECONDARY,
    )

_RPANELS_CACHE: list = [0.0, None]

def _reader_panels() -> dict:
    return page_metadata.reader_panels(cache=_RPANELS_CACHE, vault=VAULT)

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

def _entity_assessments(fm: dict, rel: str) -> list[dict]:
    return page_metadata.entity_assessments(
        fm, rel, wiki=WIKI, split_frontmatter=split_fm
    )

@app.get("/api/page")
def api_page(path: str = Query(...)):
    """Render any wiki page for click-through navigation."""
    return page_metadata.page_response(
        path, resolve_page=_resolve_page, split_frontmatter=split_fm,
        metadata_items=_meta_panel_items, render=render_md, panel_for=_panel_for,
        provenance_for=_provenance, conflicts_for=_shape_conflicts,
        observations=_observations_by_canonical, assessments_for=_entity_assessments,
        recent_reporting_for=_recent_reporting, wiki=WIKI,
    )

def _recent_reporting(fm: dict) -> list[dict]:
    return page_metadata.recent_reporting(fm, wiki=WIKI, split_frontmatter=split_fm)

def _provenance(fm: dict, body: str) -> dict:
    return page_metadata.provenance(fm, body, source_reliability=_source_reliability)

def _clean_markdown(raw: str, title: str | None = None) -> str:
    return exports.clean_markdown(
        raw,
        title,
        split_frontmatter=split_fm,
        resolve_embeds=_resolve_embeds,
        uncode_wikilinks=_uncode_wikilinks,
        delink=_delink,
        deref_local_links=_deref_local_links,
    )

_PDF_CSS = exports.PDF_CSS

def _pandoc(clean_md: str, fmt: str, title: str | None = None) -> bytes:
    return exports.convert(
        clean_md, fmt, title, subprocess_module=subprocess, pdf_css=_PDF_CSS
    )

_DL_MIME = exports.MIME_TYPES

@app.get("/api/download")
def api_download(request: Request, fmt: str, path: str = Query(...)):
    return exports.download_response(
        request, fmt, path, mime_types=_DL_MIME, resolve_page=_resolve_page,
        split_frontmatter=split_fm, clean_markdown_text=_clean_markdown,
        exports_enabled=_EXPORTS_ENABLED, guard=_guard, semaphore=_EXPORT_SEM,
        convert_document=_pandoc,
    )

@app.get("/api/search")
def api_search(request: Request, q: str = Query(...), limit: int = 40):
    return search.search(
        request, q, limit, wiki=WIKI, guard=_guard, semaphore=_SEARCH_SEM,
        excluded_dirs=_excluded_dirs, namespaces=_ns_dirs, subprocess_module=subprocess,
    )

_BACKLINKS: dict = {"map": None, "ts": 0.0}
_BACKLINKS_TTL = limits.intenv("OKENGINE_BACKLINKS_TTL", 86400, lo=60)  # 24h default — the fallback
_BL_LOCK = threading.Lock()
_BL_ARTIFACT_MAX_AGE = limits.intenv("OKENGINE_BACKLINKS_MAX_AGE", 172800, lo=3600)
_BL_ARTIFACT: dict = {"map": None, "mtime": None}

_RESERVED_BL_NAMES = frozenset({"HOT.md", "log.md"})
def _skip_backlink_src(key: str) -> bool:
    return backlinks.skip_source(
        key,
        skip=_skip,
        reserved_names=_RESERVED_BL_NAMES,
        is_reserved_segment=_is_reserved_seg,
        excluded_dirs=_excluded_dirs,
        surfaced_derived=_SURFACED_DERIVED,
        backlink_drop_dirs=_backlink_drop_dirs,
        namespaces=lambda source: _ns_dirs(WIKI / source),
    )

def _backlink_title(src: str) -> str:
    return backlinks.source_title(
        src, wiki=WIKI, split_frontmatter=split_fm, read_head=_read_head, h1_re=_H1_RE
    )

_BL_FM = backlinks.FRONTMATTER_RE
_BL_WIKI = backlinks.WIKILINK_RE
_BL_MD = backlinks.MARKDOWN_LINK_RE
_BL_FENCE = backlinks.FENCE_RE
_BL_INLINE = backlinks.INLINE_CODE_RE

def _bl_strip(text: str) -> str:
    return backlinks.strip_markdown(
        text, frontmatter_re=_BL_FM, fence_re=_BL_FENCE, inline_re=_BL_INLINE
    )

def _bl_wikikey(inner: str):
    return backlinks.wikilink_key(inner)

def _bl_mdkey(url: str, doc_dir: str):
    return backlinks.markdown_key(url, doc_dir)

def _scan_forward_refs() -> list:
    return backlinks.scan_forward_refs(
        wiki=WIKI, skip_source=_skip_backlink_src, strip_markdown_text=_bl_strip,
        wiki_re=_BL_WIKI, markdown_re=_BL_MD, wikilink_parser=_bl_wikikey,
        markdown_parser=_bl_mdkey,
    )

def _build_backlinks() -> dict:
    return backlinks.build(
        scan=_scan_forward_refs, skip_source=_skip_backlink_src, source_title=_backlink_title
    )

def _artifact_backlinks() -> dict | None:
    return backlinks.artifact_map(
        wiki=WIKI, max_age=_BL_ARTIFACT_MAX_AGE, cache=_BL_ARTIFACT
    )

def _refresh_backlinks_async() -> None:
    """Kick at most one background graph rebuild (no-op if one is already running
    or the map is still fresh). Used by the request path so it never blocks."""
    backlinks.refresh_async(
        lock=_BL_LOCK,
        state=_BACKLINKS,
        ttl=_BACKLINKS_TTL,
        load=_load_backlinks,
        threading_module=threading,
    )

def _load_backlinks(blocking: bool = True) -> dict:
    return backlinks.load_map(
        blocking=blocking,
        artifact=_artifact_backlinks,
        state=_BACKLINKS,
        ttl=_BACKLINKS_TTL,
        lock=_BL_LOCK,
        build=_build_backlinks,
        refresh=_refresh_backlinks_async,
    )

def _prewarm_backlinks() -> None:
    """Build the backlink graph in the background at startup so the first user
    request doesn't block on the build."""
    backlinks.prewarm(load=_load_backlinks, threading_module=threading)

@app.get("/api/backlinks")
def api_backlinks(path: str = Query(...), limit: int = 100):
    """Pages that reference `path` via the precomputed wikilink graph. `path` is the
    wiki-relative key without .md (e.g. 'concepts/example-topic')."""
    key = path[:-3] if path.endswith(".md") else path
    refs = _load_backlinks(blocking=False).get(key, [])    # never block on the build
    return {"path": key, "count": len(refs),
            "backlinks": refs[:max(1, min(limit, 500))]}

@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(
        STATIC / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )

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
