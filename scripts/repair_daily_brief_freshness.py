#!/usr/bin/env python3
"""Repair pages admitted forever by the pre-#507 daily-brief string comparison.

The old selector considered ``str(value)[:10] >= since`` fresh.  This utility
targets only malformed date values that satisfy that old predicate; valid
current activity is never rewritten. Source publication dates are recovered
from a canonical YYYY/MM/DD path, then a raw filename, then a valid lifecycle
date. Invalid optional lifecycle fields are recovered from another valid
lifecycle value or the file mtime; an invalid ``updated`` field is removed.

Dry-run by default. Pass ``--apply`` to write changes.
"""
from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)
DATE_IN_TEXT = re.compile(r"(?<!\d)(20\d{2})[-_/](\d{2})[-_/](\d{2})(?!\d)")
COMPACT_DATE_IN_TEXT = re.compile(r"(?<!\d)(20\d{2})[-_/](\d{2})(\d{2})(?!\d)")
LIFECYCLE = ("created", "last_updated", "updated")


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _date_in(text: str) -> date | None:
    for pattern in (DATE_IN_TEXT, COMPACT_DATE_IN_TEXT):
        for match in pattern.finditer(text):
            try:
                return date(*(int(part) for part in match.groups()))
            except ValueError:
                continue
    return None


def source_date(path: Path, wiki: Path, fm: dict) -> date:
    rel = path.relative_to(wiki).as_posix()
    # A common corruption wraps an otherwise recoverable date in prose:
    # "queued for review at 2026-07-06+00:00".
    inferred = _date_in(str(fm.get("published") or ""))
    if inferred and inferred <= date.today():
        return inferred
    path_match = re.search(r"/(20\d{2})/(\d{2})/(\d{2})/", f"/{rel}")
    if path_match and path_match.group(3) != "00":
        path_date = date(*(int(part) for part in path_match.groups()))
        # A page created weeks before a future-dated shard is corrupt rather than a scheduled
        # publication. Prefer its valid lifecycle evidence in that case.
        if path_date <= date.today():
            return path_date
    raw = fm.get("raw")
    raw_text = " ".join(str(x) for x in raw) if isinstance(raw, list) else str(raw or "")
    inferred = _date_in(raw_text)
    if inferred:
        return inferred
    for field in LIFECYCLE:
        inferred = parse_date(fm.get(field))
        if inferred:
            return inferred
    if path_match and path_match.group(3) != "00":
        return date(*(int(part) for part in path_match.groups()))
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()


def _replace_scalar(text: str, field: str, value: str | None) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(field)}:[^\n]*\n")
    if value is None:
        return pattern.sub("", text, count=1)
    return pattern.sub(f"{field}: '{value}'\n", text, count=1)


def repair_page(path: Path, wiki: Path, since: str) -> tuple[str, list[str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    match = FM_RE.match(text)
    fm = yaml.safe_load(match.group(1)) if match else {}
    if not isinstance(fm, dict):
        return text, []
    changes: list[str] = []
    page_type = str(fm.get("type") or "").lower()

    published = fm.get("published")
    if page_type == "source" and parse_date(published) is None \
            and str(published or "")[:10] >= since:
        recovered = source_date(path, wiki, fm).isoformat()
        text = _replace_scalar(text, "published", recovered)
        changes.append(f"published={recovered}")

    fallback = next((parse_date(fm.get(key)) for key in LIFECYCLE
                     if parse_date(fm.get(key)) is not None), None)
    fallback = fallback or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
    for field in LIFECYCLE:
        value = fm.get(field)
        if field not in fm or parse_date(value) is not None \
                or str(value or "")[:10] < since:
            continue
        if field == "updated":
            text = _replace_scalar(text, field, None)
            changes.append("removed invalid updated")
        else:
            recovered = fallback.isoformat()
            text = _replace_scalar(text, field, recovered)
            changes.append(f"{field}={recovered}")
    return text, changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vault", type=Path)
    parser.add_argument("--since", default="2026-07-28")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    wiki = args.vault / "wiki"
    changed = 0
    for namespace in ("sources", "entities", "concepts"):
        for path in sorted((wiki / namespace).rglob("*.md")):
            repaired, changes = repair_page(path, wiki, args.since)
            if not changes:
                continue
            changed += 1
            print(f"{path.relative_to(wiki)}: {', '.join(changes)}")
            if args.apply:
                path.write_text(repaired, encoding="utf-8")
    print(f"{'repaired' if args.apply else 'would repair'} {changed} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
