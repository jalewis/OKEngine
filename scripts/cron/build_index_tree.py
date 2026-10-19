#!/usr/bin/env python3
"""build-index-tree — generate the OKF hierarchical INDEX.md tree (Phase 4).

After the Phase 1-3 migration the knowledge namespaces are nested (the pack's
schema.yaml `partitioning` declares how — e.g. by type, by date, or by initial
letter). This
generator makes that hierarchy NAVIGABLE per the OKF/Karpathy pattern: an
INDEX.md at every directory level that an agent traverses top-down — read the top
INDEX (namespaces), drill to the namespace INDEX (buckets), then the leaf INDEX
(the pages). Each INDEX lists only its immediate children, so no single file is
unbounded — closing the "navigation blindness" failure mode at scale.

Also writes vault-level structural files: BUNDLE.md (manifest/counts), HEALTH.md
(health summary), and AGENTS.md (pointer to the behavioral contract).

The whole tree is deterministic from the corpus, so it is gitignored (the
generator in the repo is the source of truth) — like the other regen dashboards.

Pure script / no_agent. Env: WIKI_PATH (default /opt/vault).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generated_page  # noqa: E402 — the shared structural-page predicate (okengine#994 [23])
import schema_lib  # noqa: E402
from okengine.file_fingerprint import fingerprint  # noqa: E402 — importable once schema_lib has set up the package
from active_view_lib import (  # noqa: E402
    FRONTMATTER_RE, frontmatter_block, is_retired_actor, read_frontmatter)
import tz_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
MAX_ENTRIES = 500
_FM_RE = FRONTMATTER_RE


def _listable(name: str) -> bool:
    """A page worth listing in an INDEX or counting as content — not structural/scaffolding.

    Delegates to generated_page.is_exempt, the one predicate backfill_ids and the generated-page
    writers already share: the reserved files (INDEX/log/BUNDLE/HEALTH/AGENTS/README/HOT), this
    generator's own INDEX-pNN pagination, any `_`- or `.`-prefixed file (the _about namespace card,
    _review-queue) and backups. okengine#994 [23]: a private list here missed INDEX-pNN, so a
    paginated directory listed its own page 2..N as rows, and the counts below took in every
    INDEX/INDEX-pNN/_about file (vendor-risk inflated about 24%)."""
    return not generated_page.is_exempt(Path(name))

_SCHEMA = schema_lib.governing_schema(VAULT)
# Non-knowledge top-level dirs to skip. The pack declares its derived/operational
# dirs via schema.yaml `exclude:`; the engine adds only generic, never-knowledge
# artifacts (raw corpus mirrors / archive). Everything else under wiki/ that holds
# markdown is a navigable namespace — so additional domains (e.g. a sub-domain dir
# with its own schema.yaml) are picked up automatically.
_SKIP_NS = schema_lib.excluded_dirs(_SCHEMA) | {"raw", "raw-cache", "_archive"}


def _discover_namespaces(wiki: Path) -> list[str]:
    out = []
    for d in sorted(wiki.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")) or d.name in _SKIP_NS:
            continue
        if any(True for _ in d.rglob("*.md")):
            out.append(d.name)
    return out


def _fm(p: Path) -> dict:
    """The page's whole frontmatter (active_view_lib.read_frontmatter: no leading-window cap)."""
    return read_frontmatter(p)


def _title(p: Path, fm: dict) -> str:
    t = fm.get("title") or fm.get("name")
    return str(t) if t else p.stem


def _date(fm: dict) -> str:
    """A human-meaningful date for the index row (YYYY-MM-DD). Prefers the curation date, then the
    write-path auto-stamp, then the source date — so pages whose slug carries no date (e.g. lacuna,
    entities, concepts) still show WHEN, keyed off the fields the write path always stamps."""
    for k in ("updated", "last_updated", "published", "created"):
        v = fm.get(k)
        if v:
            return str(v)[:10]
    return ""


def _created(fm: dict) -> str:
    """The row's birth date (YYYY-MM-DD): the write-path `created` stamp, else the source's
    published date. Separate from _date() so an INDEX shows both when-it-appeared and
    when-it-last-moved — new pages are what operators scan an INDEX for."""
    for k in ("created", "published"):
        v = fm.get(k)
        if v:
            return str(v)[:10]
    return ""


# Retirement verdicts for this run, keyed by (path, file fingerprint). _count_md runs at every
# ancestor level, so without this each page's frontmatter was read and YAML-parsed (depth + 2)
# times: about 5x the lane's runtime on a 100k-page vault, past a 600 s HERMES_CRON_SCRIPT_TIMEOUT
# (v0.15.3 release audit). The key was (path, mtime_ns) until okengine#1017: a page retired within
# the same timestamp tick as its first count kept its stale "live" verdict.
_RETIRED: dict[tuple[Path, tuple], bool] = {}


def _retired(p: Path, fm: dict | None = None) -> bool:
    fp = fingerprint(p)
    if fp is None:
        return False
    key = (p, fp)
    hit = _RETIRED.get(key)
    if hit is None:
        if fm is None:
            # okf_migrate.is_tombstoned matches only `status: tombstoned` (any case), so a page
            # whose frontmatter envelope (the same whole envelope _fm parses) never spells it is
            # live without a YAML parse.
            hit = ("tombstoned" in frontmatter_block(p).lower()
                   and is_retired_actor(_fm(p), VAULT, p))
        else:
            hit = is_retired_actor(fm, VAULT, p)
        _RETIRED[key] = hit
    return hit


def _count_md(d: Path) -> int:
    """Content pages under d: the same population gen_index lists, so a namespace's count, the
    BUNDLE entity_count and the HEALTH total agree with the rows a reader can open."""
    return sum(1 for p in d.rglob("*.md") if _listable(p.name) and not _retired(p))


def _owned_pagination(p: Path, fm: dict, rel: str) -> bool:
    """Recognize this generator's numbered artifacts, never arbitrary corpus pages."""
    match = re.fullmatch(r"INDEX-p(0[2-9]|[1-9]\d+)\.md", p.name)
    if not match:
        return False
    number = int(match[1])
    if fm.get("type") != "dashboard" or fm.get("title") != f"Index: {rel} (p{number})":
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:             # gone mid-scan: nothing left to own or remove
        return False
    envelope = _FM_RE.match(text)
    if not envelope:
        return False
    lines = text[envelope.end():].lstrip("\n").splitlines()
    if len(lines) < 8:
        return False
    heading = re.fullmatch(rf"# Index: {re.escape(rel)} — page {number}/([1-9][0-9]*)", lines[0])
    if not heading:
        return False
    total = int(heading[1])
    if total < number or len(lines[2].split()) != total + 1:
        return False
    prefix = "" if rel == "." else rel + "/"
    nav = "Pages: " + " ".join("**1**" if i == 1 else f"[[{prefix}INDEX-p{i:02d}|{i}]]"
                                 for i in range(1, total + 1))
    return lines[1:8] == ["", nav, "", f"## Pages ({number})", "",
                         "| Page | Type | Created | Updated | Title |", "|---|---|---|---|---|"]


def gen_index(d: Path, now: str) -> None:
    """Write INDEX.md for directory d (immediate children), then recurse."""
    subdirs = sorted(x for x in d.iterdir() if x.is_dir() and not x.name.startswith("."))
    files = sorted(x for x in d.glob("*.md") if _listable(x.name))  # glob-ok: per-directory INDEX; gen_index recurses into subdirs
    numbered = sorted(d.glob("INDEX-p*.md"))  # glob-ok: this directory's own pagination candidates
    # One fm read per file, then NEWEST-CREATED FIRST (empty created sorts last; ties stay
    # alphabetical via the stable sort above) — an INDEX's first job is surfacing new pages.
    rel = d.relative_to(WIKI).as_posix() or "."
    entries = [(f, _fm(f)) for f in files]
    retired = {f for f, fm in entries if _retired(f, fm)}
    # Retired actors stay on disk, so this remains scoped on repeat regeneration.
    # Leave pre-existing nonactor pagination behavior outside this direct repair.
    owned = {f for f in numbered if _owned_pagination(f, _fm(f), rel)} if retired else set()
    entries = [(f, fm) for f, fm in entries if f not in retired]
    files = [f for f, _ in entries]
    needed = {d / f"INDEX-p{i:02d}.md" for i in range(2, (len(entries) - 1) // MAX_ENTRIES + 2)}
    for p in needed:
        if retired and p.exists() and p not in owned:
            raise FileExistsError(f"Refusing to overwrite non-generated pagination file: {p}")
    entries.sort(key=lambda e: _created(e[1]) or "0000-00-00", reverse=True)
    rel = d.relative_to(WIKI).as_posix() or "."
    prefix = "" if rel == "." else rel + "/"
    lines = ["---", "type: dashboard", f'title: "Index: {rel}"', "---", "",
             f"# Index: {rel}", "", f"_generated {now} · {len(subdirs)} subdir(s), "
             f"{len(files)} page(s) here_", ""]
    _about_f = d / "_about.md"
    if _about_f.is_file():
        _at = _about_f.read_text(encoding="utf-8", errors="replace")
        _m = _FM_RE.match(_at)
        _body = (_at[_m.end():] if _m else _at).strip()
        if _body:
            lines += [_body, "", "---", ""]   # fold the namespace description card in
    if subdirs:
        lines += ["## Subdirectories", "", "| Dir | Pages | Index |", "|---|---:|---|"]
        for sd in subdirs:
            lines.append(f"| `{sd.name}/` | {_count_md(sd)} | [[{prefix}{sd.name}/INDEX|open]] |")
        lines.append("")
    def _rows(chunk):
        out = ["| Page | Type | Created | Updated | Title |", "|---|---|---|---|---|"]
        for f, fm in chunk:
            t = _title(f, fm).replace("|", "\\|")[:90]
            out.append(f"| [[{prefix}{f.stem}|{f.stem}]] | {str(fm.get('type') or '')} | "
                       f"{_created(fm)} | {_date(fm)} | {t} |")
        return out

    if files and len(files) <= MAX_ENTRIES:
        lines += ["## Pages", ""] + _rows(entries) + [""]
        (d / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    elif files:
        # Paginate: INDEX.md holds page 1 (the newest) + a page nav; INDEX-pNN.md hold the rest.
        pages = [entries[i:i + MAX_ENTRIES] for i in range(0, len(entries), MAX_ENTRIES)]
        nav = "Pages: " + " ".join(
            ("**1**" if i == 0 else f"[[{prefix}INDEX-p{i+1:02d}|{i+1}]]") for i in range(len(pages)))
        lines += [f"> {len(files)} pages — paginated at {MAX_ENTRIES}/page.", "", nav, "",
                  "## Pages (1)", ""] + _rows(pages[0]) + [""]
        (d / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
        for i, chunk in enumerate(pages[1:], start=2):
            pl = ["---", "type: dashboard", f'title: "Index: {rel} (p{i})"', "---", "",
                  f"# Index: {rel} — page {i}/{len(pages)}", "", nav, "",
                  f"## Pages ({i})", ""] + _rows(chunk) + [""]
            (d / f"INDEX-p{i:02d}.md").write_text("\n".join(pl), encoding="utf-8")
    else:
        (d / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
    for obsolete in owned - needed:
        obsolete.unlink()
    for sd in subdirs:
        gen_index(sd, now)


def main() -> int:
    if not WIKI.is_dir():
        print(f"ERROR: wiki not found at {WIKI}", file=sys.stderr)
        return 1
    now = tz_lib.deployment_now().strftime("%Y-%m-%d %H:%M %Z")  # okengine#301: deployment zone, not "UTC"
    namespaces = _discover_namespaces(WIKI)   # domain-agnostic: picks up sub-domains too
    counts = {}
    for ns in namespaces:
        d = WIKI / ns
        if d.is_dir():
            gen_index(d, now)
            counts[ns] = _count_md(d)

    # Top-level wiki/INDEX.md — namespaces only.
    top = ["---", "type: dashboard", 'title: "Wiki Index"', "---", "",
           f"# Wiki Index", "", f"_generated {now}_", "",
           "| Namespace | Pages | Index |", "|---|---:|---|"]
    for ns in namespaces:
        if ns in counts:
            top.append(f"| `{ns}/` | {counts[ns]:,} | [open]({ns}/INDEX.md) |")
    top.append("")
    (WIKI / "INDEX.md").write_text("\n".join(top), encoding="utf-8")

    total = sum(counts.values())
    # BUNDLE.md — vault manifest.
    bundle = ["---", "type: bundle", 'title: "OKF Bundle"',
              "okf_version: 0.1", f"generated: {now}", f"entity_count: {total}", "---", "",
              "# OKF Bundle Manifest", "", f"_generated {now}_", "",
              "| Namespace | Pages |", "|---|---:|"]
    for ns in namespaces:
        bundle.append(f"| {ns}/ | {counts.get(ns, 0):,} |")
    bundle += [f"| **total** | **{total:,}** |", "",
               "Structure: hierarchical OKF — namespaces are partitioned per the "
               "pack's `schema.yaml` (e.g. by type, date, or initial letter). "
               "Navigate via INDEX.md.", ""]
    (WIKI / "BUNDLE.md").write_text("\n".join(bundle), encoding="utf-8")

    # HEALTH.md — summary (detail in operational/schema-conformance.md).
    health = ["---", "type: dashboard", 'title: "Vault Health"', "---", "",
              f"# Vault Health — {now}", "",
              f"- Total curated pages: **{total:,}**",
              "- Conformance detail: [schema-conformance](operational/schema-conformance.md)",
              "- Structure: OKF hierarchical (see [BUNDLE](BUNDLE.md))", ""]
    (WIKI / "HEALTH.md").write_text("\n".join(health), encoding="utf-8")

    # AGENTS.md — OKF-reserved behavioral-contract pointer.
    agents = ["# AGENTS.md — Agent Behavioral Contract", "",
              "The authoritative agent contract for this vault is **`CLAUDE.md`** "
              "(vault root): ingest workflow, frontmatter/`schema.yaml`, prediction "
              "schema, lint rules. OKF reserves `AGENTS.md`; this file points to it.", "",
              "Structure: hierarchical OKF — traverse `INDEX.md` top-down "
              "(wiki/INDEX.md → namespace → bucket → page). Writers may create flat "
              "pages; the `reshelve-*` drains re-file them into the hierarchy.", ""]
    (WIKI / "AGENTS.md").write_text("\n".join(agents), encoding="utf-8")

    print(f"build-index-tree: {total:,} pages across {len(counts)} namespaces; "
          f"INDEX tree + BUNDLE/HEALTH/AGENTS written.")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
