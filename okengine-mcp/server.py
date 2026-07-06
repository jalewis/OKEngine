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
  find_references(target)               — IWE backlinks / resolved refs (via kb_graph)
  retrieve_context(path)                — a page + its expanded graph neighbourhood (IWE)
  graph_stats()                         — orphans / most-referenced / hierarchy (IWE)
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

import contextvars
import hmac
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scope as _scope  # noqa: E402  per-extension token resolution (okengine#132)

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


def _run(args: list[str], extra_env: dict | None = None, timeout: int = 90) -> str:
    env = {**os.environ, **(extra_env or {})}
    try:
        r = subprocess.run([PYBIN, *args], cwd=str(VAULT), env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "(query timed out)"
    out = (r.stdout or "").strip()
    return out or (r.stderr or "(no output)").strip()


def _safe(path: str) -> Path | None:
    """Resolve a wiki-relative path, refusing escapes outside the vault wiki/."""
    p = WIKI / path.lstrip("/")
    if p.suffix != ".md":
        p = p.with_suffix(".md")
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


@mcp.tool()
def search(query: str, mode: str = "search", limit: int = 8, tier: str = "") -> str:
    """Search the compiled knowledge base.

    mode: 'search' (default) — instant BM25 lexical; no model load, best for finding a
    known entity/term by name. 'hybrid' — BM25 + vector + rerank, better for
    concept/narrative queries but runs local models (slow on CPU without a GPU). Default
    is lexical for responsiveness; pass mode='hybrid' when a semantic match is needed.
    tier: optional comma list of hot,warm,cold to keep (G4 tier; empty = all tiers).
    Returns ranked passages, each with its vault path for provenance.
    """
    qmode = "search" if mode == "search" else "query"   # 'hybrid' -> qmd 'query'
    cmd = [str(SCRIPTS / "kb_search.py"), "--mode", qmode,
           "--limit", str(_clamp_limit(limit, 8)), str(query)]
    if (tier or "").strip():
        cmd += ["--tier", tier.strip()]
    return _run(cmd, extra_env=_QMD_ENV)[:8000]


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


@mcp.tool()
def find_references(target: str) -> str:
    """Knowledge-graph lookup via IWE: find pages matching `target` and show
    their resolved references / backlinks. Use to traverse the corpus graph."""
    return _run([str(SCRIPTS / "kb_graph.py"), "find", str(target)])[:8000]


@mcp.tool()
def retrieve_context(path: str) -> str:
    """Retrieve a page WITH its knowledge-graph context expanded (via IWE): the
    page plus its resolved outbound references and incoming backlinks, one hop out.
    Richer than get_page (which is the raw file) — use it to load a page together
    with its neighbourhood when you need the surrounding graph, not just the text.
    `path` is a vault page id/path, e.g. 'entities/a/example' or 'concepts/topic'."""
    if not _authorize_read(str(path)):
        return "(refused: outside this caller's read scope)"
    return _run([str(SCRIPTS / "kb_graph.py"), "retrieve", "-k", str(path)])[:16000]


@mcp.tool()
def graph_stats() -> str:
    """Knowledge-graph health/shape via IWE: orphans (pages nothing links to),
    most-referenced pages, and the link hierarchy over the whole vault. Use to
    find under-connected pages or the corpus's hubs. No arguments."""
    return _run([str(SCRIPTS / "kb_graph.py"), "stats"])[:8000]


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


def _qmd(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    try:
        r = subprocess.run([_QMD_BIN, *args], cwd=str(VAULT), env={**os.environ, **_QMD_ENV},
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, "qmd not installed"
    except subprocess.TimeoutExpired:
        return 124, "qmd timed out"


def _refresh_index() -> None:
    """Ensure the wiki collection is registered, then incrementally refresh the index."""
    rc, out = _qmd(["collection", "list"], timeout=60)
    if rc == 127:
        print("okengine-mcp: qmd not found — skipping index maintenance", file=sys.stderr, flush=True)
        return
    if "qmd://wiki" not in out:                       # not registered yet (e.g. fresh deploy)
        arc, _ = _qmd(["collection", "add", str(WIKI)])
        print(f"okengine-mcp: registered qmd 'wiki' collection (rc={arc})", file=sys.stderr, flush=True)
    rc, _ = _qmd(["update"])
    print(f"okengine-mcp: qmd index refresh rc={rc}", file=sys.stderr, flush=True)


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
    """Newest .md mtime under the wiki — a cheap change-detector for prompt reindex (okengine#80).
    Returns 0.0 if the tree is missing/empty."""
    newest = 0.0
    try:
        for root, _dirs, files in os.walk(WIKI):
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
        _refresh_index()                          # registers collection + full incremental
        done = time.monotonic()
        state["last_full"] = now
        state["last_seen"] = _vault_max_mtime()
        state["cooldown_until"] = done + _index_update_cooldown(done - now)
        return
    cur = _vault_max_mtime()
    # a page changed since the last index AND the cooldown has passed; skipped
    # changes stay pending (last_seen unchanged) and coalesce into one update
    if cur > state["last_seen"] and now >= state["cooldown_until"]:
        rc, _ = _qmd(["update"])
        done = time.monotonic()
        print(f"okengine-mcp: qmd index update on vault change rc={rc} ({done - now:.1f}s)",
              file=sys.stderr, flush=True)
        state["last_seen"] = cur
        state["cooldown_until"] = done + _index_update_cooldown(done - now)


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
            threading.Thread(target=_index_maintainer, name="qmd-index-maintainer",
                             daemon=True).start()
        uvicorn.run(app, host=host, port=int(os.environ.get("PORT", "8730")))
    else:
        mcp.run(transport=transport)
