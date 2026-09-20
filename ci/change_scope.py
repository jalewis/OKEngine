#!/usr/bin/env python3
"""Classify an exact Git diff and enforce the docs-only pipeline budget."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path


DOC_ROOTS = ("docs/", "drafts/")
DOC_NAMES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "INSTALL.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
    "TESTING.md",
}
DOC_SUFFIXES = (".md", ".mdx", ".rst")
BEHAVIOR_ROOTS = ("prompts/", "templates/")


def is_documentation(path: str) -> bool:
    normalized = path.strip().lstrip("./")
    if normalized.startswith(BEHAVIOR_ROOTS) or "/prompts/" in normalized:
        return False
    return (
        normalized in DOC_NAMES
        or normalized.startswith(DOC_ROOTS)
        or normalized.endswith(DOC_SUFFIXES)
    )


def classify(paths: list[str]) -> dict[str, object]:
    cleaned = sorted({path.strip() for path in paths if path.strip()})
    if not cleaned:
        raise ValueError("diff contains no changed paths")
    non_docs = [path for path in cleaned if not is_documentation(path)]
    return {
        "scope": "docs-only" if not non_docs else "mixed-or-code",
        "docs_only": not non_docs,
        "paths": cleaned,
        "non_documentation_paths": non_docs,
    }


def changed_paths(base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        raise ValueError("a non-zero diff base is required")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRD", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def evaluate_budget(
    classification: dict[str, object], created_at: str, now: str, budget_seconds: int
) -> dict[str, object]:
    started = parse_time(created_at)
    finished = parse_time(now)
    elapsed = (finished - started).total_seconds()
    if elapsed < 0:
        raise ValueError("pipeline creation time is later than the verdict time")
    applies = bool(classification["docs_only"])
    return {
        **classification,
        "budget_applies": applies,
        "budget_seconds": budget_seconds,
        "elapsed_seconds": round(elapsed, 3),
        "within_budget": (elapsed <= budget_seconds) if applies else None,
        "pipeline_created_at": started.isoformat(),
        "verdict_at": finished.isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pipeline-created-at")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    parser.add_argument("--budget-seconds", type=int, default=300)
    args = parser.parse_args(argv)

    try:
        result = classify(changed_paths(args.base, args.head))
        if args.pipeline_created_at:
            now = args.now or dt.datetime.now(dt.timezone.utc).isoformat()
            result = evaluate_budget(
                result, args.pipeline_created_at, now, args.budget_seconds
            )
    except (ValueError, subprocess.CalledProcessError) as exc:
        result = {"scope": "undetectable", "error": str(exc)}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"change-scope: UNDETECTABLE: {exc}")
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("change-scope: " + json.dumps(result, sort_keys=True))
    if result.get("budget_applies") and not result.get("within_budget"):
        print(
            f"change-scope: FAIL docs-only pipeline exceeded "
            f"{args.budget_seconds}s budget"
        )
        return 1
    print("change-scope: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
