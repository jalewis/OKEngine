#!/usr/bin/env python3
"""Rebuild wiki/index.md from current vault state.

Walks the pack's knowledge namespaces (schema.yaml `partitioning.namespaces`;
falls back to the on-disk top-level wiki/ dirs) and lists ALL pages with their
H1 (or fallback to slug). A `sources` namespace, if present, lists only the 30
most recent (by filename date prefix); an `entities` namespace is grouped by
type for navigation. Skips operational types so the index doesn't get polluted
with auto-generated artifacts.

Why this exists: the wiki-health-audit cron periodically reports
"N pages missing from index" because agents writing new pages don't reliably
maintain index.md. This script is a static rebuild — runs in seconds,
deterministic, no agent calls.

Page types and namespaces are PACK inputs (schema.yaml), read at runtime — the
engine ships no domain taxonomy.

Usage:
    WIKI_PATH=/path/to/vault python3 scripts/cron/rebuild_index.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402
import tz_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
INDEX = VAULT / "wiki" / "index.md"
RECENT_SOURCES = 30  # how many most-recent sources to list

OPERATIONAL_TYPES = {"dashboard", "lint", "overview", "report"}

_SCHEMA = schema_lib.governing_schema(VAULT)


def knowledge_namespaces() -> list[str]:
    """Pack-declared knowledge namespaces (schema.yaml). Fallback: the actual
    top-level dirs under wiki/ that hold markdown, minus excluded/dot/underscore
    dirs. Never a hardcoded domain list."""
    ns = schema_lib.knowledge_namespaces(_SCHEMA)
    if ns:
        return sorted(ns)
    excluded = schema_lib.excluded_dirs(_SCHEMA)
    wiki = VAULT / "wiki"
    out: list[str] = []
    if wiki.is_dir():
        for d in sorted(wiki.iterdir()):
            if not d.is_dir() or d.name.startswith((".", "_")) or d.name in excluded:
                continue
            if any(True for _ in d.rglob("*.md")):
                out.append(d.name)
    return out

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def parse_page(path: Path) -> tuple[dict | None, str | None]:
    """Return (frontmatter_dict, h1_title). Either may be None."""
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return None, None
    fm: dict | None = None
    body_start = 0
    m = FRONTMATTER_RE.match(txt)
    if m:
        try:
            fm = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            fm = None
        body_start = m.end()
    body = txt[body_start:]
    h1m = H1_RE.search(body)
    title = h1m.group(1).strip() if h1m else None
    return (fm if isinstance(fm, dict) else None), title


def is_operational(fm: dict | None) -> bool:
    if not fm:
        return False
    return str(fm.get("type", "")) in OPERATIONAL_TYPES


def short_title(slug: str, title: str | None) -> str:
    """Pick the best display title — strip wikilinks/markdown from H1."""
    if not title:
        return slug
    # Strip Obsidian wikilinks but keep display text
    cleaned = WIKILINK_RE.sub(lambda m: m.group(1).split("/")[-1], title)
    # Strip emphasis markers
    cleaned = re.sub(r"[*_`]", "", cleaned).strip()
    return cleaned or slug


def list_dir(subdir: str, limit: int | None = None, sort_recent: bool = False) -> list[str]:
    """Return [bullet-line, ...] for one wiki/ subdir."""
    d = VAULT / "wiki" / subdir
    if not d.is_dir():
        return []
    # rglob: a namespace may be sharded (sources/<year>/<month>/, entities/<type>/<letter>/).
    files = [p for p in d.rglob("*.md") if p.is_file() and not p.name.startswith("_")]
    if sort_recent:
        # Sort by filename date prefix (YYYY-MM-DD) descending — sources use this
        files.sort(key=lambda p: p.name, reverse=True)
    else:
        files.sort(key=lambda p: p.name)
    if limit:
        files = files[:limit]
    out: list[str] = []
    for p in files:
        fm, title = parse_page(p)
        if is_operational(fm):
            continue
        # Vault-relative path so the wikilink resolves under sharding; for a flat
        # namespace this is identical to "<subdir>/<slug>".
        rel = p.relative_to(VAULT / "wiki").with_suffix("").as_posix()
        slug = p.stem
        disp = short_title(slug, title)
        if disp == slug:
            out.append(f"- [[{rel}]]")
        else:
            out.append(f"- [[{rel}]] — {disp}")
    return out


def group_by_type(subdir: str) -> dict[str, list[str]]:
    """Group one namespace's pages by frontmatter `type` for navigation —
    used when a namespace is large + heterogeneous (e.g. entities)."""
    d = VAULT / "wiki" / subdir
    groups: dict[str, list[str]] = {}
    if not d.is_dir():
        return groups
    # rglob: a namespace may be sharded (entities/<type>/<letter>/, …).
    for p in sorted(d.rglob("*.md")):
        if not p.is_file() or p.name.startswith("_"):
            continue
        fm, title = parse_page(p)
        if is_operational(fm):
            continue
        t = str(fm.get("type", "untyped")) if fm else "untyped"
        rel = p.relative_to(VAULT / "wiki").with_suffix("").as_posix()
        slug = p.stem
        disp = short_title(slug, title)
        line = f"- [[{rel}]] — {disp}" if disp != slug else f"- [[{rel}]]"
        groups.setdefault(t, []).append(line)
    return groups


def render_index() -> str:
    namespaces = knowledge_namespaces()
    # Pack-declared canonical type ordering (schema.yaml), sorted; empty ⇒ types
    # are surfaced in alpha order with no preferred ordering.
    type_order = sorted(schema_lib.canonical_types(_SCHEMA))

    # Per-namespace rendering: a `sources` namespace lists the most-recent N
    # (filename-date prefix); an `entities` namespace is grouped by type; every
    # other namespace is a flat alphabetical list. These two are conventional
    # OKF namespace names, not domain facts — absent them, all namespaces render
    # flat, which is still correct.
    counts: dict[str, int] = {}
    sections: list[tuple[str, list[str]]] = []  # (heading-with-count, body-lines)
    for ns in namespaces:
        if ns == "sources":
            total = sum(1 for p in (VAULT / "wiki" / ns).rglob("*.md")
                        if p.is_file() and not p.name.startswith("_"))
            counts[ns] = total
            recent = list_dir(ns, limit=RECENT_SOURCES, sort_recent=True)
            sections.append((f"## Sources (most recent {RECENT_SOURCES})",
                             recent or ["_(none)_"]))
        elif ns == "entities":
            groups = group_by_type(ns)
            total = sum(len(v) for v in groups.values())
            counts[ns] = total
            body: list[str] = []
            for t in type_order:
                entries = groups.pop(t, [])
                if entries:
                    body += [f"### {t} ({len(entries)})", ""] + entries + [""]
            # Any remaining (drift / undeclared type) — surface for the next audit
            for t, entries in sorted(groups.items()):
                tag = " — DRIFT" if type_order else ""
                body += [f"### {t} ({len(entries)}){tag}", ""] + entries + [""]
            sections.append((f"## {ns.capitalize()} ({total})", body or ["_(none)_"]))
        else:
            entries = list_dir(ns)
            counts[ns] = len(entries)
            sections.append((f"## {ns.capitalize()} ({len(entries)})",
                             entries or ["_(none)_"]))

    out: list[str] = []
    out.append("# Wiki Index")
    out.append("")
    out.append("> Content catalog. Every page listed; a `sources` namespace shows the most recent.")
    now = tz_lib.deployment_now().strftime("%Y-%m-%d %H:%M %Z")  # okengine#301: deployment zone, not "UTC"
    summary = " | ".join(f"{ns.capitalize()}: {counts[ns]}" for ns in namespaces)
    out.append(f"> Generated: {now}" + (f" | {summary}" if summary else ""))
    out.append(f"> Rebuilt by `scripts/cron/rebuild_index.py` — re-run anytime; safe to overwrite.")
    out.append("")

    if not namespaces:
        out.append("_(no knowledge namespaces found)_")
        out.append("")
    for heading, body in sections:
        out.append(heading)
        out.append("")
        out.extend(body)
        out.append("")

    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild wiki/index.md from current vault state")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout without writing")
    args = parser.parse_args()

    if not (VAULT / "wiki").is_dir():
        print(f"ERROR: vault not found at {VAULT}", file=sys.stderr)
        return 1

    body = render_index()

    if args.dry_run:
        sys.stdout.write(body)
        return 0

    if INDEX.exists():
        # ONE overwritten sidecar, NOT .md-suffixed: dated .bak.<ts>.md copies
        # accumulated unboundedly AND were indexed/searched as pages (they end
        # in .md) — the vault grew three 500KB "index" pages in three days.
        bak = INDEX.parent / (INDEX.name + ".bak")
        shutil.copy2(INDEX, bak)
        print(f"snapshot → {bak.name}", file=sys.stderr)

    INDEX.write_text(body)
    print(f"wrote {INDEX} ({len(body)} bytes)", file=sys.stderr)
    # wake-gate signal for cron-plus — pure side-effect script, no agent needed
    print('{"wakeAgent": false}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
