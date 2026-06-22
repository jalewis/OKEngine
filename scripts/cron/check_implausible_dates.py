#!/usr/bin/env python3
"""check_implausible_dates.py — flag implausible date-field years on pages.

The committed-content analogue of `year_derivation_audit` (which only diagnoses
the gitignored raw/ corpus). Catches the corruption classes that a plain
malformed-date check misses because the value *parses* fine — it's just wrong:

  - epoch / typo years (1970 mtime default, `1025-05-01`, `0205-...`)
  - a FUTURE date on a backward-looking field (you can't `published:` tomorrow's
    article, or `made_on:` a forecast next year)
  - an implausibly far-FUTURE date on a forward-looking field (`resolves_by:
    3025-01-01` is a typo; `resolves_by: 2029` is a legitimate long horizon)

Dates are directional, so bounds differ by field:
  - backward fields (published/ingested/made_on/created/updated/start_date/...)
    must be in [FLOOR_YEAR, today + 2d slack]
  - forward fields (resolves_by/target_date/end_date/next_earnings_date) may be
    future, but not past today + FORWARD_MAX_YEARS

A single fixed [2018, 2027] window (what year_derivation uses for raw mtimes)
would false-positive on legitimately old sources AND legitimately long-horizon
predictions — hence the split.

Two modes (mirrors check_prediction_dates):
  - default: scan wiki/{sources,predictions,entities,concepts}; print a report
    to stdout; exit 0 (for an agent / manual run).
  - --check --paths <files>: commit-gate; exit 1 on any violation, no output
    side effects. Used by the pre-commit hook and vault-autocommit.sh.

Env: WIKI_PATH (vault root).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
SCAN_DIRS = ["sources", "predictions", "entities", "concepts"]

FLOOR_YEAR = 1990          # below this = epoch/typo; no real content date precedes it
BACKWARD_SLACK_DAYS = 2    # clock-skew / timezone tolerance for "not future"
FORWARD_MAX_YEARS = 15     # a forward date past today + this many years is a typo

BACKWARD_FIELDS = {
    "published", "ingested", "made_on", "created", "updated",
    "last_material_move", "start_date", "generated", "date",
    "first_seen", "last_seen",
}
FORWARD_FIELDS = {
    "resolves_by", "target_date", "end_date", "next_earnings_date",
}

_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
_YEAR = re.compile(r"^(\d{4})\b")
_FULLDATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _get(fm: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
    return m.group(1).strip().strip('"').strip("'") if m else None


def classify_implausible(fm: str, today: date) -> list[str]:
    """Pure: return a list of implausible-date issue strings for a frontmatter
    block (empty if all date fields are plausible)."""
    issues: list[str] = []
    fwd_ceiling_year = today.year + FORWARD_MAX_YEARS
    back_ceiling = today + timedelta(days=BACKWARD_SLACK_DAYS)

    for field in sorted(BACKWARD_FIELDS | FORWARD_FIELDS):
        val = _get(fm, field)
        if not val:
            continue
        ym = _YEAR.match(val)
        if not ym:
            continue  # not a date-like value (e.g. horizon: "12 months")
        year = int(ym.group(1))

        if year < FLOOR_YEAR:
            issues.append(f"{field}={val}: implausible year {year} (before {FLOOR_YEAR})")
            continue

        if field in BACKWARD_FIELDS:
            full = _FULLDATE.match(val)
            if full:
                try:
                    d = date(int(full[1]), int(full[2]), int(full[3]))
                except ValueError:
                    continue  # malformed — that's check_prediction_dates' / schema's job
                if d > back_ceiling:
                    issues.append(f"{field}={val}: future date on a backward-looking field")
            elif year > today.year:
                issues.append(f"{field}={val}: future year on a backward-looking field")
        else:  # forward-looking
            if year > fwd_ceiling_year:
                issues.append(f"{field}={val}: implausibly far-future (year {year})")

    return issues


def _classify_file(p: Path, today: date) -> list[str]:
    try:
        txt = p.read_text(errors="replace")
    except OSError:
        return []
    m = _FM.match(txt)
    if not m:
        return []
    return classify_implausible(m.group(1), today)


def audit_files(paths, today: date) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for raw in paths:
        p = Path(raw)
        if p.name.startswith("_"):
            continue
        for issue in _classify_file(p, today):
            findings.append((p.name, issue))
    return findings


def audit_vault(today: date) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for sub in SCAN_DIRS:
        d = VAULT / "wiki" / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.md")):
            if p.name.startswith("_") or any(".bak." in part for part in p.parts):
                continue
            for issue in _classify_file(p, today):
                findings.append((str(p.relative_to(VAULT)), issue))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="implausible content-date check / gate")
    ap.add_argument("--check", action="store_true",
                    help="commit-gate mode: exit 1 on any violation, no side effects")
    ap.add_argument("--paths", nargs="*", default=None,
                    help="restrict to these staged files (gate mode)")
    args = ap.parse_args(argv)
    today = datetime.now(timezone.utc).date()

    if args.check:
        findings = audit_files(args.paths or [], today)
        if findings:
            print(f"✗ {len(findings)} implausible date(s) — fix before committing:",
                  file=sys.stderr)
            for name, issue in findings:
                print(f"    {name}: {issue}", file=sys.stderr)
            return 1
        print("✓ content dates plausible", file=sys.stderr)
        return 0

    findings = audit_vault(today)
    print(f"# Implausible content dates — {today.isoformat()}\n")
    if not findings:
        print("✅ No implausible date-field years across "
              f"wiki/{{{','.join(SCAN_DIRS)}}}.")
        return 0
    print(f"⚠️ **{len(findings)} implausible date field(s).** Likely year typos, "
          "epoch defaults, or future-dated backward fields.\n")
    print("| Page | Issue |")
    print("|------|-------|")
    for name, issue in findings:
        print(f"| `{name}` | {issue} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
