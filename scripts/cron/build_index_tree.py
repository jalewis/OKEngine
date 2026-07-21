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

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402
import tz_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
MAX_ENTRIES = 500
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
_STRUCT = {"INDEX.md", "index.md", "log.md", "BUNDLE.md", "HEALTH.md", "AGENTS.md"}


def _listable(name: str) -> bool:
    """A page worth listing in an INDEX — not structural/scaffolding. Excludes _STRUCT
    (INDEX/log/BUNDLE/HEALTH/AGENTS) and any `_`-prefixed file (the _about namespace
    description card, _review-queue, etc.) — those exist but are not navigable pages."""
    return name not in _STRUCT and not name.startswith("_")

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
    try:
        m = _FM_RE.match(p.read_text(encoding="utf-8", errors="replace")[:4000])
    except OSError:
        return {}
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


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


def _count_md(d: Path) -> int:
    return sum(1 for _ in d.rglob("*.md"))


def gen_index(d: Path, now: str) -> None:
    """Write INDEX.md for directory d (immediate children), then recurse."""
    subdirs = sorted(x for x in d.iterdir() if x.is_dir() and not x.name.startswith("."))
    files = sorted(x for x in d.glob("*.md") if _listable(x.name))  # glob-ok: per-directory INDEX; gen_index recurses into subdirs
    # One fm read per file, then NEWEST-CREATED FIRST (empty created sorts last; ties stay
    # alphabetical via the stable sort above) — an INDEX's first job is surfacing new pages.
    entries = [(f, _fm(f)) for f in files]
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
