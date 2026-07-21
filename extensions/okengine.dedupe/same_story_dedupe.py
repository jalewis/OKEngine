#!/usr/bin/env python3
"""okengine.dedupe:same-story — merge duplicate SOURCE pages describing the same story
(no_agent, ZERO LLM tokens).

On a composed multi-pack deployment the same article arrives through several packs' feed
lanes (raw/indicators/... AND raw/detections/... carrying one Hacker News item), and the
ingest lane mints a source page per raw copy — different slugs, same story. Every
per-story count downstream (actor news activity, CVE mentions, theme velocity) then
double-counts, and the boards show two rows for one event.

Detection is DELIBERATELY exact — a wrong merge is worse than a visible duplicate
(no-fabricated-facts). Three signals are computed per page:
  raw : a raw-file BASENAME  (one feed item fetched into two raw/ subtrees);
  url : a normalized URL     (redirector-unwrapped, scheme/www/query/fragment/
        trailing-slash stripped, stub paths rejected);
  hpd : host + URL path-slug + published DATE (survives an agent-mangled URL path
        segment — observed: /2026/05/ vs /2026/07/ for the same article slug).
Two pages merge only when they agree on >= 2 DISTINCT signal types. One signal is not
enough on real data: agents mis-stamp `raw:` (two different articles carrying one raw
basename) and a single fuzzy key can collide — either alone would tombstone a distinct
story. Two independent agreements is what makes an automatic merge safe. Title-similarity
matching is never attempted — distinct outlets covering one event are distinct sources.

Merge: the fullest body wins; list fields (sources/tags/aliases/related/raw) union into
the winner; missing scalars fill from the loser; inbound references anywhere in wiki/**
([[wikilink]] and plain-path frontmatter refs) rewrite to the winner; the loser is
TOMBSTONED per the engine convention (status: tombstoned + superseded_by + reason —
frontmatter and body preserved, so its `raw:` refs keep those raw files marked processed
and the write path refuses resurrection).

Env: WIKI_PATH (/opt/vault) · DEDUPE_STORY_BATCH (max merges/run, default 50)
Usage: same_story_dedupe.py [--vault DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.S)
_SKIP_NAMES = ("INDEX", "_", ".")


def _split(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def _raw_basenames(raw_val) -> list[str]:
    """Basenames of the page's raw-file ref(s). The field is a path, a comma-joined
    string of paths (importer quirk), or a list."""
    if not raw_val:
        return []
    items = raw_val if isinstance(raw_val, list) else re.split(r"[,\s]+", str(raw_val))
    out = []
    for it in items:
        it = str(it).strip().strip(",")
        if not it:
            continue
        base = it.rsplit("/", 1)[-1].lower()
        out.append(base[:-3] if base.endswith(".md") else base)
    return out


# Redirector hosts whose real target rides in a `?url=`/`?u=`/`?q=` param. Upstream news aggregators deliver
# many links as Google redirects (https://www.google.com/url?...&url=<real>), so without
# unwrapping every one of them normalizes to the SAME low-entropy key and union-find chains
# dozens of unrelated stories into one blob (okcti 2026-07-14 — a 29-page false group).
_REDIRECT_HOSTS = {"google.com", "google.co", "www.google.com", "l.facebook.com",
                   "out.reddit.com", "href.li", "t.co"}
_REDIRECT_PARAMS = ("url", "u", "q", "target")
# Normalized-URL path components too generic to identify a story on their own.
_STUB_PATHS = {"", "url", "search", "redirect", "r", "link", "out", "go", "click"}
# Web page extensions to strip from a URL slug. ONLY these — a bare `.` split would wreck
# dotted identifiers that are part of the id, not an extension (arXiv abs/2606.21349 -> 2606).
_WEB_EXTS = (".html", ".htm", ".php", ".aspx", ".asp", ".jsp", ".jspx", ".shtml", ".cfm")


def _slug_of(path: str) -> str:
    """Last path segment with a web extension stripped (but dotted ids preserved)."""
    slug = path.rsplit("/", 1)[-1].lower()
    for ext in _WEB_EXTS:
        if slug.endswith(ext):
            return slug[: -len(ext)]
    return slug


def _unwrap_redirect(u: str) -> str:
    """If `u` is a known redirector, return the real target from its query param; else `u`."""
    from urllib.parse import parse_qs
    try:
        p = urlparse(u)
    except ValueError:
        return u
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    if host not in {h[4:] if h.startswith("www.") else h for h in _REDIRECT_HOSTS}:
        return u
    q = parse_qs(p.query)
    for key in _REDIRECT_PARAMS:
        if q.get(key) and "://" in q[key][0]:
            return q[key][0]
    return u


def _norm_url(u: str) -> str | None:
    """Canonical host+path key, or None when it can't identify a story (stub/empty path).
    Redirector wrappers are unwrapped to the real target first."""
    if not u or "://" not in str(u):
        return None
    try:
        p = urlparse(_unwrap_redirect(str(u).strip()))
    except ValueError:
        return None
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = p.path.rstrip("/")
    if not host:
        return None
    if _slug_of(path) in _STUB_PATHS:             # no real story slug -> useless (unsafe) key
        return None
    return f"{host}{path}"


def _url_slug_key(u: str, published) -> str | None:
    """host + path-basename + published DATE — survives a mangled mid-path segment."""
    n = _norm_url(u)
    if not n or "/" not in n:
        return None
    host = n.split("/", 1)[0]
    slug = _slug_of(n)                             # web-ext stripped, dotted ids preserved
    date = str(published or "")[:10]
    if not slug or not date:
        return None
    return f"{host}|{slug}|{date}"


def group_keys(fm: dict) -> list[str]:
    """The exact-match grouping keys for one source page's frontmatter."""
    keys = [f"raw:{b}" for b in _raw_basenames(fm.get("raw"))]
    n = _norm_url(str(fm.get("url") or ""))
    if n:
        keys.append(f"url:{n}")
    hpd = _url_slug_key(str(fm.get("url") or ""), fm.get("published"))
    if hpd:
        keys.append(f"hpd:{hpd}")
    return keys


_TITLE_STOP = {"with", "from", "after", "that", "this", "into", "your", "being", "could",
               "three", "more", "than", "over", "what", "when", "have", "will", "using",
               "used", "uses", "about", "their", "them", "they", "were", "been", "also",
               "news", "report", "hacker", "the", "and", "for", "are", "new"}


def _page_title(fm: dict, body: str = "") -> str:
    """The page's title: `title:` frontmatter, else the body's first `# ` H1. Many okcti
    source pages keep the title ONLY as the body H1 (no frontmatter title) — the veto must
    see it, or a corrupt-provenance page with no fm title slips through (the npm/ModHeader
    case: raw+url matched, fm title absent, body H1 was the real 'npm botnet' story)."""
    t = fm.get("title")
    if t:
        return str(t)
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
        if s and not s.startswith(("#", "-", ">", "|", "!", "[")):
            return ""                                # first real content line isn't an H1 -> no title
    return ""


def _title_tokens(fm: dict, body: str = "") -> set:
    """Distinctive lowercase tokens of a page's title (len>=4, minus stopwords) — used only
    as a NEGATIVE signal: two pages whose provenance matches but whose titles share nothing
    are NOT safely the same story (a mis-stamped `raw:`/`url:` points a distinct article at
    another's provenance). We never MATCH on titles (distinct outlets write alike); we veto."""
    t = _page_title(fm, body).lower()
    return {w for w in re.findall(r"[a-z0-9]{4,}", t) if w not in _TITLE_STOP}


def titles_agree(a_fm: dict, b_fm: dict, a_body: str = "", b_body: str = "") -> bool:
    """True unless the two titles are DISJOINT enough to distrust a provenance match. Lenient
    by design (only the egregiously-different case is vetoed): a garbled agent-retype of one
    headline still shares tokens; a wholly different story shares ~none. No title -> no veto."""
    a, b = _title_tokens(a_fm, a_body), _title_tokens(b_fm, b_body)
    if not a or not b:
        return True                                  # nothing to judge -> don't veto
    shared = a & b
    if len(shared) >= 2:
        return True
    jac = len(shared) / len(a | b)
    return jac >= 0.12


_UNION = ("sources", "tags", "aliases", "related", "raw")


def merge_fm(winner: dict, loser: dict) -> dict:
    """Union list fields into the winner; fill missing scalars; never overwrite."""
    out = dict(winner)
    for k in _UNION:
        w = out.get(k)
        l = loser.get(k)
        if not l:
            continue
        wl = w if isinstance(w, list) else ([w] if w else [])
        ll = l if isinstance(l, list) else [l]
        seen, merged = set(), []
        for item in wl + ll:
            key = str(item).strip().strip(",").lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(str(item).strip().strip(","))
        out[k] = merged if len(merged) > 1 else (merged[0] if merged else None)
    for k, v in loser.items():
        if k not in out or out[k] in (None, "", [], {}):
            if v not in (None, "", [], {}) and k not in ("status", "superseded_by",
                                                         "tombstone_reason"):
                out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    batch = int(os.environ.get("DEDUPE_STORY_BATCH", "50"))
    # A genuine same-story group is one article fetched by a handful of pack lanes — a few
    # pages. A large group means a low-entropy key over-matched and union-find chained
    # unrelated stories; merging it would destroy real content. Report, never merge.
    max_group = int(os.environ.get("DEDUPE_STORY_MAX_GROUP", "6"))
    wiki = Path(args.vault) / "wiki"
    src = wiki / "sources"
    if not src.is_dir():
        print("same-story: no sources/ — nothing to do")
        print(json.dumps({"wakeAgent": False}))
        return 0

    # 1) scan (tolerant — pages move mid-scan)
    pages: dict[str, dict] = {}                     # rel -> {fm, body, keys}
    key2rels: dict[str, list[str]] = {}
    for p in src.rglob("*.md"):
        if p.name.startswith(_SKIP_NAMES):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # page moved/deleted by a concurrent lane mid-scan
        fm, body = _split(text)
        if not fm or str(fm.get("status") or "").lower() == "tombstoned":
            continue
        rel = p.relative_to(wiki).as_posix()[:-3]
        keys = group_keys(fm)
        pages[rel] = {"fm": fm, "body": body, "keys": set(keys)}
        for k in keys:
            key2rels.setdefault(k, []).append(rel)

    # 2) union-find, but a pair merges ONLY when it agrees on >=2 distinct key TYPES
    #    (raw / url / hpd). One signal is not enough: agents mis-stamp `raw:` (two different
    #    articles pointing at one raw file) and a single fuzzy key (hpd) can collide — either
    #    alone would tombstone a distinct story. Requiring two independent signals to agree
    #    is what makes an automatic merge safe (okcti 2026-07-14: raw-only and hpd-only false
    #    pairs both correctly left un-merged; the real balochistan dup shares raw+hpd).
    def _kt(k: str) -> str:
        return k.split(":", 1)[0]

    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    seen_pairs: set = set()
    for rels in key2rels.values():
        if len(rels) < 2 or len(rels) > 200:       # skip huge low-entropy buckets outright
            continue
        for i in range(len(rels)):
            for j in range(i + 1, len(rels)):
                a, b = rels[i], rels[j]
                pair = (a, b) if a < b else (b, a)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                shared_types = {_kt(k) for k in pages[a]["keys"] & pages[b]["keys"]}
                if len(shared_types) >= 2:
                    parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for rel in pages:
        if rel in parent:
            groups.setdefault(find(rel), []).append(rel)
    all_dups = sorted([sorted(g) for g in groups.values() if len(g) > 1])
    oversized = [g for g in all_dups if len(g) > max_group]
    dup_groups = [g for g in all_dups if len(g) <= max_group][:batch]

    for g in oversized:
        print(f"  SKIP oversized group ({len(g)} pages > {max_group}) — likely a low-entropy "
              f"key over-match, NOT merged: {g[0]} + {len(g) - 1} more")

    if not dup_groups:
        print(f"same-story: {len(pages)} source pages scanned, no mergeable duplicate stories"
              f"{f' ({len(oversized)} oversized group(s) skipped)' if oversized else ''}")
        print(json.dumps({"wakeAgent": False}))
        return 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    merged = rewritten = vetoed = 0
    for g in dup_groups:
        winner = max(g, key=lambda r: (len(pages[r]["body"]), -len(r)))
        # TITLE VETO: a member whose title is disjoint from the winner's is provenance-matched
        # but topically different (corrupt raw:/url:) — leave it un-merged and report it.
        losers, skipped = [], []
        for r in g:
            if r == winner:
                continue
            agree = titles_agree(pages[winner]["fm"], pages[r]["fm"],
                                 pages[winner]["body"], pages[r]["body"])
            (losers if agree else skipped).append(r)
        for s in skipped:
            print(f"  VETO title-disjoint (provenance matched, NOT merged): {s}  !=  {winner}")
            vetoed += 1
        if not losers:
            continue
        wfm = pages[winner]["fm"]
        for loser in losers:
            wfm = merge_fm(wfm, pages[loser]["fm"])
        print(f"  {winner}  <=  {', '.join(losers)}")
        if args.dry_run:
            merged += len(losers)
            continue
        # winner: merged frontmatter, body untouched
        head = yaml.safe_dump(wfm, sort_keys=False, allow_unicode=True).rstrip()
        (wiki / f"{winner}.md").write_text(
            f"---\n{head}\n---\n{pages[winner]['body']}", encoding="utf-8")
        # losers: tombstone (engine convention — fm+body preserved, raw: refs keep the
        # raw files marked processed; the write path refuses resurrection)
        for loser in losers:
            lfm = dict(pages[loser]["fm"])
            lfm["status"] = "tombstoned"
            lfm["tombstone_reason"] = f"same-story duplicate of {winner}"
            lfm["superseded_by"] = winner
            try:
                lfm["version"] = int(lfm.get("version", 1)) + 1
            except (TypeError, ValueError):
                lfm["version"] = 2
            lfm["last_updated"] = now
            lhead = yaml.safe_dump(lfm, sort_keys=False, allow_unicode=True).rstrip()
            (wiki / f"{loser}.md").write_text(
                f"---\n{lhead}\n---\n{pages[loser]['body']}", encoding="utf-8")
            merged += 1
        # 3) rewrite inbound references (wikilinks AND plain-path fm refs) to the winner
        loser_set = {l for l in losers}
        for p in wiki.rglob("*.md"):
            if p.name.startswith(_SKIP_NAMES):
                continue
            rel = p.relative_to(wiki).as_posix()[:-3]
            if rel == winner or rel in loser_set:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            new = text
            for loser in losers:
                if loser in new:
                    new = new.replace(loser, winner)
            if new != text:
                try:
                    p.write_text(new, encoding="utf-8")
                    rewritten += 1
                except OSError:
                    pass

    print(f"same-story: {len(dup_groups)} duplicate group(s), {merged} page(s) "
          f"{'would be ' if args.dry_run else ''}tombstoned, "
          f"{rewritten} citing page(s) rewritten"
          f"{f', {vetoed} title-vetoed' if vetoed else ''}"
          f"{f', {len(oversized)} oversized group(s) skipped' if oversized else ''}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
