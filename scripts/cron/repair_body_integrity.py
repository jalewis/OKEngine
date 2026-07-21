#!/usr/bin/env python3
"""Bounded, dry-run-first repair for malformed/duplicated Markdown H2 sections (#242).

Repairs canonical body text without reparsing or reserializing frontmatter:

* ``## ## Name`` becomes ``## Name``;
* duplicate H2 sections with the same normalized name collapse into the first;
* duplicate bullet entries collapse by cited source + date when available, otherwise exact text.

Reader-derived panels are deliberately NOT deleted automatically; corpus_audit reports them and
the write path prevents new ones. Removing an existing computed-looking section may discard prose,
so that remains a governed human repair.

Usage:
  python repair_body_integrity.py [--vault PATH] [--limit 25]          # dry-run
  python repair_body_integrity.py [--vault PATH] [--limit 25] --apply  # atomic writes
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

_FM_RE = re.compile(r"\A(---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z))(.*)\Z", re.DOTALL)
_H2_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_SOURCE_REF_RE = re.compile(r"\[\[(sources/[^|\]#]+)")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_SKIP_PARTS = {"dashboards", "operational", "_archived", ".okengine", ".backlinks"}


def _clean_heading(raw: str) -> str:
    value = raw.strip()
    while value.startswith("#"):
        value = re.sub(r"^#{1,6}(?:[ \t]+|$)", "", value).strip()
    return value


def _entry_key(lines: list[str]) -> tuple:
    text = "\n".join(lines).strip()
    sources = tuple(sorted(set(_SOURCE_REF_RE.findall(text))))
    date_match = _DATE_RE.search(text)
    if sources and date_match:
        return ("source-date", sources, date_match.group(1))
    return ("text", re.sub(r"\s+", " ", text).casefold())


def _dedupe_bullets(lines: list[str]) -> list[str]:
    """Dedupe bullet blocks while preserving prose and continuation lines."""
    out: list[str] = []
    seen: set[tuple] = set()
    index = 0
    while index < len(lines):
        marker = _FENCE_RE.match(lines[index])
        if marker:
            run = marker.group(1)
            out.append(lines[index])
            index += 1
            while index < len(lines):
                out.append(lines[index])
                close = _FENCE_RE.match(lines[index])
                index += 1
                if close and close.group(1)[0] == run[0] and len(close.group(1)) >= len(run):
                    break
            continue
        if not _BULLET_RE.match(lines[index]):
            out.append(lines[index])
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not _BULLET_RE.match(lines[end]):
            # A blank followed by non-indented prose starts content after the bullet list.
            if (
                not lines[end].strip()
                and end + 1 < len(lines)
                and lines[end + 1].strip()
                and not lines[end + 1].startswith((" ", "\t"))
            ):
                break
            end += 1
        block = lines[index:end]
        key = _entry_key(block)
        if key not in seen:
            seen.add(key)
            out.extend(block)
        index = end
    return out


def _bullet_count(lines: list[str]) -> int:
    count = 0
    fence: tuple[str, int] | None = None
    for line in lines:
        marker = _FENCE_RE.match(line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1]:
                fence = None
            continue
        if fence is None and _BULLET_RE.match(line):
            count += 1
    return count


def repair_body(body: str) -> tuple[str, dict]:
    """Return repaired body and deterministic change counts."""
    preamble: list[str] = []
    sections: list[dict] = []
    current: dict | None = None
    fence: tuple[str, int] | None = None
    malformed = 0
    for line in body.splitlines():
        marker = _FENCE_RE.match(line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1]:
                fence = None
        match = _H2_RE.match(line) if fence is None and marker is None else None
        if match:
            if match.group(1).lstrip().startswith("#"):
                malformed += 1
            current = {"heading": _clean_heading(match.group(1)), "lines": []}
            sections.append(current)
        elif current is None:
            preamble.append(line)
        else:
            current["lines"].append(line)

    merged: list[dict] = []
    by_name: dict[str, dict] = {}
    duplicates = 0
    for section in sections:
        key = section["heading"].casefold()
        if key not in by_name:
            by_name[key] = section
            merged.append(section)
            continue
        duplicates += 1
        target = by_name[key]
        if target["lines"] and target["lines"][-1].strip():
            target["lines"].append("")
        target["lines"].extend(section["lines"])

    before_bullets = sum(_bullet_count(section["lines"]) for section in merged)
    for section in merged:
        section["lines"] = _dedupe_bullets(section["lines"])
    after_bullets = sum(_bullet_count(section["lines"]) for section in merged)

    lines = list(preamble)
    for section in merged:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(f"## {section['heading']}")
        lines.extend(section["lines"])
    repaired = "\n".join(lines).rstrip() + "\n"
    return repaired, {
        "malformed_headings": malformed,
        "duplicate_sections": duplicates,
        "duplicate_entries": before_bullets - after_bullets,
    }


def repair_page(path: Path) -> tuple[str | None, dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, {}
    match = _FM_RE.match(text)
    if not match:
        return None, {}
    body, stats = repair_body(match.group(2))
    if not any(stats.values()):
        return None, stats
    repaired = match.group(1) + body
    return (repaired if repaired != text else None), stats


def candidates(vault: Path) -> list[Path]:
    wiki = vault / "wiki"
    if not wiki.is_dir():
        return []
    return [
        path for path in sorted(wiki.rglob("*.md"))
        if not _SKIP_PARTS.intersection(path.relative_to(wiki).parts)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    vault = Path(args.vault)
    wiki = vault / "wiki"
    changed = 0
    for path in candidates(vault):
        repaired, stats = repair_page(path)
        if repaired is None:
            continue
        rel = path.relative_to(wiki)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"{mode} {rel}: malformed={stats['malformed_headings']} "
            f"duplicate-sections={stats['duplicate_sections']} "
            f"duplicate-entries={stats['duplicate_entries']}"
        )
        if args.apply:
            tmp = path.with_suffix(path.suffix + ".body-integrity.tmp")
            tmp.write_text(repaired, encoding="utf-8")
            os.replace(tmp, path)
        changed += 1
        if changed >= args.limit:
            break

    print(f"body-integrity-repair | {'applied' if args.apply else 'would repair'} {changed} page(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
