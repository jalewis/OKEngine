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


def _artifact_backlinks() -> dict | None:
    """The cron-precomputed backlink map (wiki/.backlinks.json), or None when
    absent/stale/corrupt — callers then fall back to the bounded in-process scanner.
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
    m = data.get("backlinks") if isinstance(data, dict) else None
    if not isinstance(m, dict):
        return None
    _BL_ARTIFACT["map"] = m
    _BL_ARTIFACT["mtime"] = st.st_mtime
    return m


def _bl_skip_name(name: str) -> bool:
    return (
        name.startswith(("_", "."))
        or ".bak." in name
        or name in ("INDEX.md", "index.md")
        or name.startswith(("INDEX-", "index-"))
    )


def _backlink_drop_dirs() -> frozenset:
    """schema.yaml `backlink_drop:` (default {'sources'}; pack knob). MIRRORS backlink_lib."""
    global _BL_DROP_CACHE
    now = time.monotonic()
    if _BL_DROP_CACHE[1] is not None and now - _BL_DROP_CACHE[0] < _DIR_TTL:
        return _BL_DROP_CACHE[1]
    drop = {"sources"}
    sp = _governing_schema_path()
    if sp.is_file():
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            if "backlink_drop" in sch:
                drop = set()
                for e in sch.get("backlink_drop") or []:
                    seg = str(e).strip().strip("/")
                    if seg.startswith("wiki/"):
                        seg = seg[len("wiki/") :]
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
            text = text[end + 4 :]
    h1 = _H1_BL.search(text)
    return h1.group(1).strip() if h1 else (src.split("/")[-1].replace("-", " ").strip() or src)


def _bl_strip(text: str) -> str:
    m = _BL_FM.match(text)
    if m:
        text = text[m.end() :]
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
        for rx, kf in (
            (_BL_WIKI, lambda m: _bl_wikikey(m.group(1))),
            (_BL_MD, lambda m: _bl_mdkey(m.group(1), doc_dir)),
        ):
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
        return  # a build is already in progress
    try:
        stale = _BACKLINKS["map"] is None or time.monotonic() - _BACKLINKS["ts"] > _BACKLINKS_TTL
    finally:
        _BL_LOCK.release()
    if stale:
        threading.Thread(target=lambda: _load_backlinks(blocking=True), daemon=True).start()


def _load_backlinks(blocking: bool = True) -> dict:
    m = _artifact_backlinks()
    if m is not None:  # precomputed artifact wins — no iwe here
        return m
    now = time.monotonic()
    if _BACKLINKS["map"] is not None and now - _BACKLINKS["ts"] <= _BACKLINKS_TTL:
        return _BACKLINKS["map"]
    if not blocking:
        # Never block a request on a full-vault scan, even though the in-process scanner is bounded:
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


def api_backlinks(path: str = Query(...), limit: int = 100):
    """Docs that reference `path` via the precomputed wikilink graph. `path` is the
    wiki-relative key without .md (e.g. 'concepts/<name>')."""
    key = path[:-3] if path.endswith(".md") else path
    refs = _load_backlinks(blocking=False).get(key, [])  # never block the UI on a graph build
    # Typed "Related" rail: group referrers by their namespace (the first path segment == the OKF
    # type bucket — predictions/, findings/, entities/, dashboards/, …). Counts are over ALL
    # referrers; items are capped per group. Ordered most-connected first — generic, no domain
    # priority baked into the engine. (sources/ is already dropped from the graph upstream.)
    groups: dict[str, list] = {}
    for r in refs:
        rk = str(r.get("key") or "")
        ns = rk.split("/", 1)[0] if "/" in rk else "(root)"
        groups.setdefault(ns, []).append(r)
    grouped = [
        {"ns": ns, "label": _humanize(ns), "count": len(items), "items": items[:_BL_GROUP_CAP]}
        for ns, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return {
        "path": key,
        "count": len(refs),
        "groups": grouped,
        "backlinks": refs[: max(1, min(limit, 500))],
    }


def _skip(name: str) -> bool:
    """Reserved / generated files the browse rail never lists or renders (underscore/dot
    reserved, backups, the generated per-directory INDEX pages)."""
    return (
        name.startswith(("_", "."))
        or ".bak." in name
        or name in ("INDEX.md", "index.md")
        or name.startswith(("INDEX-", "index-"))
    )


def _within(base: Path, p: Path) -> bool:
    """True iff `p` (already resolved) is inside `base` — path-traversal guard."""
    try:
        p.relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _excluded_dirs() -> frozenset:
    """Top-level wiki/ dir names hidden from browse: the pack's schema.yaml `exclude:` set
    MINUS the surfaced synthesized namespaces (dashboards/). Cached (vault :ro)."""
    global _EXCLUDE_CACHE
    now = time.monotonic()
    if now - _EXCLUDE_CACHE[0] < _BROWSE_TTL:
        return _EXCLUDE_CACHE[1]
    out: set[str] = set()
    sp = _governing_schema_path()
    if sp.is_file():
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            out = excluded_namespaces_from_schema(sch)
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
    sp = _governing_schema_path()
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
    sp = _governing_schema_path()
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
    return [d.name for d in WIKI.iterdir() if d.is_dir() and not _skip(d.name) and d.name not in ex]


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
    return {
        "path": rel,
        "title": title,
        "type": str(fm.get("type") or "").strip(),
        "status": str(fm.get("status") or "").strip(),
        "updated": _disp_ts(fm.get("last_updated") or fm.get("updated") or fm.get("created")),
    }


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
            if _skip(p.name) or _hidden_page(
                p
            ):  # reserved sub-dirs + walk-up excluded (batch-2 re-verify)
                continue
            out.append(_page_meta(p.resolve()))
    out.sort(key=lambda r: (r["title"].lower(), r["path"]))
    _BROWSE_CACHE[sub] = (now, out)
    return out


def _pages_of_types(types: frozenset) -> list[dict]:
    """Every page across all non-excluded namespaces whose `type` is in `types`."""
    out = [pg for sub in _top_dirs() for pg in _scan_dir(sub)
           if pg["type"].lower() in types
           and pg.get("status", "").lower() != "tombstoned"]
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
    return {"vault": str(WIKI), "dirs": dirs, "top_section": {"label": label, "namespaces": top}}


def api_groups():
    """Pack-declared display groups (label -> page count) — browse entities BY KIND across
    namespaces. A declared-but-UNPOPULATED kind (0 pages, e.g. a 'Report vendors' group whose
    type was never ingested) is omitted, matching how /api/tree hides empty namespaces — else the
    browse rail shows a dead '… 0' row (okengine#259, Browse cleanup). Empty when none populated."""
    out = []
    for label, types in _display_groups():
        n = len(_pages_of_types(types))
        if n:
            out.append({"label": label, "count": n})
    return {"groups": out}


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


def _about_info() -> dict:
    """Deployment identity for the About panel: vault name + version (pack.yaml) and the
    engine/Hermes pins. Read fresh — both files are tiny and About is cold."""
    info = {
        "vault": "",
        "vault_version": "",
        "engine_version": "",
        "hermes_pin": "",
        "project_url": "",
    }

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
        cm = (
            (VAULT / "CLAUDE.md").read_text(encoding="utf-8")
            if (VAULT / "CLAUDE.md").is_file()
            else ""
        )
        info["installed_domains"] = [
            ln[len("## Installed domain:") :].strip()
            for ln in cm.splitlines()
            if ln.startswith("## Installed domain:")
        ]
    except OSError:
        info["installed_domains"] = []
    try:
        info["sub_domains"] = sorted(
            d.name for d in WIKI.iterdir() if d.is_dir() and (d / "schema.yaml").is_file()
        )
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
                exts.append(
                    {
                        "id": str(x.get("id") or ""),
                        "name": str(x.get("name") or x.get("id") or ""),
                        "description": str(x.get("description") or ""),
                    }
                )
            else:
                exts.append({"id": str(x), "name": str(x), "description": ""})
        info["extensions"] = sorted(exts, key=lambda e: e["id"])
    else:
        ext = _yaml(VAULT / ".okengine" / "extensions.yaml")
        ids = (
            sorted((ext.get("enabled") or {}).keys())
            if isinstance(ext.get("enabled"), dict)
            else []
        )
        info["extensions"] = [{"id": i, "name": i, "description": ""} for i in ids]
    ev = _yaml(VAULT / "engine.version")
    # Prefer the deploy-stamped runtime marker (the ACTUAL engine/Hermes running) over the pack's
    # DECLARED pins, which can be stale vs the deployed engine. Fall back to the declared pin.
    rt = _yaml(VAULT / ".hermes-data" / "engine-runtime.yaml")
    info["engine_version"] = str(rt.get("engine_release") or ev.get("version") or "")
    info["hermes_pin"] = str(rt.get("hermes_pin") or ev.get("hermes_pin") or "")
    info["project_url"] = os.environ.get("OKENGINE_PROJECT_URL") or str(pk.get("project_url") or "")
    return info


def api_about():
    """Vault name + engine/Hermes versions for the About panel."""
    info = _about_info()
    info["chat_enabled"] = _chat_enabled()
    return info
