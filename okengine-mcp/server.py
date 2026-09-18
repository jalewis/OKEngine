#!/usr/bin/env python3
"""OKEngine MCP query surface (ENGINE, Phase 3).

Exposes the OKF vault (whatever pack is mounted) as read-only query tools so
other agents can consume the compiled corpus as a tool ("grow the vault's own
substrate surface"). Domain-agnostic: it serves the mounted vault, not a
hardcoded domain.

Every result carries its vault PATH — the provenance contract that makes the
knowledge attributable when a consumer ingests it (`discovered_by`).

Tools:
  search(query, mode, limit)            — qmd hybrid/lexical search (via kb_search)
  get_page(path)                        — fetch a wiki page (frontmatter + body)
  find_references(target)               — precomputed backlinks + direct forward refs
  retrieve_context(path)                — a page + its precomputed graph neighbourhood
  graph_stats()                         — precomputed graph health and hubs
  list_pages(namespace, type, status)   — list pages in a namespace, filtered by
                                          frontmatter type/status (domain-agnostic)

Transport: stdio by default; set OKENGINE_MCP_TRANSPORT=streamable-http for networked
consumers. Read-only — no tool mutates the vault.

Per-extension scoped tokens (okengine#132): the admin token (OKENGINE_MCP_TOKEN) keeps
FULL read (gateway crons + reader Chat relay are unaffected). A token minted for an
extension is limited to its declared read scopes on the explicit-path tools
(get_page / retrieve_context) and filtered in list_pages. search / find_references /
graph_stats are full-vault for any authenticated caller in v1 — read-scope filtering of
those text/graph surfaces is a documented deferral (lower-risk discovery surfaces).

Env: WIKI_PATH (/opt/vault), OKENGINE_MCP_SCRIPTS (/opt/data/scripts).
"""
from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
import threading
import time
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from okengine.mcp import projection as _projection

_BACKLINK_READ_ERRORS = (OSError, json.JSONDecodeError, ValueError)
from okengine.mcp import scope as _scope

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
SCRIPTS = Path(os.environ.get("OKENGINE_MCP_SCRIPTS", "/opt/data/scripts"))
PYBIN = os.environ.get("OKENGINE_MCP_PY", "/opt/hermes/.venv/bin/python")
_QMD_ENV = {
    "XDG_CACHE_HOME": "/opt/data/qmd/cache",
    "XDG_CONFIG_HOME": "/opt/data/qmd/config",
    "QMD_FORCE_CPU": "1",
}
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)

# Disable the MCP SDK's DNS-rebinding host allowlist (okengine#138): on a bridge the gateway
# reaches this by SERVICE NAME (Host: okengine-mcp:8730), which FastMCP's loopback-default
# allowlist rejects with 421 "Invalid Host header" — silently killing the read MCP. This server
# is internal-only (the per-pack bridge / a loopback host port) and authenticated by
# OKENGINE_MCP_TOKEN, not browser-facing, so DNS-rebinding protection is moot; the token is the
# guard. (Without this, every bridge deployment loses the okengine read tools.)
mcp = FastMCP("okengine",
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

# One shared admission gate covers interactive qmd searches and background qmd
# maintenance. It is deliberately thread-based: the HTTP tool runs on asyncio
# while the index maintainer is a thread. Callers poll non-blockingly, so a
# canceled HTTP request never leaves a waiter that later steals a slot.
_QMD_CONCURRENCY = max(1, int(os.environ.get("OKENGINE_MCP_QMD_CONCURRENCY", "2") or 2))
_QMD_CAPACITY = threading.BoundedSemaphore(_QMD_CONCURRENCY)
_SEARCH_QUEUE_SECONDS = max(
    0.0, float(os.environ.get("OKENGINE_MCP_SEARCH_QUEUE_SECONDS", "10") or 10))
_SEARCH_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("OKENGINE_MCP_SEARCH_TIMEOUT_SECONDS", "120") or 120))
_INDEX_REFRESH_LOCK = threading.Lock()
_INDEX_STATUS_LOCK = threading.Lock()
_INDEX_STATUS = {"active": False, "ready": False, "error": ""}


def _set_index_status(*, active: bool | None = None, ready: bool | None = None,
                      error: str | None = None) -> None:
    with _INDEX_STATUS_LOCK:
        if active is not None:
            _INDEX_STATUS["active"] = active
        if ready is not None:
            _INDEX_STATUS["ready"] = ready
        if error is not None:
            _INDEX_STATUS["error"] = error


def _index_unavailable_message() -> str | None:
    with _INDEX_STATUS_LOCK:
        status = dict(_INDEX_STATUS)
    if not status["active"] or status["ready"]:
        return None
    detail = status["error"] or "knowledge index is building"
    return f"(search unavailable: {detail}; retry later)"


def _run(args: list[str], extra_env: dict | None = None, timeout: int = 90) -> str:
    """Run a helper script with a timeout that actually bounds wall-clock (okengine#198).

    subprocess.run(timeout=) only kills the DIRECT child; a spawned grandchild
    survives holding the stdout pipe, and the post-kill communicate() blocks until IT exits — so
    the internal timeout was cosmetic and the client hung to its own 300s ceiling. Start the child
    in its OWN process group (start_new_session) and killpg the whole tree on timeout."""
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.Popen([PYBIN, *args], cwd=str(VAULT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)     # the whole group, including grandchildren
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()                           # reap; pipes close now the group is dead
        return "(query timed out)"
    out = (stdout or "").strip()
    return out or (stderr or "(no output)").strip()


def _kill_process_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _run_async(args: list[str], extra_env: dict | None = None,
                     timeout: float = 90) -> str:
    """Cancellation-safe helper execution for HTTP tools.

    Cancellation and timeout kill the complete helper process group (Python
    wrapper plus qmd descendants) and await reaping before returning control.
    """
    env = {**os.environ, **(extra_env or {})}
    proc = await asyncio.create_subprocess_exec(
        PYBIN, *args, cwd=str(VAULT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_process_group(proc)
        await proc.communicate()
        raise
    except asyncio.CancelledError:
        _kill_process_group(proc)
        await proc.communicate()
        raise
    out = (stdout or b"").decode(errors="replace").strip()
    err = (stderr or b"").decode(errors="replace").strip()
    return out or err or "(no output)"


async def _acquire_qmd_capacity(wait_seconds: float) -> bool:
    deadline = time.monotonic() + wait_seconds
    while True:
        if _QMD_CAPACITY.acquire(blocking=False):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _safe(path: str) -> Path | None:
    """Resolve the same canonical path spelling accepted by the write surface."""
    try:
        wiki_abs = WIKI.resolve()
    except OSError:
        wiki_abs = WIKI
    rel = str(path).strip()
    prefixes = []
    for base in (wiki_abs, wiki_abs.parent):
        value = str(base)
        prefixes.extend((value, value.lstrip("/")))
    rel = rel.lstrip("/")
    # Tolerate a repeatedly-prefixed absolute path from clients that accidentally joined a
    # vault root to an already-absolute value. Strip recognized roots until no progress remains.
    while True:
        before = rel
        for prefix in sorted({value for value in prefixes if value}, key=len, reverse=True):
            if rel == prefix or rel.startswith(prefix + "/"):
                rel = rel[len(prefix):].lstrip("/")
                break
        while rel == "wiki" or rel.startswith("wiki/"):
            rel = rel[len("wiki"):].lstrip("/")
        if rel == before:
            break

    parts = rel.split("/")
    exact = WIKI / rel
    if exact.suffix != ".md":
        exact = exact.with_name(exact.name + ".md")
    try:
        exact_resolved = exact.resolve()
        exact_relative = exact_resolved.relative_to(wiki_abs)
    except (OSError, ValueError):
        exact_relative = None
    entity_shape = (
        parts and parts[0] == "entities"
        and (len(parts) == 2 or (len(parts) >= 3 and all(len(seg) == 1 for seg in parts[1:-1])))
    )
    # Canonical shards win only when they actually exist. Flat legacy pages and special files
    # remain readable; INDEX.md is never treated as an entity slug.
    special_entity = parts and parts[-1].lower() in {"index", "index.md"}
    if exact_relative is not None and exact_resolved.is_file() and (
        not entity_shape or special_entity
    ):
        rel = str(exact_relative)
    elif entity_shape:
        stem = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
        if stem:
            first = stem[0].lower()
            one = f"entities/{first}/{stem}.md"
            second = stem[1].lower() if len(stem) > 1 and stem[1].isalnum() else "_"
            two = f"entities/{first}/{second}/{stem}.md"
            try:
                leaf = WIKI / "entities" / first
                if (WIKI / two).is_file() or (
                    not (WIKI / one).exists()
                    and leaf.is_dir()
                    and any(child.is_dir() and len(child.name) == 1 for child in leaf.iterdir())
                ):
                    rel = two if (WIKI / two).is_file() else str(exact_relative or rel)
                else:
                    rel = one if (WIKI / one).is_file() else str(exact_relative or rel)
            except OSError:
                rel = one

    p = WIKI / rel
    if p.suffix != ".md":
        # APPEND, never with_suffix() — it strips everything after the last dot and truncates a
        # dotted slug, desyncing the read path from the write path.
        p = p.with_name(p.name + ".md")
    try:
        p = p.resolve()
        p.relative_to(WIKI.resolve())
    except (OSError, ValueError):
        return None
    return p


_LIMIT_MAX = int(os.environ.get("OKENGINE_MCP_LIMIT_MAX", "100") or 100)


def _clamp_limit(v, default: int) -> int:
    """Coerce + clamp a caller-supplied `limit` to [1, _LIMIT_MAX] (okengine#51): a non-int or
    absurd value must not crash a tool or let a caller pull an unbounded result set."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, _LIMIT_MAX))


async def _search(query: str, mode: str = "search", limit: int = 8, tier: str = "") -> str:
    """Search the compiled knowledge base.

    mode: 'search' (default) — instant BM25 lexical; no model load, best for finding a
    known entity/term by name. 'hybrid' — BM25 + vector + rerank, better for
    concept/narrative queries but runs local models (slow on CPU without a GPU). Default
    is lexical for responsiveness; pass mode='hybrid' when a semantic match is needed.
    tier: optional comma list of hot,warm,cold to keep (G4 tier; empty = all tiers).
    Returns ranked passages, each with its vault path for provenance.
    """
    unavailable = _index_unavailable_message()
    if unavailable:
        return unavailable
    qmode = "search" if mode == "search" else "query"   # 'hybrid' -> qmd 'query'
    cmd = [str(SCRIPTS / "kb_search.py"), "--mode", qmode,
           "--limit", str(_clamp_limit(limit, 8)), str(query)]
    if (tier or "").strip():
        cmd += ["--tier", tier.strip()]
    if not await _acquire_qmd_capacity(_SEARCH_QUEUE_SECONDS):
        print("okengine-mcp: SEARCH_SATURATED qmd capacity queue expired",
              file=sys.stderr, flush=True)
        # THIS is the saturation okengine#410 was about: a lane refused an answer. It was
        # unmeasured because the first telemetry only wrapped `_qmd`, which searches never use.
        _record_qmd("search", "saturated")
        _publish_qmd_stats()
        return "(search saturated: qmd capacity is busy; retry with backoff)"
    started = time.monotonic()
    try:
        try:
            result = (await _run_async(
                cmd, extra_env=_QMD_ENV, timeout=_SEARCH_TIMEOUT_SECONDS))[:8000]
            _record_qmd("search", "ok", (time.monotonic() - started) * 1000)
            return result
        except TimeoutError:
            print(f"okengine-mcp: SEARCH_TIMEOUT after {_SEARCH_TIMEOUT_SECONDS:.0f}s",
                  file=sys.stderr, flush=True)
            _record_qmd("search", "timeouts", (time.monotonic() - started) * 1000)
            return "(search timed out: narrow the query or retry later)"
    finally:
        _QMD_CAPACITY.release()
        _publish_qmd_stats()


@mcp.tool()
async def search(query: str, mode: str = "search", limit: int = 8, tier: str = "") -> str:
    """Search the compiled knowledge base with bounded qmd capacity."""
    return await _search(query, mode, limit, tier)


@mcp.tool()
async def projection_status() -> dict:
    """Report the optional PostgreSQL projection epoch and freshness.

    Raises an explicit tool error when the projection is not configured or has never completed.
    Unlike completeness-sensitive query tools, status itself reports (rather than rejects) stale
    state so operators can diagnose it.
    """
    _authorize_projection_query()
    return await _projection.projection_status()


def _authorize_projection_query() -> None:
    """Projection queries can reveal aggregate facts outside a narrow path scope.

    Until SQL predicates are compiled from arbitrary extension globs, permit only the admin or a
    token whose declared scope already covers the whole vault. Refusal is safer than returning a
    complete-but-unauthorized count.
    """
    caller = _caller()
    if caller.get("kind") != "admin" and not _scope.is_full(caller.get("read_scopes") or []):
        raise PermissionError("PostgreSQL projection tools require full-vault read scope")


@mcp.tool()
async def count_pages(namespace: str = "", type: str = "", status: str = "",
                      include_tombstoned: bool = False, published_after: str = "",
                      published_before: str = "", updated_after: str = "",
                      updated_before: str = "") -> dict:
    """Count all matching canonical pages using the complete, freshness-checked projection."""
    _authorize_projection_query()
    return await _projection.count_pages(namespace, type, status, include_tombstoned,
                                         published_after=published_after,
                                         published_before=published_before,
                                         updated_after=updated_after, updated_before=updated_before)


@mcp.tool()
async def find_projected_pages(namespace: str = "", type: str = "", status: str = "",
                               include_tombstoned: bool = False,
                               published_after: str = "", published_before: str = "",
                               updated_after: str = "", updated_before: str = "",
                               order: str = "path", limit: int = 40) -> dict:
    """Find structured page metadata with matched/returned/truncated coverage."""
    _authorize_projection_query()
    return await _projection.find_pages(
        namespace, type, status, include_tombstoned, published_after=published_after,
        published_before=published_before, updated_after=updated_after,
        updated_before=updated_before, order=order, limit=limit)


@mcp.tool()
async def get_projected_page_meta(path_or_id: str) -> dict:
    """Fetch projected metadata by vault path or canonical page id; never returns page prose."""
    _authorize_projection_query()
    return await _projection.get_page_meta(path_or_id)


@mcp.tool()
async def find_projected_links(target: str = "", source: str = "", resolution: str = "",
                               limit: int = 40) -> dict:
    """Query projected wikilinks, including exact/alias/slug/unresolved provenance."""
    _authorize_projection_query()
    return await _projection.find_links(target, source, resolution, limit)


@mcp.tool()
def get_page(path: str) -> str:
    """Fetch a single wiki page by its vault-relative path (e.g.
    'concepts/topic/example-pattern' or
    'entities/a/acme-corp'). Returns frontmatter + body."""
    p = _safe(path)
    if p is None:
        return "(refused: path outside the vault)"
    if not _authorize_read(path):
        return "(refused: outside this caller's read scope)"
    if not p.is_file():
        return f"(not found: {path})"
    return p.read_text(encoding="utf-8", errors="replace")[:16000]


# ── knowledge-graph backlinks: serve the cron-precomputed artifact ────────────────────────────────
# The `backlinks-refresh` cron writes the inverted {target -> [{key,title}]} graph to
# wiki/.backlinks.json (okengine#168/#179); the reader + cockpit serve it directly. This MCP used to
# rebuild the IWE graph live on EVERY find_references/retrieve_context call (kb_graph -> iwe
# subprocess) — O(rebuild-whole-graph), which on a 60k-page vault blew past the MCP call timeout
# (a 60k-page deployment, recurring). The artifact is authoritative: request paths must never turn a typo,
# ambiguous slug, or missed refresh into an unbounded whole-corpus subprocess.
_BL_ARTIFACT_MAX_AGE = max(3600, int(os.environ.get("OKENGINE_BACKLINKS_MAX_AGE", "172800")))
_BL_CACHE: dict = {"map": None, "mtime": None, "doc": None}
_WL_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:[#|][^\]]*)?\]\]")


def _artifact_backlinks() -> dict | None:
    """The precomputed {target -> [{key,title}]} backlink map (wiki/.backlinks.json), or None when
    absent / stale / corrupt. Callers return an explicit degraded result. mtime-cached (steady-state cost
    per call is one stat)."""
    p = WIKI / ".backlinks.json"
    try:
        st = p.stat()
    except OSError:
        return None
    if time.time() - st.st_mtime > _BL_ARTIFACT_MAX_AGE:
        return None
    if _BL_CACHE["mtime"] == st.st_mtime and _BL_CACHE["map"] is not None:
        return _BL_CACHE["map"]
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except _BACKLINK_READ_ERRORS:
        return None
    m = doc.get("backlinks") if isinstance(doc, dict) else None
    if not isinstance(m, dict):
        return None
    _BL_CACHE["map"], _BL_CACHE["doc"], _BL_CACHE["mtime"] = m, doc, st.st_mtime
    return m


def _artifact_doc() -> dict | None:
    """The FULL backlinks artifact (meta: pages/targets/edges/built_at + the map), same cache and
    freshness rules as _artifact_backlinks. graph_stats reads the meta (okengine#199)."""
    return _BL_CACHE["doc"] if _artifact_backlinks() is not None else None


def _resolve_key(target: str, bl: dict) -> str | None:
    """Resolve an agent-supplied `target` (a page path OR a bare name) to a canonical backlink key:
    exact key, then a page on disk, then a unique basename match against the artifact's keys."""
    t = target.strip().strip("/")
    t = t[:-3] if t.endswith(".md") else t
    if t in bl:
        return t
    # On-disk fallback goes through _safe() like get_page does: an unguarded
    # `(WIKI / t).is_file()` let `../CLAUDE` resolve to the vault-root persona file and let a
    # symlink inside wiki/ read anything on the host (okengine#660). The key handed back is the
    # CANONICAL wiki-relative path, so an in-vault `a/../a/x` cannot mint a second graph key.
    p = _safe(t)
    if p is not None and p.is_file():
        return p.relative_to(WIKI.resolve()).as_posix()[:-3]
    slug = t.split("/")[-1].lower()
    hits = {k for k in bl if k.split("/")[-1].lower() == slug}
    if not hits:                                    # also scan referrer keys (pages with no inbound)
        for refs in bl.values():
            for r in refs:
                k = str(r.get("key", ""))
                if k.split("/")[-1].lower() == slug:
                    hits.add(k)
    return next(iter(hits)) if len(hits) == 1 else None


def _forward_links(key: str) -> list[str]:
    """A page's own outbound [[wikilinks]] (forward refs) — a cheap body parse, no IWE."""
    try:
        txt = (WIKI / (key + ".md")).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out, seen = [], set()
    for m in _WL_RE.finditer(txt):
        k = m.group(1).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _fmt_refs(head: str, items: list, cap: int) -> list[str]:
    lines = [f"## {head} ({len(items)})"]
    for it in items[:cap]:
        lines.append(f"- [[{it}]]" if isinstance(it, str)
                     else f"- [[{it.get('key','')}]] — {it.get('title','')}")
    if len(items) > cap:
        lines.append(f"- … and {len(items) - cap} more")
    return lines


def _graph_unavailable() -> str:
    return ("(knowledge graph unavailable: wiki/.backlinks.json is absent, stale, or corrupt; "
            "run the backlinks-refresh lane)")


def _target_unresolved(target: str) -> str:
    return f"(not found or ambiguous knowledge-graph target: {target})"


@mcp.tool()
def find_references(target: str) -> str:
    """Knowledge-graph lookup: pages that reference `target` (backlinks) plus `target`'s own outbound
    references. Served from the cron-precomputed backlink graph (wiki/.backlinks.json).
    `target` is a page path or name."""
    bl = _artifact_backlinks()
    if bl is None:
        return _graph_unavailable()
    key = _resolve_key(str(target), bl)
    if key is None:
        return _target_unresolved(str(target))
    lines = [f"# {key}", ""]
    lines += _fmt_refs("Referenced by", bl.get(key, []), 50) + [""]
    lines += _fmt_refs("References", _forward_links(key), 50)
    return "\n".join(lines)[:8000]


@mcp.tool()
def retrieve_context(path: str) -> str:
    """Retrieve a page WITH its knowledge-graph context expanded: the page plus its outbound
    references and incoming backlinks, one hop out. Richer than get_page (the raw file) — use it to
    load a page together with its neighbourhood. Served from the precomputed backlink graph.
    `path` is a vault page id/path, e.g. 'entities/a/example'."""
    if not _authorize_read(str(path)):
        return "(refused: outside this caller's read scope)"
    bl = _artifact_backlinks()
    if bl is None:
        return _graph_unavailable()
    key = _resolve_key(str(path), bl)
    if key is None:
        return _target_unresolved(str(path))
    page = _safe(key)                  # keys come from the artifact or _resolve_key; contain anyway
    try:
        body = (page.read_text(encoding="utf-8", errors="replace")[:12000]
                if page is not None else "(page body unavailable)")
    except OSError:
        body = "(page body unavailable)"
    lines = [body, "", "---"]
    lines += _fmt_refs("Incoming backlinks", bl.get(key, []), 30) + [""]
    lines += _fmt_refs("Outbound references", _forward_links(key), 30)
    return "\n".join(lines)[:16000]


@mcp.tool()
def graph_stats() -> str:
    """Knowledge-graph health/shape: page/edge totals, pages with no inbound link,
    and the most-referenced pages (the corpus's hubs). Use to find under-connected
    pages or the hubs. No arguments.

    Served from the precomputed wiki/.backlinks.json artifact (okengine#199 — a live
    whole-graph rebuild per call times out on large vaults)."""
    doc = _artifact_doc()
    if doc is not None:
        bl = doc.get("backlinks") or {}
        pages, targets, edges = doc.get("pages"), doc.get("targets"), doc.get("edges")
        if isinstance(pages, int) and isinstance(targets, int) and isinstance(edges, int):
            built = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(doc["built_at"])) \
                if isinstance(doc.get("built_at"), (int, float)) else "unknown"
            hubs = sorted(bl.items(), key=lambda kv: len(kv[1]), reverse=True)[:15]
            excl = ", ".join(doc.get("excluded_namespaces") or []) or "none"
            lines = [f"Knowledge-graph stats (from the backlinks artifact, built {built}):",
                     f"  pages: {pages}  ·  link targets: {targets}  ·  edges: {edges}",
                     f"  pages with no inbound link: {pages - targets} "
                     f"(namespaces excluded from the graph: {excl})",
                     "", "Most-referenced pages (inbound links):"]
            lines += [f"  {len(v):>5}  {k}" for k, v in hubs]
            return "\n".join(lines)[:8000]
    return _graph_unavailable()


@mcp.tool()
def list_pages(namespace: str, type: str = "", status: str = "", limit: int = 40) -> str:
    """List pages under a vault namespace — a top-level directory such as
    'entities' or 'concepts', or any directory a pack defines — newest first,
    each with its vault path. Optionally filter by frontmatter `type` and/or
    `status`. Domain-agnostic: it hardcodes no domain types or directories, and
    also scans sub-domain namespaces (`*/<namespace>`).
    """
    ns = (namespace or "").strip().strip("/")
    if not ns or ".." in ns:
        return "(refused: bad namespace)"
    want_type = (type or "").strip().lower()
    want_status = (status or "").strip().lower()
    rows = []
    for base in (WIKI / ns, *sorted(WIKI.glob(f"*/{ns}"))):  # glob-ok: discovers per-pack namespace dirs (*/<ns>), not content
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            if p.name == "INDEX.md" or p.name.startswith((".", "_")):
                continue
            try:
                m = _FM.match(p.read_text(encoding="utf-8", errors="replace")[:3000])
            except OSError:
                continue
            if not m:
                continue
            try:
                fm = yaml.safe_load(m.group(1)) or {}
            except Exception:
                continue
            if not isinstance(fm, dict):
                continue
            if want_type and str(fm.get("type") or "").lower() != want_type:
                continue
            st = str(fm.get("status") or "").lower()
            if want_status and st != want_status:
                continue
            rel = p.relative_to(WIKI).as_posix()[:-3]
            if not _authorize_read(rel):        # drop out-of-scope rows (okengine#132)
                continue
            date = str(fm.get("updated") or fm.get("resolves_by")
                       or fm.get("made_on") or fm.get("created") or "")
            rows.append((date, str(fm.get("title") or fm.get("name") or p.stem),
                         str(fm.get("type") or ""), st, rel))
    rows.sort(reverse=True)
    if not rows:
        flt = "".join((f" type={type!r}" if want_type else "",
                       f" status={status!r}" if want_status else ""))
        return f"(no pages in {ns!r}{flt})"
    head = " · ".join(x for x in (ns, f"type={type}" if want_type else "",
                                  f"status={status}" if want_status else "") if x)
    lines = [f"# {head} — {len(rows)}", ""]
    for date, title, typ, st, path in rows[:_clamp_limit(limit, 40)]:
        meta = " · ".join(x for x in (typ, st, date) if x)
        lines.append(f"- {title}" + (f" [{meta}]" if meta else "") + f" — {path}")
    return "\n".join(lines)


# Built-in token used when OKENGINE_MCP_TOKEN is unset, so a fresh deployment
# comes up authenticated out of the box (painless local-first). It is safe ONLY
# because the deployment binds the host port to loopback by default; set a real
# OKENGINE_MCP_TOKEN before widening the bind beyond localhost.
DEFAULT_LOCAL_TOKEN = "okengine-local"

_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _resolve_http_auth(env, host: str):
    """Decide MCP HTTP auth (local-first). Returns (token, warning):

      token is None  => serve with NO auth (explicit OKENGINE_MCP_ALLOW_UNAUTHENTICATED=1)
      token is a str => require `Bearer <token>`; an unset OKENGINE_MCP_TOKEN
                        falls back to the built-in DEFAULT_LOCAL_TOKEN so the
                        service always comes up (never crashes for missing auth).

    `warning` is a non-fatal message to log, or None. We warn only when bound
    beyond loopback with weak/no auth — on localhost the default is fine."""
    exposed = host not in _LOOPBACK
    if env.get("OKENGINE_MCP_ALLOW_UNAUTHENTICATED", "") == "1":
        warning = (f"binding {host} with NO authentication "
                   "(OKENGINE_MCP_ALLOW_UNAUTHENTICATED=1) — the whole vault is served "
                   "unauthenticated.") if exposed else None
        return None, warning
    token = env.get("OKENGINE_MCP_TOKEN") or DEFAULT_LOCAL_TOKEN
    warning = None
    if token == DEFAULT_LOCAL_TOKEN and exposed:
        # The built-in default token is PUBLIC (it's in the source) — binding it beyond
        # loopback serves the whole vault to anyone who reads the code. Fail CLOSED unless the
        # operator explicitly accepts it (okengine#50). Loopback default stays painless.
        if env.get("OKENGINE_MCP_ALLOW_DEFAULT_TOKEN", "") != "1":
            raise SystemExit(
                f"okengine-mcp: refusing to bind {host} with the built-in DEFAULT token — it is "
                "public. Set OKENGINE_MCP_TOKEN to a secret (or OKENGINE_MCP_ALLOW_DEFAULT_TOKEN=1 "
                "to override and serve the vault with the well-known token).")
        warning = (f"binding {host} with the built-in DEFAULT token "
                   "(OKENGINE_MCP_ALLOW_DEFAULT_TOKEN=1) — it is public; set OKENGINE_MCP_TOKEN "
                   "to a secret.")
    return token, warning


# Per-request caller identity, set by the auth middleware and read by the tools.
# None (stdio, or unauthenticated mode, or no middleware) = trusted local = FULL read,
# which is the pre-#132 behavior — back-compat by construction.
_caller_var: contextvars.ContextVar = contextvars.ContextVar("okengine_mcp_caller", default=None)


def _caller() -> dict:
    c = _caller_var.get()
    return c if c is not None else {"kind": "admin", "read_scopes": None}


def _authorize_read(rel_path: str) -> bool:
    """May the current caller read this wiki-relative path? Admin = always; an
    extension = only within its declared read scopes (okengine#132)."""
    c = _caller()
    if c.get("kind") == "admin":
        return True
    return _scope.path_in_scopes(rel_path, c.get("read_scopes") or [])


class _ScopedAuth:
    """ASGI middleware: resolve `Bearer <token>` -> caller identity, 401 if unknown.

    The configured admin token (OKENGINE_MCP_TOKEN) keeps FULL read — the gateway's
    cron jobs and the reader Chat relay use it, so their behavior is unchanged. A token
    minted for an extension (in the vault token store) resolves to its read scopes."""

    def __init__(self, app, admin_token: str):
        self.app, self.admin_token = app, admin_token

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            token = provided[7:] if provided.startswith("Bearer ") else ""
            caller = None
            if self.admin_token and hmac.compare_digest(token, self.admin_token):
                caller = {"kind": "admin", "read_scopes": None}
            else:
                rec = _scope.resolve(token)
                if rec is not None:
                    caller = {"kind": "extension", "ext_id": rec.get("ext_id"),
                              "read_scopes": rec.get("read_scopes") or []}
            if caller is None:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            _caller_var.set(caller)
        await self.app(scope, receive, send)


# ── background index maintenance ─────────────────────────────────────────────
# qmd lives HERE in the mcp container (not the gateway where cron-plus runs), so the
# search index can't be kept fresh by a normal cron job. The long-lived HTTP server
# self-maintains it: on startup ensure the wiki collection is registered (so a fresh
# deploy self-bootstraps search), then incrementally `qmd update` on a timer. Lexical
# (FTS) only — vector embeddings (`qmd embed`) are heavy and off the default search path.
_QMD_BIN = os.environ.get("OKENGINE_QMD_BIN", "qmd")
_INDEX_REFRESH_HOURS = float(os.environ.get("OKENGINE_MCP_INDEX_REFRESH_HOURS", "6") or 0)


# ── qmd search telemetry ─────────────────────────────────────────────────────
# okengine#410 fixed search saturation by ADMISSION CONTROL (a 2-slot semaphore) and recommended
# "expose timeout/saturation distinctly in fleet health". That half never shipped, so the very
# conditions that would justify revisiting the search layer -- rising p95, routine saturation --
# are currently unobservable (okengine#568). A cap you cannot see hitting is indistinguishable from
# a cap you never reach.
#
# The mcp cannot reach /opt/data/metrics (not mounted here), but /opt/data/qmd IS its writable
# mount and the gateway sees the same directory, so that is the channel. A tiny JSON snapshot,
# rewritten atomically, read by fleet_health.
_QMD_STATS_PATH = Path(os.environ.get(
    "OKENGINE_MCP_QMD_STATS", "/opt/data/qmd/search-telemetry.json"))
_QMD_STATS_LOCK = threading.Lock()
# SEARCH and MAINTENANCE are counted SEPARATELY. They contend for the same two slots -- which is
# why saturation is reported as one shared number -- but their latencies are different populations
# and pooling them produces a metric that lies. The first live reading did exactly that: a 7.6s
# index refresh rendered as "search p95 over 5000ms" on a vault whose searches run in ~0.5s.
#
# Worse, the first version instrumented `_qmd` only, and SEARCHES DO NOT GO THROUGH `_qmd` -- they
# take the async path. So a file called search-telemetry.json measured zero searches, and its
# `saturated: 0` could never have incremented from the search path at all.
_QMD_LATENCY_SAMPLES = 200
_QMD_STATS: dict = {
    "search": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
    "maintenance": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
    "saturated": {"search": 0, "maintenance": 0},
}


def _record_qmd(kind: str, outcome: str, elapsed_ms: float | None = None) -> None:
    with _QMD_STATS_LOCK:
        if outcome == "saturated":
            _QMD_STATS["saturated"][kind] = _QMD_STATS["saturated"].get(kind, 0) + 1
            return
        bucket = _QMD_STATS.setdefault(
            kind, {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []})
        bucket[outcome] = bucket.get(outcome, 0) + 1
        if elapsed_ms is not None:
            samples = bucket["latency_ms"]
            samples.append(round(elapsed_ms))
            if len(samples) > _QMD_LATENCY_SAMPLES:
                del samples[: len(samples) - _QMD_LATENCY_SAMPLES]


def _percentiles(samples: list[int]) -> dict:
    if not samples:
        return {}
    ordered = sorted(samples)
    # nearest-rank; with few samples this IS the max, which is the honest answer rather than an
    # interpolated number implying precision the sample size does not support
    return {
        "p50_ms": ordered[max(0, (len(ordered) * 50) // 100 - 1)],
        "p95_ms": ordered[max(0, (len(ordered) * 95) // 100 - 1)],
        "max_ms": ordered[-1],
        "samples": len(ordered),
    }


def _qmd_stats_snapshot() -> dict:
    with _QMD_STATS_LOCK:
        search = dict(_QMD_STATS["search"])
        maint = dict(_QMD_STATS["maintenance"])
        saturated = dict(_QMD_STATS["saturated"])
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "concurrency_limit": _QMD_CONCURRENCY,
        "queue_seconds": _SEARCH_QUEUE_SECONDS,
        # shared, because the contention is shared -- one lane's refresh can refuse another's search
        "saturated": sum(saturated.values()),
        "saturated_by_kind": saturated,
    }
    for name, bucket in (("search", search), ("maintenance", maint)):
        counts = {k: v for k, v in bucket.items() if k != "latency_ms"}
        out[name] = {**counts, "calls": sum(counts.values()),
                     **_percentiles(bucket["latency_ms"])}
    return out


def _publish_qmd_stats() -> None:
    """Best-effort. Telemetry must never be able to break search: a failure here is swallowed
    deliberately, because the alternative is an unwritable metrics path taking the MCP down."""
    try:
        snap = _qmd_stats_snapshot()
        _QMD_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _QMD_STATS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
        tmp.replace(_QMD_STATS_PATH)
    except Exception:
        pass


def _qmd(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    # Every search AND every background refresh passes through here, which is what makes it the
    # right place to measure: they contend for the SAME 2 slots, so counting only searches would
    # under-report exactly the contention #410 was about.
    if not _QMD_CAPACITY.acquire(timeout=_SEARCH_QUEUE_SECONDS):
        _record_qmd("maintenance", "saturated")
        _publish_qmd_stats()
        return 75, "qmd capacity saturated"
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            [_QMD_BIN, *args], cwd=str(VAULT), env={**os.environ, **_QMD_ENV},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.communicate()
            _record_qmd("maintenance", "timeouts", (time.monotonic() - started) * 1000)
            return 124, "qmd timed out"
        rc = proc.returncode
        _record_qmd("maintenance", "ok" if rc == 0 else "errors",
                    (time.monotonic() - started) * 1000)
        return rc, (stdout or "") + (stderr or "")
    except FileNotFoundError:
        _record_qmd("maintenance", "errors")
        return 127, "qmd not installed"
    finally:
        _QMD_CAPACITY.release()
        _publish_qmd_stats()


def _refresh_index() -> bool:
    """Ensure the wiki collection is registered, then incrementally refresh the index. Returns
    whether the refresh SUCCEEDED — the caller must not mark the index current on a failure, or a
    failed `qmd update` silently reads as 'up to date' and search stays stale (invariant-audit).
    qmd-absent (rc 127) returns True: a permanent condition, not a transient failure to retry-spin on."""
    if not _INDEX_REFRESH_LOCK.acquire(blocking=False):
        print("okengine-mcp: qmd index refresh coalesced", file=sys.stderr, flush=True)
        return True
    try:
        return _refresh_index_locked()
    finally:
        _INDEX_REFRESH_LOCK.release()


def _refresh_index_locked() -> bool:
    rc, out = _qmd(["collection", "list"], timeout=60)
    if rc == 127:
        print("okengine-mcp: qmd not found — skipping index maintenance", file=sys.stderr, flush=True)
        _set_index_status(ready=False, error="qmd is not installed")
        return True
    if "qmd://wiki" not in out:                       # not registered yet (e.g. fresh deploy)
        arc, _ = _qmd(["collection", "add", str(WIKI)])
        print(f"okengine-mcp: registered qmd 'wiki' collection (rc={arc})", file=sys.stderr, flush=True)
    rc, _ = _qmd(["update"])
    print(f"okengine-mcp: qmd index refresh rc={rc}", file=sys.stderr, flush=True)
    if rc == 0:
        _set_index_status(ready=True, error="")
    else:
        _set_index_status(ready=False, error=f"initial qmd refresh failed (rc={rc})")
    return rc == 0


_INDEX_POLL_SECONDS = float(os.environ.get("OKENGINE_MCP_INDEX_POLL_SECONDS", "30") or 0)

# Debounce for change-triggered reindexing. On a large vault an incremental
# `qmd update` can take minutes; during a write burst (backfill lanes) an
# update-per-write keeps the container churning and starves tool calls into
# the client's timeout. Change-triggered updates therefore wait out a cooldown:
# at least MIN_UPDATE_SECONDS, and at least DUTY x the previous update's own
# duration (so reindexing never exceeds ~1/(1+DUTY) of the maintainer's time,
# no matter how slow qmd is on this vault). Writes landing during the cooldown
# are NOT lost — the mtime poll still sees them and one update covers them all.
_INDEX_MIN_UPDATE_SECONDS = float(os.environ.get("OKENGINE_MCP_INDEX_MIN_UPDATE_SECONDS", "60") or 0)
_INDEX_UPDATE_DUTY = 3.0


def _index_update_cooldown(duration: float) -> float:
    """Seconds to wait after an index update (which took `duration` s) before
    the next change-triggered one may run."""
    return max(_INDEX_MIN_UPDATE_SECONDS, _INDEX_UPDATE_DUTY * duration)


def _vault_max_mtime() -> float:
    """Newest mtime under the wiki — a cheap change-detector for reindex (okengine#80). Returns 0.0
    if the tree is missing/empty.

    Includes DIRECTORY mtimes, not just .md files: a reshelve/reshard moves a page with os.rename,
    which PRESERVES the file's mtime but bumps the source + destination directory mtimes. A file-only
    scan therefore missed the move entirely, so search served the old path (and 404'd the new one)
    until the 6h full refresh (okengine#326 [30])."""
    newest = 0.0
    try:
        for root, _dirs, files in os.walk(WIKI):
            try:
                m = os.stat(root).st_mtime          # dir mtime — catches os.rename moves (mtime-stable file)
                if m > newest:
                    newest = m
            except OSError:
                pass
            for fn in files:
                if fn.endswith(".md"):
                    try:
                        m = os.stat(os.path.join(root, fn)).st_mtime
                        if m > newest:
                            newest = m
                    except OSError:
                        pass
    except OSError:
        pass
    return newest


def _index_maintainer_step(state: dict) -> None:
    """One poll iteration of the index maintainer (extracted so the debounce is
    testable without the thread). `state` keys: last_full, last_seen,
    cooldown_until — all floats on the time.monotonic() clock."""
    now = time.monotonic()
    due_full = _INDEX_REFRESH_HOURS > 0 and (now - state["last_full"]) >= _INDEX_REFRESH_HOURS * 3600
    if state["last_full"] == 0.0 or due_full:
        # Snapshot the vault's max mtime BEFORE the (slow) refresh — a page written DURING the
        # refresh would otherwise bump last_seen to a value the just-started index never saw, so its
        # change would read as "already indexed" and never trigger the incremental branch until the
        # next full refresh hours later (invariant-audit M9). Capturing first keeps it pending.
        seen_before = _vault_max_mtime()
        ok = _refresh_index()                     # registers collection + full incremental
        done = time.monotonic()
        state["last_full"] = now                  # advance the periodic clock (no tight retry loop)
        state["cooldown_until"] = done + _index_update_cooldown(done - now)
        if ok:
            state["last_seen"] = seen_before      # mark the index current ONLY if the refresh worked;
        # on failure last_seen is unchanged, so the incremental branch keeps trying to catch the
        # pending changes rather than silently treating a failed full refresh as up-to-date (audit).
        return
    cur = _vault_max_mtime()
    # a page changed since the last index AND the cooldown has passed; skipped
    # changes stay pending (last_seen unchanged) and coalesce into one update
    if cur > state["last_seen"] and now >= state["cooldown_until"]:
        rc, _ = _qmd(["update"])
        done = time.monotonic()
        print(f"okengine-mcp: qmd index update on vault change rc={rc} ({done - now:.1f}s)",
              file=sys.stderr, flush=True)
        state["cooldown_until"] = done + _index_update_cooldown(done - now)
        if rc == 0:
            state["last_seen"] = cur              # mark indexed ONLY on success; a failed update
            _set_index_status(ready=True, error="")
        # leaves last_seen so the change is retried next poll, not silently lost (invariant-audit).


def _index_maintainer() -> None:
    """Keep the qmd index fresh. A full refresh on start + every REFRESH_HOURS catches deletes /
    orphaned hashes; BETWEEN those, poll the vault every POLL_SECONDS and run an incremental
    `qmd update` when pages change — debounced by _index_update_cooldown so a write burst
    can't starve the container (an idle vault's first write still indexes on the next poll,
    keeping the write -> recall loop of okengine#80)."""
    state = {"last_full": 0.0, "last_seen": -1.0, "cooldown_until": 0.0}
    while True:
        try:
            _index_maintainer_step(state)
        except Exception as e:                        # never let an error kill the thread
            print(f"okengine-mcp: index maintainer error: {e}", file=sys.stderr, flush=True)
        time.sleep(_INDEX_POLL_SECONDS if _INDEX_POLL_SECONDS > 0 else _INDEX_REFRESH_HOURS * 3600)


if __name__ == "__main__":
    transport = os.environ.get("OKENGINE_MCP_TRANSPORT", "stdio")
    if transport in ("streamable-http", "http"):
        import uvicorn
        app = mcp.streamable_http_app()
        # Local-first: the service ALWAYS comes up with a token (the built-in
        # default if OKENGINE_MCP_TOKEN is unset), so `docker compose up` just
        # works. The container binds 0.0.0.0 (OKENGINE_MCP_HOST) because Docker
        # port-forwarding requires it; LAN exposure is gated at the host-port
        # mapping (loopback by default — see docker-compose.yml), NOT here.
        # OKENGINE_MCP_ALLOW_UNAUTHENTICATED=1 is an explicit opt-out to serve
        # with no auth at all.
        host = os.environ.get("OKENGINE_MCP_HOST", "127.0.0.1")
        token, warning = _resolve_http_auth(os.environ, host)
        if warning:
            print(f"WARNING: okengine-mcp {warning}", file=sys.stderr, flush=True)
        if token is not None:
            app = _ScopedAuth(app, token)
        # Self-maintain the search index (qmd is only in this container; cron-plus can't).
        if _INDEX_REFRESH_HOURS > 0:
            _set_index_status(active=True, ready=False, error="")
            threading.Thread(target=_index_maintainer, name="qmd-index-maintainer",
                             daemon=True).start()
        uvicorn.run(app, host=host, port=int(os.environ.get("PORT", "8730")))
    else:
        mcp.run(transport=transport)
