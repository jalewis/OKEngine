#!/usr/bin/env python3
"""Prove a cron-script stage actually landed, by comparing the TARGET against source.

A deploy's exit code says the deploy ran, not that the right bytes arrived. Both halves of that gap
have now been seen in one day on one fleet:

  * `deploy-cron-scripts.sh` returned 0 and printed `done.` on five gateways while okcti-test's
    pre-#267 fork of `nvd_import.py` overwrote the engine's copy on every single run. Three weeks.
    It surfaced only because somebody hashed the file inside the container.
  * a `| tail -3` on a fleet roll returned tail's status, so five hosts reported success having
    staged nothing.

So this is the check the deploy runs on ITSELF: for every script the stage was supposed to place,
does the copy in the container match the source it came from?

Split deliberately. The comparison is pure and fully tested here; getting the staged hashes out of
a container is one `docker exec ... sha256sum` in the caller, piped to `--staged-listing -`. There
is nothing to mock and nothing that only runs in production.

Verdicts, all of them failures, because each means the deploy did not do what it reported:
  drifted  staged bytes differ from source     -- something else wrote that name (a pack fork, a
                                                 hand edit, a half-finished copy)
  missing  in source, never arrived            -- the stage silently skipped it
  extra    staged, in no source                -- a fossil the reconcile step should have removed;
                                                 it stays importable and a stale lane keeps running it

An EMPTY staged listing is `undetectable`, never `clean`: proving nothing is not proving parity.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


def file_hash(path: Path) -> str:
    """sha256 of a file's bytes, or "" when it cannot be read.

    Unreadable is not "identical" and not "absent" -- it is a hash that will match nothing, so the
    file reports as drifted rather than quietly passing.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def source_hashes(dirs: list[Path], files: list[Path]) -> dict[str, str]:
    """{basename -> sha256} for every *.py the stage is expected to place.

    Later dirs win on a name collision, mirroring the deploy's own ordering (engine first, pack
    second). That is the shadowing bug's mechanism -- reproduce it faithfully here rather than
    reporting drift against a file the deploy never intended to be the live one.
    """
    out: dict[str, str] = {}
    for d in dirs:
        for py in sorted(d.glob("*.py")):        # glob-ok: flat script dir, not a content namespace
            out[py.name] = file_hash(py)
    for f in files:
        if f.is_file():
            out[f.name] = file_hash(f)
    return out


def parse_listing(text: str) -> dict[str, str]:
    """Parse `sha256sum` output into {basename -> hash}.

    Tolerates the ` ` / `*` binary marker and absolute paths. A line that is not a hash+path pair
    is skipped: `sha256sum` writes its errors to stderr, but a caller that merges streams must not
    turn "No such file" into an entry claiming some file hashes to "sha256sum:".
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*").strip()
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest.lower()):
            continue
        if not name.endswith(".py"):
            continue
        out[Path(name).name] = digest.lower()
    return out


def compare(source: dict[str, str], staged: dict[str, str]) -> dict[str, list[str]]:
    """Pure verdict. Every category is a way the deploy's own report was wrong."""
    src, stg = set(source), set(staged)
    return {
        "drifted": sorted(n for n in src & stg if source[n] != staged[n]),
        "missing": sorted(src - stg),
        "extra": sorted(stg - src),
    }


def report(result: dict[str, list[str]], *, checked: int) -> list[str]:
    """Human-readable lines. Says what was COMPARED, not just what was wrong -- a clean result over
    zero files reads identically to a clean result over a hundred otherwise."""
    lines: list[str] = []
    for kind, explain in (
            ("drifted", "staged bytes differ from source (something else wrote this name)"),
            ("missing", "in source but never staged (the deploy skipped it)"),
            ("extra", "staged but in no source (a fossil that stays importable)")):
        for name in result[kind]:
            lines.append(f"    {kind:8} {name}  -- {explain}")
    if not lines:
        lines.append(f"  staged scripts match source ({checked} file(s) compared)")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source-dir", action="append", default=[], type=Path,
                    help="directory of *.py the stage places (repeatable; later wins on collision)")
    ap.add_argument("--source-file", action="append", default=[], type=Path,
                    help="individually staged file (repeatable)")
    ap.add_argument("--staged-listing", required=True,
                    help="`sha256sum` output from the target, or - for stdin")
    ap.add_argument("--label", default="target", help="name of the target, for the report")
    args = ap.parse_args(argv)

    dirs = [d for d in args.source_dir if d.is_dir()]
    source = source_hashes(dirs, list(args.source_file))
    if not source:
        print(f"staged-drift [{args.label}]: no source scripts found in "
              f"{[str(d) for d in args.source_dir]} — parity is UNDETECTABLE, not proven",
              file=sys.stderr)
        return 1

    if args.staged_listing == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.staged_listing).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"staged-drift [{args.label}]: cannot read staged listing ({e}) — "
                  "parity is UNDETECTABLE, not proven", file=sys.stderr)
            return 1

    staged = parse_listing(text)
    if not staged:
        print(f"staged-drift [{args.label}]: the target listed no staged scripts — parity is "
              "UNDETECTABLE, not proven (is the path right? did the exec succeed?)", file=sys.stderr)
        return 1

    result = compare(source, staged)
    bad = sum(len(v) for v in result.values())
    if bad:
        print(f"ERROR: staged scripts do not match source on {args.label}:", file=sys.stderr)
        for line in report(result, checked=len(source)):
            print(line, file=sys.stderr)
        print("       The deploy reported success; the target disagrees. Re-stage, and if a name "
              "keeps coming back wrong, find what else writes it.", file=sys.stderr)
        return 1
    for line in report(result, checked=len(source)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
