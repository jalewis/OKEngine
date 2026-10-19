#!/usr/bin/env python3
"""engine_source_guard.py — refuse to build a deployment from a source tree that is not release state.

A deployment's images are built from `$ENGINE_DIR`. Nothing checked WHAT that tree was, and on
2026-08-15 it pointed at a working checkout sitting on a feature branch, eight commits behind main.
A plain `docker compose build okengine-cockpit` there would have produced a cockpit with
`_subject_key` absent -- silently reverting a fix that was live and serving -- while reporting a
successful build, a healthy container and a rendering page. The deploy would have rolled the vault
BACKWARDS and every surface would have said it worked.

That is the same class the other deploy guards already cover from the other two directions:
`deployment_checks.check_write_path_libs` (baked vs staged) and `staged_drift.py` (source vs staged).
This is the third: source vs RELEASE STATE, checked before anything is built.

Refuses, rather than warns, because a warning in a deploy log is not a gate -- the whole failure
mode here is that every downstream signal looks green.

Env: OKENGINE_ALLOW_UNRELEASED_BUILD=1 to override (prints loudly and still reports the state, so
the override is visible in the log rather than silent).
"""
from __future__ import annotations

import argparse
import os
import subprocess
from subprocess import SubprocessError
import sys
import tempfile
from pathlib import Path

DEFAULT_ALLOWED = ("main",)


def _git(repo: Path, *args: str) -> str | None:
    """Run a git command, or None if it fails/git is absent. None means UNKNOWN, never 'clean'."""
    try:
        out = subprocess.run(["git", "-C", str(repo), *args],
                             capture_output=True, text=True, timeout=30)
    except (OSError, SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_state(repo: Path) -> dict:
    """Describe the tree. Every field may be None, which is reported as undetectable, not as ok."""
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    top_level = _git(repo, "rev-parse", "--show-toplevel") if inside == "true" else None
    # Git discovers parent repositories from arbitrary subdirectories. A build
    # context must be the checkout root whose revision we are attributing.
    if not top_level or Path(top_level).resolve() != repo.resolve():
        return {"is_repo": False, "head": None, "branch": None, "dirty": None, "tag": None}
    status = _git(repo, "status", "--porcelain")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "is_repo": True,
        "head": _git(repo, "rev-parse", "HEAD"),
        # A detached HEAD reports "HEAD"; treat that as no branch so a release tag is judged by tag.
        "branch": None if branch in (None, "HEAD") else branch,
        # None (git failed) is NOT False. A guard that reads a failed probe as "clean" is the bug.
        "dirty": None if status is None else bool(status),
        "tag": _git(repo, "describe", "--tags", "--exact-match"),
    }


def evaluate(state: dict, allowed: tuple[str, ...] = DEFAULT_ALLOWED) -> list[str]:
    """Return the reasons this tree must not be built from. Empty means it may be."""
    if not state.get("is_repo"):
        return ["not a git checkout — its contents cannot be attributed to any revision"]
    problems: list[str] = []
    if not state.get("head"):
        problems.append("could not determine the source revision (git rev-parse HEAD failed)")
    branch, tag, dirty = state.get("branch"), state.get("tag"), state.get("dirty")
    if dirty is None:
        problems.append("could not determine whether the tree is clean (git status failed)")
    elif dirty:
        problems.append("tree has uncommitted changes — the image would contain unreviewed code")
    # An exact release tag is a legitimate build source even on a detached HEAD; that is how a
    # rollback to a prior release is performed.
    if tag is None and branch not in allowed:
        where = f"branch {branch!r}" if branch else "a detached HEAD at no tag"
        problems.append(f"on {where}, not {' or '.join(allowed)} and not an exact release tag")
    return problems


def report(path: Path, state: dict, problems: list[str], allowed: tuple[str, ...]) -> None:
    where = state.get("tag") or state.get("branch") or "(detached)"
    head = (state.get("head") or "unknown")[:10]
    print(f"engine source: {path}")
    print(f"  ref {where} @ {head}  dirty={state.get('dirty')}")
    if not problems:
        print(f"  OK — buildable ({' or '.join(allowed)} or an exact release tag, clean)")
        return
    print("  REFUSED — this tree must not be built into a deployment:", file=sys.stderr)
    for problem in problems:
        print(f"    - {problem}", file=sys.stderr)
    print("  An image built here can silently REVERT code that is live and serving, while the "
          "build, the container health check and the page all report success.", file=sys.stderr)


def pin_deployment_source(pack: Path, engine: Path, state: dict) -> bool:
    """Persist the verified host build context, preserving operator configuration."""
    if not (pack / "docker-compose.yml").is_file():
        raise ValueError("deployment has no docker-compose.yml")
    if evaluate(state) or not state.get("head"):
        raise ValueError("cannot pin an unverified engine source")
    path = pack / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    source = str(engine.resolve())
    if any(char in source for char in ("\n", "\r")):
        raise ValueError("engine source path must be one line")
    desired = {"ENGINE_DIR": "'" + source.replace("'", "\\'") + "'",
               "OKENGINE_SOURCE_SHA": state["head"]}
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        if key in desired:
            if key not in seen:
                out.append(f"{key}={desired[key]}")
                seen.add(key)
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in desired.items() if key not in seen)
    body = "\n".join(out).rstrip() + "\n"
    changed = not path.exists() or path.read_text(encoding="utf-8") != body
    # Stage owner-only content beside the original. Failed writes/rename must not
    # truncate operator secrets; replace only after the complete file is durable.
    owner = path.stat() if path.exists() else None
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=pack,
                                     prefix=".okengine-source-", delete=False) as handle:
        staged = Path(handle.name)
        try:
            if owner is not None:
                os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine_dir", nargs="?", default=os.environ.get("ENGINE_DIR", "."))
    parser.add_argument("--allow-branch", action="append", default=None,
                        help="branch that may be built from (repeatable; default: main)")
    parser.add_argument("--pack", type=Path,
                        help="persist the verified ENGINE_DIR and source SHA into this deployment")
    parser.add_argument("--expected-sha", help="refuse a different source revision")
    args = parser.parse_args(argv)

    allowed = tuple(args.allow_branch or DEFAULT_ALLOWED)
    path = Path(args.engine_dir).resolve()
    state = git_state(path)
    problems = evaluate(state, allowed)
    if args.expected_sha and state.get("head") != args.expected_sha:
        problems.append("source revision differs from the expected qualified SHA")
    report(path, state, problems, allowed)
    if not problems:
        if args.pack:
            try:
                changed = pin_deployment_source(args.pack.resolve(), path, state)
            except (OSError, ValueError) as exc:
                print(f"source configuration refused: {exc}", file=sys.stderr)
                return 1
            print("  deployment build source pinned" if changed else "  deployment build source current")
        return 0
    if os.environ.get("OKENGINE_ALLOW_UNRELEASED_BUILD") == "1":
        print("  OVERRIDDEN by OKENGINE_ALLOW_UNRELEASED_BUILD=1 — proceeding anyway",
              file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
