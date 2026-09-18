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

from okengine.html_sanitize import restore_panel_svg, sanitize, stash_panel_svg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

_FM_SCAN_BYTES = 262_144


def split_fm(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        # This is the hot path for every landing-tab namespace scan. ``safe_load`` always selects
        # Python's SafeLoader; explicitly selecting CSafeLoader when available cuts large-vault
        # frontmatter parsing several-fold while retaining the same non-constructor-safe grammar.
        fm = yaml.load(m.group(1), Loader=_FAST_YAML_LOADER) or {}  # nosec B506
    except (yaml.YAMLError, ValueError):
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _humanize(s: str) -> str:
    """Slug/key -> display title, preserving common initialisms a naive `.title()` mangles
    (`ai-research` -> "AI Research", not "Ai Research"; `iot` -> "IoT"). Generic acronym set only —
    domain-specific acronyms live in the pack that generates the content."""
    words = re.sub(r"[-_]+", " ", str(s)).split()
    return " ".join(_DISPLAY_ACRONYMS.get(w.lower(), w.capitalize()) for w in words)


def load_application_declaration(vault: Path) -> dict | None:
    """Load the pack's active application binding for the read-only Cockpit surface.

    Deployment validation remains the authority for conformance. Cockpit only normalizes the
    already-deployed declaration so an application cannot be operational yet invisible.
    """
    path = vault / ".okengine" / "application.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict) or not str(raw.get("profile") or "").strip():
        return None
    propositions = (
        (raw.get("bindings") or {}).get("propositions")
        if isinstance(raw.get("bindings"), dict)
        else []
    )
    roles = (
        (raw.get("bindings") or {}).get("roles") if isinstance(raw.get("bindings"), dict) else {}
    )
    return {
        "profile": str(raw["profile"]).strip(),
        "profile_version": str(raw.get("profile_version") or "").strip(),
        "propositions": [row for row in (propositions or []) if isinstance(row, dict)],
        "roles": roles if isinstance(roles, dict) else {},
        "surfaces": raw.get("surfaces") if isinstance(raw.get("surfaces"), dict) else {},
        "queues": raw.get("queues") if isinstance(raw.get("queues"), dict) else {},
        "success_measures": (
            raw.get("success_measures") if isinstance(raw.get("success_measures"), dict) else {}
        ),
    }


def load_cockpit_config(vault: Path) -> dict:
    """Parse the OPTIONAL `cockpit:` block from <vault>/schema.yaml into a normalized
    config with generic defaults. PURE (no caching / no globals) so it is importable
    and unit-testable. Reads the governing composed artifact when present,
    otherwise schema.yaml; no hermes import."""
    raw: dict = {}
    sp = _governing_schema_path(vault)
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
    # The document title may be descriptive while the toolbar has deliberately scarce space.
    # With no explicit compact label, preserve the original single-title behaviour.
    short_title = str(raw.get("short_title") or "").strip() or title

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
            cfg = {
                "key": key,
                "label": str(s.get("label") or key).strip() or key,
                "dir": d,
                "pdf": bool(s.get("pdf")),
            }
            if s.get("type"):
                cfg["type"] = str(s["type"]).strip()
            if s.get("glob"):
                cfg["glob"] = str(s["glob"]).strip()
            streams.append(cfg)
    if not streams:
        streams = [
            {"key": "briefings", "label": "Recent briefings", "dir": "briefings", "pdf": False}
        ]
    streams_by_key = {s["key"]: s for s in streams}

    # watchlist — OPTIONAL tracker tab; absent => watchlist + competitors hidden
    watchlist: dict | None = None
    rw = raw.get("watchlist")
    if isinstance(rw, dict):
        lbl = rw.get("labels") if isinstance(rw.get("labels"), dict) else {}
        ets = [str(t).strip() for t in (rw.get("entity_types") or []) if str(t).strip()]
        watchlist = {
            "entity_dir": str(rw.get("entity_dir") or "entities").strip().strip("/"),
            "entity_types": ets,  # empty => all types
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
    for c in raw.get("competitors") or []:
        if isinstance(c, dict) and c.get("path"):
            comps.append(
                {
                    "key": str(c.get("key") or c["path"]).strip(),
                    "path": str(c["path"]).strip().strip("/"),
                }
            )

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
    tab_aliases = (
        {
            str(k).strip(): str(v).strip()
            for k, v in (raw.get("tab_aliases") or {}).items()
            if str(k).strip() and str(v).strip()
        }
        if isinstance(raw.get("tab_aliases"), dict)
        else {}
    )

    # Application contracts are operational metadata surfaced generically from Ops. A pack may
    # independently define an `application` dataset tab as its domain workspace; keeping these two
    # concepts separate prevents contract/status plumbing from displacing the work itself.
    application = load_application_declaration(vault)

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
                fields = [str(f).strip() for f in order if f is not None and str(f).strip()]
                if fields:
                    profiles[str(t).strip()] = fields

    return {
        "title": title,
        "short_title": short_title,
        "streams": streams,
        "streams_by_key": streams_by_key,
        "watchlist": watchlist,
        "competitors": comps,
        "predictions_dirs": pdirs,
        "tabs": tabs,
        "dashboards": dashboards,
        "tab_defs": tab_defs,
        "tab_aliases": tab_aliases,
        "profiles": profiles,
        "application": application,
    }


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


def _deref_local_links(s: str) -> str:
    """Flatten internal markdown links to their text for portable export — `[APT41](entities/a/apt41)`
    -> `APT41`. External http(s)/mailto links are kept. Vault paths resolve only inside the reader,
    so an exported md/docx/pdf must not carry them as dead links."""
    return _MD_LOCAL_LINK.sub(r"\1", s)


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
            try:
                cp.relative_to(WIKI.resolve())
            except ValueError:
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
        if not target:  # same-page anchor [[#heading]] — no page
            return disp
        return f'<a class="wl" data-page="{target.replace(chr(34), "&quot;")}">{disp}</a>'

    s = _WIKILINK.sub(repl, s)
    # drop dangling "[[" from truncated wikilinks (source cells cut with …/(+N))
    return re.sub(r"\[\[(?![^\]\n]*\]\])", "", s)


def _strip_md(s: str) -> str:
    """Claim text as clean plain prose (for search + truncated cells)."""
    s = _delink(s or "")
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)  # [text](url) -> text
    return s.replace("**", "").replace("__", "").replace("`", "").strip()


def _inline_md(s: str) -> str:
    """Render claim INLINE: bold/code/links + clickable [[wikilinks]]. Trusted vault
    content, but HTML-escaped before adding our own tags."""
    s = (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = _linkify(s)  # [[wl]] -> <a class=wl>
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


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
            return (
                f'<a class="ext" href="{_esc(url)}" target="_blank" rel="noopener noreferrer"'
                f' title="original article">{label}</a>'
            )
        return f'<a class="wl" data-page="{_esc(rel)}">{label}</a>'

    return _SRC_LINK.sub(_enrich, html)


def _uncode_wikilinks(s: str) -> str:
    return _UNCODE_WIKILINK.sub(r"\1", s)


def render_md(body: str) -> str:
    body = _resolve_embeds(body)
    body = re.sub(
        r"```dataview(js)?\n.*?\n```",
        "_[Dataview view — open in Obsidian to compute]_",
        body,
        flags=re.DOTALL,
    )
    body, stash = stash_panel_svg(body)
    body = _uncode_wikilinks(body)
    body = _linkify(body)
    html = md.markdown(body, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    html = restore_panel_svg(html, stash)
    # The served HTML lands in the browser via innerHTML and the source is agent/feed-derived:
    # sanitize LAST, through the allowlist shared with the reader (okengine#659).
    return sanitize(_link_originals(html))


def safe_read(base: Path, rel: str) -> str:
    """Read a file strictly under `base` (path-traversal guard). Read-only."""
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(404, "not found")
    if not p.is_file():
        raise HTTPException(404, "not found")
    return p.read_text(encoding="utf-8", errors="replace")


def _file_date(name: str) -> str | None:
    m = _DATE_RE.search(name)
    return m.group(1) if m else None


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
    return {
        "title": cfg["title"],
        "short_title": cfg["short_title"],
        "tabs": tabs,
        "watchlist": cfg["watchlist"] is not None,
        # labels for pack-defined dataset tabs (the frontend builds their panes dynamically)
        "tab_labels": {k: v["label"] for k, v in (cfg.get("tab_defs") or {}).items()},
        "chat_enabled": _chat_enabled(),  # gate the Chat tab on a configured agent
        # deployment timezone for the UI clock (okengine#301) — the container starts with $TZ
        # (compose passes TZ=${TZ:-UTC}); the clock renders in this zone, not hardcoded UTC.
        "tz": os.environ.get("TZ") or "UTC",
        "review_enabled": _REVIEW_ENABLED,
        "review_auth_mode": _REVIEW_AUTH_MODE,
    }
