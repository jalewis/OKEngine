#!/usr/bin/env python3
"""Canonical, machine-readable provenance for engineering evidence."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _release(repo: Path) -> str | None:
    try:
        text = (repo / "engine-manifest.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?m)^engine_release:\s*([^\s#]+)", text)
    return match.group(1) if match else None


def collect(repo: Path) -> dict:
    repo = repo.resolve()
    inside = _git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") if inside else None
    if branch == "HEAD":
        branch = None
    distance = _git(repo, "rev-list", "--left-right", "--count", "HEAD...origin/main") \
        if inside else None
    ahead = behind = None
    if distance and re.fullmatch(r"\d+\s+\d+", distance):
        ahead, behind = map(int, distance.split())
    status = _git(repo, "status", "--porcelain") if inside else None
    tag = _git(repo, "describe", "--tags", "--exact-match") if inside else None
    return {
        "schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "repository": str(repo), "engine_release": _release(repo), "is_repo": inside,
        "sha": _git(repo, "rev-parse", "HEAD") if inside else None,
        "branch": branch, "exact_tag": tag,
        "detached": bool(inside and branch is None),
        "dirty": None if status is None else bool(status),
        "origin_main": _git(repo, "rev-parse", "origin/main") if inside else None,
        "ahead_of_origin_main": ahead, "behind_origin_main": behind,
        "remote_distance_available": ahead is not None and behind is not None,
    }


def problems(context: dict) -> list[str]:
    errors = []
    if not context.get("is_repo") or not context.get("sha"):
        errors.append("source is not attributable to a Git revision")
    if not context.get("engine_release"):
        errors.append("engine release is unavailable")
    if context.get("dirty") is None:
        errors.append("working-tree state is unavailable")
    elif context.get("dirty"):
        errors.append("working tree is dirty")
    if not context.get("remote_distance_available"):
        errors.append("origin/main distance is unavailable")
    elif context.get("behind_origin_main"):
        errors.append(f"revision is {context['behind_origin_main']} commit(s) behind origin/main")
    if context.get("detached") and not context.get("exact_tag"):
        errors.append("detached revision is not an exact tag")
    return errors


def render(context: dict, errors: list[str], *, overridden: bool = False) -> str:
    ref = context.get("exact_tag") or context.get("branch") or "(detached)"
    distance = (f"+{context.get('ahead_of_origin_main')}/-{context.get('behind_origin_main')}"
                if context.get("remote_distance_available") else "unavailable")
    lines = [
        f"review context: {ref} @ {(context.get('sha') or 'unknown')[:12]}",
        f"  release={context.get('engine_release') or 'unknown'} dirty={context.get('dirty')} "
        f"origin/main={distance}",
    ]
    if errors:
        lines.append("  REFUSED" if not overridden else "  OVERRIDDEN (unattributable evidence)")
        lines.extend(f"    - {error}" for error in errors)
    else:
        lines.append("  attributable and current")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--artifact")
    parser.add_argument("--allow-unattributable", action="store_true")
    args = parser.parse_args(argv)
    context = collect(Path(args.repo))
    errors = problems(context)
    override = args.allow_unattributable or \
        os.environ.get("OKENGINE_ALLOW_UNATTRIBUTABLE_EVIDENCE") == "1"
    payload = {**context, "problems": errors, "override": override and bool(errors)}
    if args.artifact:
        target = Path(args.artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True) if args.json else render(context, errors,
                                                                      overridden=override))
    return 1 if args.strict and errors and not override else 0


if __name__ == "__main__":
    raise SystemExit(main())
