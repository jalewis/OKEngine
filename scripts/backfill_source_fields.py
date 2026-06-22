#!/usr/bin/env python3
"""One-time field-tail backfill for `source` pages (#3 / OKF conformance).

After the typeless sweep, the residual non-conformance is sources missing
`published` and/or `source_kind`. This fills them WITHOUT fabrication, in three
honest phases (run any subset; default is all, dry-run):

  1. rename  — the value already exists under a legacy key. Rename, don't invent:
                 date, published_date  -> published   (publish date under old key)
                 source_type, kind     -> source_kind
               `created` is deliberately NOT used for `published` (it's the
               ingest date, not the publication date).
  2. filename — `published` still missing AND the filename starts YYYY-MM-DD:
               use that. The whole corpus is named by publication date (the
               ingest pipeline's convention), so this is recovery, not a guess.
  3. classify — `source_kind` still missing: assign the publisher's DOMINANT
               source_kind, LEARNED from the ~28k sources that already have both
               (only when the publisher has >=MIN_SAMPLES sources and one kind is
               >=DOMINANCE of them). Publishers without a confident signal are
               left for page-quality-enrich / a human. Data-driven, not invented.

Edits only the frontmatter key line(s); body stays byte-identical. Idempotent.
Only touches type:source pages. Usage:
  python backfill_source_fields.py [--apply] [--phases rename,filename,classify]
                                   [--root /opt/vault]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?\n)(---[ \t]*(?:\n|\Z))", re.S)
_DATE_FN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")
_RENAME = {"date": "published", "published_date": "published",
           "source_type": "source_kind", "kind": "source_kind"}

MIN_SAMPLES = 5      # publisher must have >= this many kinded sources
DOMINANCE = 0.70     # and one kind must be >= this fraction


def _fm_of(text: str):
    m = _FM_RE.match(text)
    if not m:
        return None, None
    try:
        fm = yaml.safe_load(m.group(2))
    except Exception:
        return None, m
    return (fm if isinstance(fm, dict) else None), m


def _has(fm: dict, k: str) -> bool:
    v = fm.get(k)
    return v not in (None, "")


def _set_line_after_open(m, key: str, value: str, text: str) -> str:
    """Insert `key: value` as the first frontmatter line (after opening ---)."""
    return m.group(1) + f"{key}: {value}\n" + m.group(2) + m.group(3) + text[m.end():]


def _rename_key(m, old: str, new: str, text: str) -> str | None:
    """Rename a top-level FM key, preserving its value and the rest byte-for-byte."""
    body = m.group(2)
    pat = re.compile(rf"^{re.escape(old)}([ \t]*:.*)$", re.M)
    if not pat.search(body):
        return None
    new_body = pat.sub(lambda mm: f"{new}{mm.group(1)}", body, count=1)
    return m.group(1) + new_body + m.group(3) + text[m.end():]


def _learn_publisher_kind(sources_dir: Path) -> dict[str, str]:
    by_pub: dict[str, Counter] = defaultdict(Counter)
    for p in sources_dir.rglob("*.md"):
        try:
            fm, _ = _fm_of(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not fm or fm.get("type") != "source":
            continue
        pub = str(fm.get("publisher") or "").strip().lower()
        kind = fm.get("source_kind")
        if pub and kind:
            by_pub[pub][str(kind)] += 1
    learned = {}
    for pub, c in by_pub.items():
        total = sum(c.values())
        kind, n = c.most_common(1)[0]
        if total >= MIN_SAMPLES and n / total >= DOMINANCE:
            learned[pub] = kind
    return learned


def process(root: Path, apply: bool, phases: set[str]) -> None:
    sources = root / "wiki" / "sources"
    learned = _learn_publisher_kind(sources) if "classify" in phases else {}
    if "classify" in phases:
        print(f"learned confident publisher->source_kind for {len(learned)} publishers\n")
    counts = Counter()
    for p in sorted(sources.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if "/raw/" in rel:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm, m = _fm_of(text)
        if not fm or m is None or fm.get("type") != "source":
            continue
        changed = False

        if "rename" in phases:
            for old, new in _RENAME.items():
                if _has(fm, old) and not _has(fm, new):
                    nt = _rename_key(m, old, new, text)
                    if nt:
                        text = nt
                        fm, m = _fm_of(text)
                        counts[f"rename {old}->{new}"] += 1
                        changed = True

        if "filename" in phases and not _has(fm, "published"):
            d = _DATE_FN.match(p.name)
            if d:
                text = _set_line_after_open(m, "published", "-".join(d.groups()), text)
                fm, m = _fm_of(text)
                counts["filename->published"] += 1
                changed = True

        if "classify" in phases and not _has(fm, "source_kind"):
            pub = str(fm.get("publisher") or "").strip().lower()
            kind = learned.get(pub)
            if kind:
                text = _set_line_after_open(m, "source_kind", kind, text)
                fm, m = _fm_of(text)
                counts[f"classify->{kind}"] += 1
                changed = True

        if changed and apply:
            try:
                p.write_text(text, encoding="utf-8")
            except OSError as e:
                print(f"  ! cannot write {rel}: {e}", file=sys.stderr)

    print(f"{'APPLIED' if apply else 'DRY-RUN'} — phases={sorted(phases)}")
    for k, v in counts.most_common():
        print(f"  {v:5d}  {k}")
    print(f"  total edits: {sum(counts.values())}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--phases", default="rename,filename,classify")
    ap.add_argument("--root", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    args = ap.parse_args(argv)
    process(Path(args.root), args.apply, set(args.phases.split(",")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
