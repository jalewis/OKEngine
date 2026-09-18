#!/usr/bin/env python3
"""Enforce exact statement and branch floors from a coverage.py JSON report."""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


def configured_floor(config: Path) -> float:
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    return float(data["tool"]["okengine"]["coverage"]["branch_fail_under"])


def percentage(covered: int, total: int) -> float:
    return 100.0 if total == 0 else covered * 100.0 / total


def check(report: Path, floor: float) -> tuple[bool, float, int, int]:
    data = json.loads(report.read_text(encoding="utf-8"))
    totals = data["totals"]
    covered = int(totals["covered_branches"])
    total = int(totals["num_branches"])
    percent = percentage(covered, total)
    return percent >= floor, percent, covered, total


def check_all(report: Path, line_floor: float, branch_floor: float,
              per_file: bool = False) -> tuple[bool, list[str]]:
    """Check statement and branch percentages, optionally for every measured file."""
    data = json.loads(report.read_text(encoding="utf-8"))
    scopes = [("TOTAL", data["totals"])]
    if per_file:
        scopes.extend((name, details["summary"])
                      for name, details in sorted(data["files"].items()))
    failures: list[str] = []
    for name, totals in scopes:
        measurements = (
            ("statements", int(totals["covered_lines"]), int(totals["num_statements"]),
             line_floor),
            ("branches", int(totals["covered_branches"]), int(totals["num_branches"]),
             branch_floor),
        )
        for category, covered, total, minimum in measurements:
            actual = percentage(covered, total)
            if actual < minimum:
                failures.append(
                    f"{name}: {category} {actual:.2f}% ({covered}/{total}); "
                    f"minimum {minimum:.2f}%"
                )
    return not failures, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--config", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--min", dest="minimum", type=float)
    parser.add_argument("--line-min", type=float)
    parser.add_argument("--per-file", action="store_true")
    args = parser.parse_args(argv)
    floor = args.minimum if args.minimum is not None else configured_floor(args.config)
    try:
        if args.line_min is not None or args.per_file:
            line_floor = floor if args.line_min is None else args.line_min
            ok, failures = check_all(args.report, line_floor, floor, args.per_file)
            for failure in failures:
                print(f"coverage: {failure}")
            if ok:
                print(f"coverage: all requested categories meet their minimums")
            return 0 if ok else 1
        ok, percent, covered, total = check(args.report, floor)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"branch coverage: invalid report/config: {exc}", file=sys.stderr)
        return 2
    print(f"branch coverage: {percent:.2f}% ({covered}/{total}); minimum {floor:.2f}%")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
