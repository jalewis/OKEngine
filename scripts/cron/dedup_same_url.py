#!/usr/bin/env python3
"""dedup_same_url.py — merge live source pages that hold the same URL (okengine#886).

A source's identity is its URL, yet a vault can hold one article at several seats: importers and
model lanes filed it under different ids before identity was URL-derived. Every such pair doubles
counts and splits citations. `dedup_partition_collisions.py` repairs ONE slug at two paths; this
repairs one URL at different slugs — through the write path (tombstoning losers, never deleting),
where that older lane writes files directly.

Grouping is by the write path's own `_norm_url`. Two classes of "same URL" are NOT the same
document, and the lane never fuses them:

  - PLACEHOLDER urls (`https://arxiv.org/abs/[pending]`, `{id}`, `/tbd`, example.com — the write
    path's `_PLACEHOLDER_URL`): one live vault filed 20 different papers under one. They are
    reported, and `--placeholders` repairs each from its own raw capture instead.
  - IMPOSTORS: a member whose title disagrees with the group (a different issue number, or little
    token overlap) carries a wrong URL. It is excluded, flagged `needs_review`, and reported.

Survivor (among the title-consistent majority): the page holding the URL-derived id; else most
inbound links; then longest body; then oldest. It takes the longest body (`needs_review` when two
substantive bodies differ), fields only a loser has, and every loser's raw backlinks. Inbound links
are rewritten to it, then each loser is tombstoned `superseded_by` it. An id never moves: an
id-less loser is first given a slug id from its own stem (it cannot be tombstoned without one).
A group stops at its first refused write, and that loser stays live.

Everything goes through the write path as `cron:dedup-same-url`, held to its own output_contract.
DRY RUN unless --apply. Env: WIKI_PATH (/opt/vault) · OKENGINE_MCP_DIR (/opt/hermes/okengine-mcp).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml
from yaml import YAMLError

sys.path.insert(0, str(Path(__file__).resolve().parent))  # staged siblings
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))  # scripts/ (backfill_ids)
import engine_package  # noqa: E402,F401 — makes `okengine` importable when loaded by path
from okengine.write_services.state import _PLACEHOLDER_URL  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
ACTOR = "cron:dedup-same-url"
SUBSTANTIVE = 600
MIN_OVERLAP = 0.34   # share of the shorter title's tokens two same-document titles must share
NEVER_COPY = {"id", "version", "created", "last_updated", "updated", "producer_lane", "status",
              "extension_id", "tombstone_reason", "superseded_by", "raw", "needs_review"}
_STOP = {"the", "and", "for", "with", "from", "into", "that", "this", "how", "what", "why",
         "are", "its", "new", "via", "using", "use", "our", "your"}
_YEAR = re.compile(r"^(?:19|20)\d\d\Z")
_SOURCE_REF = re.compile(r"sources/[A-Za-z0-9_./-]+")
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _write_server():
    """The write path under this lane's identity (mirrors a lane's writer MCP server)."""
    os.environ["OKENGINE_WRITE_ACTOR"] = ACTOR
    os.environ.setdefault("OKENGINE_CRON_JOBS", "/opt/data/cron-plus/jobs.json")
    os.environ.setdefault("OKENGINE_OUTPUT_CONTRACT_MODE", "enforce")
    mcp_dir = Path(os.environ.get("OKENGINE_MCP_DIR", "/opt/hermes/okengine-mcp"))
    sys.path[:0] = [str(mcp_dir), str(mcp_dir.parent / "scripts" / "cron"),
                    str(mcp_dir.parent / "scripts")]
    import write_server as W
    return W


def is_placeholder(url: object) -> bool:
    return bool(_PLACEHOLDER_URL.search(str(url or "").strip()))


def _history(rel: str) -> bool:
    return rel.startswith("_logs/") or rel == "log.md" or Path(rel).name.startswith(("INDEX", "_"))


def live_sources(W) -> dict[str, tuple[dict, str]]:
    """{`sources/…` rel without .md: (frontmatter, body)} for every live source page."""
    out: dict[str, tuple[dict, str]] = {}
    for p in sorted((WIKI / "sources").rglob("*.md")):
        rel = p.relative_to(WIKI).as_posix()
        if _history(rel):
            continue
        try:
            fm, body = W._read_page(p)
        except (OSError, ValueError):
            continue
        if isinstance(fm, dict) and str(fm.get("status") or "").lower() != "tombstoned":
            out[rel[:-3]] = (fm, body or "")
    return out


def _title_parts(title: object) -> tuple[set[str], set[str]]:
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    numbers = {w for w in words if w.isdigit() and len(w) >= 2 and not _YEAR.match(w)}
    tokens = {w.removesuffix("s") for w in words
              if len(w) >= 3 and w not in _STOP and not w.isdigit()}
    return tokens, numbers


def consistent(a: object, b: object) -> bool:
    """Whether two titles can name the same document. When both carry a distinctive number (an
    issue or version), the numbers decide either way: `Import AI 447` never matches `453`, and two
    differently-worded summaries of issue 453 are one document. Otherwise they must share
    MIN_OVERLAP of the shorter title's words. No title: undecidable — the URL is evidence enough."""
    (ta, na), (tb, nb) = _title_parts(a), _title_parts(b)
    if na and nb:
        return bool(na & nb)
    if not ta or not tb:
        return True
    return len(ta & tb) / min(len(ta), len(tb)) >= MIN_OVERLAP


def inbound(pages: set[str]) -> dict[str, set[str]]:
    """{candidate: live non-history pages linking it} — one pass over the wiki."""
    refs: dict[str, set[str]] = defaultdict(set)
    for p in sorted(WIKI.rglob("*.md")):
        rel = p.relative_to(WIKI).as_posix()
        if _history(rel):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FM.match(text)
        if m and re.search(r"^status:\s*['\"]?tombstoned\b", m.group(1), re.M | re.I):
            continue
        # A generated report (no frontmatter at all, or a `generator:` stamp) is rebuilt or is a
        # historical snapshot, never a citation. The write path refuses to rewrite such pages, and a
        # refusal stops the whole group, so a daily dashboard and a two-month-old lint report kept
        # 13 duplicates live on one vault (okengine#886). Its stale link still resolves: the retired
        # page is tombstoned with `superseded_by` the survivor.
        if not m or re.search(r"^generator:", m.group(1), re.M):
            continue
        for ref in {r.removesuffix(".md").rstrip("./") for r in _SOURCE_REF.findall(text)}:
            if ref in pages and ref != rel[:-3]:
                refs[ref].add(rel)
    return refs


def plan(W, pages: dict[str, tuple[dict, str]]) -> tuple[list[dict], list[str]]:
    """(merge groups, placeholder pages). Nothing is written."""
    by_url: dict[str, list[str]] = defaultdict(list)
    placeholders: list[str] = []
    for rel, (fm, _body) in pages.items():
        raw_url = str(fm.get("url") or "").strip()
        if raw_url and is_placeholder(raw_url):
            placeholders.append(rel)
            continue
        url = W._norm_url(raw_url)
        if url:
            by_url[url].append(rel)
    groups = {u: sorted(m) for u, m in by_url.items() if len(m) > 1}
    links = inbound({r for members in groups.values() for r in members})
    out = []
    for url, members in sorted(groups.items()):
        title = {r: pages[r][0].get("title") for r in members}
        uid = W.source_url_id(url)

        def rank(r: str) -> tuple:
            fm, body = pages[r]
            return (fm.get("id") != uid, -len(links[r]), -len("".join(body.split())),
                    str(fm.get("created") or "9999"), r)

        agree = {r: sum(consistent(title[r], title[o]) for o in members if o != r) for r in members}
        anchor = min(members, key=lambda r: (-agree[r], rank(r)))
        cluster = [r for r in members if consistent(title[anchor], title[r])]
        survivor = min(cluster, key=rank)
        losers = [r for r in cluster if r != survivor and consistent(title[survivor], title[r])]
        excluded = [r for r in members if r != survivor and r not in losers]
        if not losers:
            # No two members agree (e.g. three different papers filed under one URL): which one
            # the URL really belongs to is unknown, so every member is flagged, the "survivor" too.
            out.append({"url": url, "survivor": survivor, "losers": [],
                        "excluded": sorted([survivor] + excluded), "body_from": None,
                        "needs_review": False, "links": {}})
            continue
        body_from = max([survivor] + losers,
                        key=lambda r: (len("".join(pages[r][1].split())), r == survivor))
        substantive = len("".join(pages[survivor][1].split())) > SUBSTANTIVE
        review = body_from != survivor and substantive and \
            len("".join(pages[body_from][1].split())) > SUBSTANTIVE
        out.append({"url": url, "survivor": survivor, "losers": losers, "excluded": excluded,
                    "body_from": body_from if body_from != survivor else None,
                    "needs_review": review, "links": {r: sorted(links[r]) for r in losers}})
    return out, sorted(placeholders)


def _raws(value) -> list[str]:
    return [value] if isinstance(value, str) else [str(v) for v in (value or [])]


def _ok(result: str) -> bool:
    return not result.startswith(("rejected", "refused", "error"))


def _relink(W, page: str, old: str, new: str) -> str:
    pat = re.compile(re.escape(old) + r"(?=\.md\b|[\]\|#\s\"'),]|$)", re.M)
    fm, body = W._read_page(WIKI / page)

    def swap(value):
        if isinstance(value, str):
            return pat.sub(new, value)
        if isinstance(value, list):
            return [swap(v) for v in value]
        if isinstance(value, dict):
            return {k: swap(v) for k, v in value.items()}
        return value

    patch = {k: swap(v) for k, v in fm.items() if swap(v) != v}
    new_body = pat.sub(new, body or "")
    return W._update(page, patch or None, new_body if new_body != (body or "") else None)


def claimed_ids(W) -> set[str]:
    """Every id stamped on a source page, tombstoned ones included (a retired page keeps its id)."""
    ids: set[str] = set()
    for p in (WIKI / "sources").rglob("*.md"):
        try:
            fm, _body = W._read_page(p)
        except (OSError, ValueError):
            continue
        if isinstance(fm, dict) and str(fm.get("id") or "").strip():
            ids.add(str(fm["id"]).strip())
    return ids


def _stamp_id(rel: str, pid: str, claimed: set[str]) -> str:
    """Give an id-less page identity, below the write path exactly as backfill_ids does (the write
    path cannot update a page that has none). `pid` if free, else `pid-2`, `pid-3`, …"""
    import backfill_ids
    path = WIKI / f"{rel}.md"
    text = path.read_text(encoding="utf-8")
    _fm, m = backfill_ids._parse_fm(text)
    base, n = pid, 2
    while pid in claimed:
        pid, n = f"{base}-{n}", n + 1
    path.write_text(backfill_ids._insert_id(text, pid, m), encoding="utf-8")
    claimed.add(pid)
    return pid


def _stamp_stem_id(loser: str, claimed: set[str]) -> str:
    """An id-less loser gets `sources:<stem>` (never the survivor's id) before it can be retired."""
    return _stamp_id(loser, f"sources:{Path(loser).stem}", claimed)


def apply_group(W, group: dict, pages: dict, claimed: set[str]) -> list[str]:
    """Execute one planned group; stop at the first refused write. Returns the step log."""
    log: list[str] = []
    for rel in group["excluded"]:
        note = (f"dedup-same-url: title disagrees with the other pages holding {group['url']} "
                "— this page's URL is probably wrong (okengine#886)")
        log.append(f"flag {rel}: {W._flag(rel + '.md', note).splitlines()[0]}")
    if not group["losers"]:
        return log
    survivor = group["survivor"]
    sfm, _sbody = pages[survivor]
    patch: dict = {}
    raws = _raws(sfm.get("raw"))
    for loser in group["losers"]:
        lfm = pages[loser][0]
        patch.update({k: v for k, v in lfm.items()
                      if k not in NEVER_COPY and k not in sfm and k not in patch})
        raws += [r for r in _raws(lfm.get("raw")) if r not in raws]
    if raws != _raws(sfm.get("raw")):
        patch["raw"] = raws
    body = pages[group["body_from"]][1] if group["body_from"] else None
    if patch or body is not None:
        result = W._update(survivor + ".md", patch or None, body)
        log.append(f"update {survivor}: {result.splitlines()[0]}")
        if not _ok(result):
            return log + ["STOP: survivor update refused — no loser retired"]
    if group["needs_review"]:
        note = (f"dedup-same-url: body replaced by the longer copy from {group['body_from']}; "
                "both copies were substantive — check nothing was lost (okengine#886)")
        log.append(f"flag {survivor}: {W._flag(survivor + '.md', note).splitlines()[0]}")
    for loser in group["losers"]:
        for page in group["links"].get(loser, []):
            result = _relink(W, page, loser, survivor)
            log.append(f"relink {page}: {result.splitlines()[0]}")
            if not _ok(result):
                return log + [f"STOP: relink refused — {loser} stays live"]
        if not str(pages[loser][0].get("id") or "").strip():
            log.append(f"stamp {loser}: {_stamp_stem_id(loser, claimed)}")
        result = W._tombstone(loser + ".md", f"same url as {survivor} ({group['url']}); merged "
                              "by dedup-same-url (okengine#886)", superseded_by=survivor)
        log.append(f"tombstone {loser}: {result.splitlines()[0]}")
        if not _ok(result):
            return log + [f"STOP: tombstone refused — {loser} stays live"]
    return log


def recover_url(W, fm: dict) -> tuple[str, str]:
    """(url, "") when the page's own raw captures agree on ONE real URL, else ("", reason)."""
    found: set[str] = set()
    for ref in _raws(fm.get("raw")):
        path = VAULT / ref
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".json":
            candidates = [W._json_raw_url(text)]
        else:
            m = _FM.match(text)
            try:
                raw_fm = yaml.safe_load(m.group(1)) if m else None
            except YAMLError:
                raw_fm = None
            raw_fm = raw_fm if isinstance(raw_fm, dict) else {}
            candidates = [raw_fm.get("canonical_url"), raw_fm.get("url")]
        found |= {W._norm_url(c) for c in candidates if c and not is_placeholder(c)} - {""}
    if len(found) == 1:
        return found.pop(), ""
    return "", "no raw capture carries a real URL" if not found else \
        f"raw captures disagree ({len(found)} URLs)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--placeholders", action="store_true",
                    help="repair placeholder urls from their own raw captures instead of merging")
    args = ap.parse_args(argv)
    if not (WIKI / "sources").is_dir():
        print('{"wakeAgent": false}')
        return 0
    W = _write_server()
    pages = live_sources(W)
    groups, placeholders = plan(W, pages)
    mode = "APPLY" if args.apply else "DRY RUN"
    summary: dict[str, int] = defaultdict(int)
    claimed = claimed_ids(W)
    if args.placeholders:
        print(f"# dedup-same-url --placeholders [{mode}] — "
              f"{len(placeholders)} placeholder url(s)\n")
        for rel in placeholders:
            url, why = recover_url(W, pages[rel][0])
            if not url:
                summary["unrepairable"] += 1
                print(f"- {rel}: cannot repair — {why}")
                continue
            summary["repairable"] += 1
            result = "planned"
            if args.apply:
                if not str(pages[rel][0].get("id") or "").strip():
                    # the real URL is now known, so the page gets its canonical identity
                    print(f"  stamp {rel}: {_stamp_id(rel, W.source_url_id(url), claimed)}")
                result = W._update(rel + ".md", {"url": url}).splitlines()[0]
            print(f"- {rel}: → {url}  [{result}]")
            summary["repaired"] += int(args.apply and _ok(result))
    else:
        print(f"# dedup-same-url [{mode}] — {len(groups)} shared url(s), "
              f"{len(placeholders)} placeholder url(s) left for --placeholders\n")
        for group in groups:
            summary["losers"] += len(group["losers"])
            summary["excluded"] += len(group["excluded"])
            summary["needs_review"] += int(group["needs_review"])
            print(f"## {group['url']}\n  " + (f"keep   {group['survivor']}" if group["losers"]
                                                 else "no merge: no two titles agree"))
            for rel in group["losers"]:
                print(f"  retire {rel}  (links: {len(group['links'].get(rel, []))})")
            for rel in group["excluded"]:
                print(f"  EXCLUDED (title disagrees; flag needs_review) {rel}")
            if group["body_from"]:
                print(f"  body from {group['body_from']}"
                      + ("  → needs_review" if group["needs_review"] else ""))
            if args.apply:
                for line in apply_group(W, group, pages, claimed):
                    print(f"    {line}")
                    summary["stopped"] += int(line.startswith("STOP"))
    print(json.dumps({"wakeAgent": False, "groups": len(groups),
                      "placeholders": len(placeholders), **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
