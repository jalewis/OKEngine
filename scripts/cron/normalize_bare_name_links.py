#!/usr/bin/env python3
"""normalize_bare_name_links.py — rewrite bare-name wikilinks to canonical entity paths.

A common drift: a page writes `[[Qilin]]` / `[[Velvet Ant]]` / `[[CVE-2026-11317]]` — a bare
display name rather than the canonical `[[entities/q/qilin]]`. The link doesn't resolve (wrong
case / spacing / no path), so it shows up as a broken-wikilink even though the entity exists.

This drain repairs the unambiguous, deterministic slice (no LLM): for each broken **bare-name**
target (no `/`), if it matches EXACTLY ONE entity by name / alias / stem (case- and
punctuation-insensitive), rewrite the link to that entity's canonical path, preserving any
`#anchor` / `|display`. The reader renders `[[entities/q/qilin]]` with the entity's title, so the
bare display name isn't lost. Ambiguous (>1 match), already-resolving, path-form, and junk
(numeric / single-char) targets are left for the agent broken-wikilinks-drain / human triage.

Conservative + idempotent + write-guard-safe:
  - rewrites ONLY in the body (frontmatter refs are plain paths already);
  - touches ONLY genuinely-broken bare-name links with a single exact entity match;
  - additive in spirit (changes a link's target, never drops content).

Script-only (wakeAgent=false). Set NORMALIZE_LINKS_DRY_RUN=1 to report without writing.

Env:
  WIKI_PATH                vault root (default /opt/vault)
  NORMALIZE_LINKS_DRY_RUN  if set, count + report but don't write
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
ENT_DIR = WIKI / "entities"
OUT_DIR = WIKI / "operational"
DRY_RUN = bool(os.environ.get("NORMALIZE_LINKS_DRY_RUN"))

_FM = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.S)
# target (no pipe/hash/slash captured separately), optional #anchor, optional |display
_WL = re.compile(r"\[\[\s*([^\[\]|#\n]+?)\s*(#[^\[\]|\n]+)?\s*(\|[^\[\]\n]+)?\]\]")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _split_body(text: str) -> tuple[str, str]:
    """(frontmatter-block-incl-fences, body). Empty fm-block when none."""
    m = _FM.match(text)
    if not m:
        return "", text
    return text[: m.end()], text[m.end():]


def build_index() -> tuple[dict, set]:
    """norm(name|alias|stem) -> {canonical entity path}; and the set of all valid link slugs
    (page rel-paths + bare stems) used to tell a resolving link from a broken one."""
    name_index: dict[str, set] = {}
    valid_slugs: set[str] = set()
    for p in WIKI.rglob("*.md"):
        if not p.is_file():
            continue
        rel = p.relative_to(WIKI).with_suffix("")
        valid_slugs.add(str(rel))
        valid_slugs.add(p.stem)
    for p in ENT_DIR.rglob("*.md"):
        if not p.is_file():
            continue
        try:
            m = _FM.match(p.read_text(encoding="utf-8", errors="ignore"))
            fm = yaml.safe_load(m.group(1)) if m else None
        except Exception:
            fm = None
        fm = fm if isinstance(fm, dict) else {}
        rel = str(p.relative_to(WIKI).with_suffix(""))
        names = [fm.get("name")] + (fm.get("aliases") or []) + [p.stem]
        for nm in names:
            if nm:
                name_index.setdefault(_norm(nm), set()).add(rel)
    return name_index, valid_slugs


def rewrite_text(body: str, name_index: dict, valid_slugs: set) -> tuple[str, list]:
    """Return (new_body, [(old_target, canonical), ...])."""
    fixes: list = []

    def repl(m: re.Match) -> str:
        tgt = m.group(1).strip()
        if "/" in tgt:
            return m.group(0)                         # path-form, not a bare name
        key = _norm(tgt)
        if len(key) < 2 or key.isdigit():
            return m.group(0)                         # junk: numeric / single-char
        if tgt in valid_slugs:
            return m.group(0)                         # already resolves exactly
        matches = name_index.get(key)
        if not matches or len(matches) != 1:
            return m.group(0)                         # missing or ambiguous → leave for triage
        canon = next(iter(matches))
        fixes.append((tgt, canon))
        return f"[[{canon}{m.group(2) or ''}{m.group(3) or ''}]]"

    return _WL.sub(repl, body), fixes


def main() -> int:
    if not ENT_DIR.is_dir():
        print('{"wakeAgent": false}')
        return 0
    name_index, valid_slugs = build_index()

    pages_changed = 0
    links_fixed = 0
    samples: list = []
    for p in WIKI.rglob("*.md"):
        if not p.is_file() or any(part.startswith((".", "_")) or ".bak." in part for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        head, body = _split_body(text)
        new_body, fixes = rewrite_text(body, name_index, valid_slugs)
        if not fixes:
            continue
        pages_changed += 1
        links_fixed += len(fixes)
        if len(samples) < 12:
            samples.extend((f"{p.relative_to(WIKI)}", o, c) for o, c in fixes[:2])
        if not DRY_RUN:
            p.write_text(head + new_body, encoding="utf-8")

    verb = "would fix" if DRY_RUN else "fixed"
    print(f"normalize-bare-name-links: {verb} {links_fixed} bare-name link(s) "
          f"across {pages_changed} page(s)")
    for rel, old, canon in samples[:12]:
        print(f"  {rel}: [[{old}]] -> [[{canon}]]")
    if not DRY_RUN and (links_fixed or OUT_DIR.is_dir()):
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / "bare-name-link-normalize.md").write_text(
                f"---\ntype: report\ntitle: Bare-name link normalization\n---\n\n"
                f"Last run fixed **{links_fixed}** bare-name link(s) across {pages_changed} page(s).\n",
                encoding="utf-8")
        except OSError:
            pass
    print('{"wakeAgent": false}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
