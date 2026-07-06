#!/usr/bin/env python3
"""Backlink-graph build: invert IWE forward-references into {target: [referrers]}.

Canonical home of the invert + source-filter + title logic (okengine#168). The
`backlinks-refresh` cron uses this to precompute wiki/.backlinks.json once per
deployment per day; the reader and cockpit serve that artifact instead of each
running the heavy `iwe find -f json -l 0` full-graph dump (~minutes / ~2GB RSS
on a large vault) inside their own containers. The UIs keep a live-iwe build
ONLY as a fallback for a missing/stale artifact — when touching the filter
semantics here, mirror the change in okengine-reader/app.py
(_skip_backlink_src) so the degraded path doesn't drift.

Filter semantics (same exclusions the reader rail applies):
  - reserved/generated file names: dot/underscore prefixes, backups,
    INDEX.md / INDEX-pNN.md / index.md, and the root HOT.md / log.md;
  - namespaces the pack's schema.yaml `exclude:`s (e.g. operational/);
  - dashboards/ — surfaced for READING (okengine#117) but its generated
    digests aren't meaningful "what links here" edges.

Titles are curated, not IWE's (IWE's "title" is the first heading of ANY
level, which surfaces `## Summary` for most sources): frontmatter
`title`/`name`, else the true `# H1`, else the de-slugged basename.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_FM_HEAD_BYTES = 8192
# dashboards/ is schema-excluded but reader-surfaced; it stays excluded as a
# backlink SOURCE either way, so the filter set is simply exclude ∪ this.
_SURFACED_DERIVED = frozenset({"dashboards"})
# DEFAULT namespaces dropped from the backlink graph in BOTH directions (as referrer AND as
# target) — beyond the schema `exclude:` set (which only drops referrers). Overridable per pack
# via schema.yaml `backlink_drop: [...]`; set `backlink_drop: []` to re-include sources.
#
# This is a PRODUCT choice, NOT a performance one: since okengine#179 the graph is built by a
# link-scanner that skips sources cheaply, so cost is no longer a factor. sources/ is the
# raw-ingest tree and dominates the graph both ways (observed: 46% of targets, 68% of edges);
# dropping it keeps entity/concept backlinks a CURATED knowledge graph rather than a wall of
# raw-article mentions, and shrinks the artifact ~86% (10.8MB -> 1.5MB). Trade-off: source pages
# lose "cited by" and entities lose "mentioned in article X" backlinks (a page's forward
# `sources:` links still carry that provenance). A vault that wants the source-mention trail in
# "what links here" can re-include it with `backlink_drop: []`.
_BACKLINK_DROPPED = frozenset({"sources"})
_RESERVED_ROOT_NAMES = frozenset({"HOT.md", "log.md"})

ARTIFACT_NAME = ".backlinks.json"
ARTIFACT_VERSION = 1


def _first_seg(e) -> str:
    seg = str(e).strip().strip("/")
    if seg.startswith("wiki/"):
        seg = seg[len("wiki/"):]
    return seg.strip("/").split("/")[0]


def excluded_top_dirs(vault_root: Path) -> frozenset[str]:
    """Top-level wiki/ namespace segments excluded from the backlink graph: the surfaced-derived
    dirs (dashboards/) + the schema's `exclude:` set (referrer-side) + the backlink-drop set. The
    drop set is `schema.yaml backlink_drop:` when the key is present (a pack knob — `[]` re-includes
    sources), else the default _BACKLINK_DROPPED ({sources})."""
    out: set[str] = set(_SURFACED_DERIVED)
    drop: set[str] = set(_BACKLINK_DROPPED)
    sp = vault_root / "schema.yaml"
    if sp.is_file():
        try:
            import yaml
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            if "backlink_drop" in sch:   # explicit pack override (incl. [] to re-include sources)
                drop = {s for s in (_first_seg(x) for x in (sch.get("backlink_drop") or [])) if s}
            for e in (sch.get("exclude") or []):
                if (seg := _first_seg(e)):
                    out.add(seg)
        except Exception:
            pass
    out |= drop
    return frozenset(out)


def skip_name(name: str) -> bool:
    """Reserved / non-content / generated file names (reader's _skip)."""
    return (name.startswith(("_", ".")) or ".bak." in name
            or name in ("INDEX.md", "index.md")
            or name.startswith(("INDEX-", "index-")))


def skip_source(key: str, excluded: frozenset[str]) -> bool:
    """True if a backlink *source* doc is generated/operational machinery whose
    links must not contribute "what links here" edges."""
    name = key.split("/")[-1]
    if not name.endswith(".md"):
        name += ".md"
    if skip_name(name) or name in _RESERVED_ROOT_NAMES:
        return True
    ns = key.split("/")[0] if "/" in key else ""
    return bool(ns) and ns in excluded


def page_title(wiki_root: Path, key: str) -> str:
    """Curated label for a source page: frontmatter title/name → # H1 →
    de-slugged basename."""
    try:
        head = (wiki_root / f"{key}.md").open("rb").read(_FM_HEAD_BYTES)
        text = head.decode("utf-8", errors="replace")
    except OSError:
        text = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            fm_text, body = text[3:end], text[end + 4:]
            try:
                import yaml
                fm = yaml.safe_load(fm_text) or {}
                t = str(fm.get("title") or fm.get("name") or "").strip()
                if t:
                    return t
            except Exception:
                pass
            text = body
    h1 = _H1_RE.search(text)
    if h1:
        return h1.group(1).strip()
    return key.split("/")[-1].replace("-", " ").strip() or key


def invert(docs: list, wiki_root: Path, excluded: frozenset[str]) -> dict:
    """Invert IWE's per-doc forward-references into {target: [{key,title}]},
    filtered, deduped, title-sorted — the exact map the UIs serve."""
    bl: dict[str, list] = {}
    titles: dict[str, str] = {}
    for d in docs:
        src = d.get("key")
        if not src or skip_source(src, excluded):
            continue
        refs = d.get("references") or []
        if not refs:
            continue
        if src not in titles:
            titles[src] = page_title(wiki_root, src)
        for ref in refs:
            tgt = ref.get("key")
            # Excluded namespaces (sources/ via _BACKLINK_DROPPED, dashboards/, schema
            # `exclude:`) are dropped as TARGETS too — not just as referrers — so nothing
            # accumulates a backlink list for a raw-ingest page.
            if not tgt or tgt == src or skip_source(tgt, excluded):
                continue
            bl.setdefault(tgt, []).append({"key": src, "title": titles[src]})
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


def build_artifact(docs: list, wiki_root: Path, vault_root: Path,
                   built_at: float) -> dict:
    """The full wiki/.backlinks.json payload (map + provenance meta)."""
    excluded = excluded_top_dirs(vault_root)
    bl = invert(docs, wiki_root, excluded)
    return {
        "version": ARTIFACT_VERSION,
        "built_at": int(built_at),
        "pages": len(docs),
        "targets": len(bl),
        "edges": sum(len(v) for v in bl.values()),
        "excluded_namespaces": sorted(excluded),
        "backlinks": bl,
    }


def write_artifact(artifact: dict, wiki_root: Path) -> Path:
    """Atomic write (tmp + rename) so UI readers never see a torn file."""
    out = wiki_root / ARTIFACT_NAME
    tmp = wiki_root / (ARTIFACT_NAME + ".tmp")
    tmp.write_text(json.dumps(artifact, ensure_ascii=False,
                              separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)
    return out


# ── forward-reference scan (okengine#179) ────────────────────────────────────
# Replaces the `iwe find -f json -l 0` full-graph dump — which parses the WHOLE vault
# (~4GB RSS / ~550s on a 52k-file vault, right at the cron's 600s timeout) — with a direct
# markdown link scan over only the NON-excluded tree. Measured ~38x faster / ~40x less
# memory (14s/104MB vs ~530s/~4.2GB) at 99.99% edge parity with iwe; the residual diff is
# links iwe's markdown parser drops inside GFM table cells (the `|` collides with [[key|label]]).
#
# iwe link semantics reproduced (reverse-engineered against iwe 0.3.2 over the real vault):
#   - [[key]] and [[key|label]]  -> reference `key` (text before `|`/`#`; the label may wrap
#     across newlines); markdown [text](path.md) -> `path` (relative-resolved, `.md` stripped)
#   - links in frontmatter, code spans/fences, and external URLs are NOT edges
#   - the target resolves by BASENAME to the real doc key (the vault shards by first letter,
#     so a link's literal path rarely matches); exact-key wins; a basename collision takes the
#     alphabetically-first key; an unresolved link stays literal (dangling, like iwe).
_SCAN_FM = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)
_SCAN_WIKI = re.compile(r"\[\[([^\]]+?)\]\]", re.DOTALL)
_SCAN_MD = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?)\)")
_SCAN_FENCE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[^\n]*$", re.DOTALL | re.MULTILINE)
_SCAN_INLINE = re.compile(r"(`+)[^\n]*?\1")


def _scan_strip(text: str) -> str:
    """Drop frontmatter + code spans/fences before link extraction (iwe parses the AST;
    links inside those are not edges)."""
    m = _SCAN_FM.match(text)
    if m:
        text = text[m.end():]
    return _SCAN_INLINE.sub(" ", _SCAN_FENCE.sub("\n", text))


def _wikilink_key(inner: str) -> str | None:
    k = inner.split("|", 1)[0].split("\n", 1)[0].split("#", 1)[0].strip()
    if not k or k.startswith(("http://", "https://", "mailto:")):
        return None
    return k[:-3] if k.endswith(".md") else k


def _mdlink_key(url: str, doc_dir: str) -> str | None:
    u = url.split("#", 1)[0].strip()
    if not u or u.startswith(("http://", "https://", "mailto:", "#")) or not u.endswith(".md"):
        return None
    rel = os.path.normpath(os.path.join(doc_dir, u))
    return None if rel.startswith("..") else rel[:-3]


def scan_forward_refs(wiki_root: Path, excluded: frozenset[str]) -> list:
    """Scan the vault for forward references (iwe-parity), returning iwe-shaped docs
    ``[{"key", "references": [{"key"}]}]`` for :func:`invert`. Only NON-excluded, non-reserved
    docs are READ (the perf win — excluded namespaces like sources/ are never opened); ALL doc
    paths seed the resolver index so links resolve to the same keys iwe produces."""
    paths = list(wiki_root.rglob("*.md"))
    keys = [p.relative_to(wiki_root).as_posix()[:-3] for p in paths]
    keyset = set(keys)
    by_base: dict[str, list] = {}
    for k in keys:
        by_base.setdefault(k.rsplit("/", 1)[-1], []).append(k)
    for lst in by_base.values():
        lst.sort()  # deterministic alphabetically-first collision pick

    def resolve(raw: str) -> str:
        if raw in keyset:
            return raw
        cands = by_base.get(raw.rsplit("/", 1)[-1])
        return cands[0] if cands else raw

    docs = []
    for p, key in zip(paths, keys):
        if skip_source(key, excluded):   # excluded namespace or reserved/generated name -> don't read
            continue
        try:
            body = _scan_strip(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        doc_dir = key.rsplit("/", 1)[0] if "/" in key else ""
        refs, seen = [], set()
        for rx, keyfn in ((_SCAN_WIKI, lambda m: _wikilink_key(m.group(1))),
                          (_SCAN_MD, lambda m: _mdlink_key(m.group(1), doc_dir))):
            for m in rx.finditer(body):
                k = keyfn(m)
                if not k:
                    continue
                k = resolve(k)
                if k != key and k not in seen:
                    seen.add(k)
                    refs.append({"key": k})
        docs.append({"key": key, "references": refs})
    return docs
