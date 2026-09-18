#!/usr/bin/env python3
"""Reattach the `raw:` backlink on source pages promoted from a raw capture without one.

The raw->sources promotion is an AGENT lane: `select_raw_batch.py` offers a digest of raw files and
the agent writes source pages. When the agent omits `raw:`, the capture and the page it produced
stop being connected, and two things follow — the provenance chain a reader follows back to the
original capture is severed, and the SELECTOR can no longer tell a processed raw file from an
unprocessed one, so it re-offers work already done. Sampling found 4 of 8 apparently-unpromoted raw
files already had a page; the backlog was overstated and the duplicate work was real.

MATCHING IS DELIBERATELY CONSERVATIVE, because a wrong backlink is worse than a missing one: it
asserts a provenance that never happened, and unlike an absence nothing downstream would question
it. A pair is joined only when a normalised title or a normalised URL matches EXACTLY ONE raw file
and EXACTLY ONE page. Ambiguity on either side is reported and skipped, never resolved by
preference.

`reference-data` pages are excluded by construction: they are API-imported reference rows that
have no raw capture at all, so a missing `raw:` there is correct rather than a gap. They dominate
the counts and would otherwise swamp the signal.

Pure no_agent script. Idempotent. Dry-run by DEFAULT.

Env: WIKI_PATH (default /opt/vault)
Usage: repair_raw_backlinks.py [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?", re.S)
_NORM = re.compile(r"[^a-z0-9]+")
# API-imported reference data has no raw capture; a missing backlink there is correct, not a gap.
EXCLUDED_KINDS = {"reference-data"}
MIN_TITLE_LEN = 12          # short titles collide across unrelated captures


def norm(value) -> str:
    return _NORM.sub(" ", str(value or "").casefold()).strip()


def norm_url(value) -> str:
    u = str(value or "").strip().rstrip("/").casefold()
    return re.sub(r"^https?://(www\.)?", "", u)


def read_page(path: Path):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    m = _FM.match(text)
    if not m:
        return None, ""
    try:
        fm = yaml.safe_load(m.group(1))
    except Exception:
        return None, ""
    return (fm if isinstance(fm, dict) else None), text[m.end():]


def index_raw(vault: Path):
    """(title -> [rel], url -> [rel]) over every raw capture."""
    by_title: dict[str, list[str]] = collections.defaultdict(list)
    by_url: dict[str, list[str]] = collections.defaultdict(list)
    base = vault / "raw"
    for path in base.rglob("*.md") if base.is_dir() else []:
        rel = path.relative_to(vault).as_posix()
        fm, _body = read_page(path)
        fm = fm or {}
        title = norm(fm.get("title") or path.stem)
        if len(title) >= MIN_TITLE_LEN:
            by_title[title].append(rel)
        url = norm_url(fm.get("url"))
        if url:
            by_url[url].append(rel)
    return by_title, by_url


def survey(vault: Path, by_title, by_url):
    """(joins, stats) — joins are (page path, raw rel, how)."""
    stats = collections.Counter()
    # a raw file already cited by some page must not be re-attached to a second one
    claimed = set()
    pages = []
    base = vault / "wiki" / "sources"
    for path in sorted(base.rglob("*.md")) if base.is_dir() else []:
        fm, _body = read_page(path)
        if not fm:
            continue
        if fm.get("raw"):
            claimed.add(str(fm["raw"]).removeprefix("wiki/"))
            stats["already backlinked"] += 1
            continue
        if str(fm.get("source_kind") or "") in EXCLUDED_KINDS:
            stats["reference-data (no raw capture by design)"] += 1
            continue
        pages.append((path, fm))

    joins = []
    title_claims: dict[str, list] = collections.defaultdict(list)
    for path, fm in pages:                       # detect page-side ambiguity before joining
        title_claims[norm(fm.get("title") or path.stem)].append(path)

    for path, fm in pages:
        url = norm_url(fm.get("url"))
        title = norm(fm.get("title") or path.stem)
        cands, how = [], ""
        if url and len(by_url.get(url, [])) == 1:
            cands, how = by_url[url], "url"
        elif len(title) >= MIN_TITLE_LEN and len(by_title.get(title, [])) == 1:
            if len(title_claims[title]) > 1:
                stats["ambiguous — several pages share the title"] += 1
                continue
            cands, how = by_title[title], "title"
        if not cands:
            stats["no raw capture matches" if not (by_url.get(url) or by_title.get(title))
                  else "ambiguous — several raw files match"] += 1
            continue
        rel = cands[0]
        if rel in claimed:
            stats["raw file already cited by another page"] += 1
            continue
        claimed.add(rel)
        joins.append((path, rel, how))
        stats[f"joined by {how}"] += 1
    return joins, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    by_title, by_url = index_raw(VAULT)
    joins, stats = survey(VAULT, by_title, by_url)
    if args.limit:
        joins = joins[:args.limit]

    written = 0
    for path, rel, _how in joins:
        if not args.apply:
            continue
        fm, body = read_page(path)
        if not fm or fm.get("raw"):
            continue
        fm["raw"] = rel
        fm["raw_backlink_repaired"] = (
            "reattached by repair_raw_backlinks: this page was promoted from a raw capture that it "
            "did not cite, which severed provenance and left the selector unable to tell processed "
            "captures from unprocessed ones")
        path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
                        + "---\n\n" + body.lstrip(), encoding="utf-8")
        written += 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"repair-raw-backlinks: {mode} — {len(joins)} backlink(s) reattachable, {written} written")
    for key, value in stats.most_common():
        print(f"    {key}: {value}")
    for path, rel, how in joins[:5]:
        print(f"    e.g. {path.relative_to(WIKI).as_posix()[:52]} -> {rel[:46]} (by {how})")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
