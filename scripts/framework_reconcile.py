#!/usr/bin/env python3
"""Reconcile files surfaced by ``framework pull --update`` (#61).

The pull step deliberately never overwrites an operator-owned definition file. Changed upstream
versions land beside it as ``<file>.upstream``. This command provides the missing review workflow:

* list pending files or show an inline unified diff;
* explicitly accept upstream or keep the local file;
* invoke an operator-selected two-file merge tool;
* validate the resulting pack after the final pending file is resolved.

Examples:
  framework reconcile <pack>
  framework reconcile <pack> --show schema.yaml
  framework reconcile <pack> --accept README.md
  framework reconcile <pack> --keep pack.yaml
  framework reconcile <pack> --merge schema.yaml --merge-tool 'meld'
  framework reconcile <pack> --interactive

A merge tool is invoked as ``<tool args> LOCAL UPSTREAM`` and must update LOCAL. The upstream copy
is removed only when the tool exits successfully and LOCAL actually changed.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent


def pending(pack: Path) -> list[Path]:
    if not pack.is_dir():
        return []
    return sorted(
        path.relative_to(pack)
        for path in pack.rglob("*.upstream")
        if path.is_file()
    )


def _pair(pack: Path, value: str) -> tuple[Path, Path, Path]:
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("file must be a relative path inside the pack")
    if raw.name.endswith(".upstream"):
        raw = raw.with_name(raw.name.removesuffix(".upstream"))
    local = pack / raw
    upstream = local.with_name(local.name + ".upstream")
    try:
        local.resolve().relative_to(pack.resolve())
        upstream.resolve().relative_to(pack.resolve())
    except ValueError as exc:
        raise ValueError("file must stay inside the pack") from exc
    if not local.is_file():
        raise ValueError(f"local file does not exist: {raw.as_posix()}")
    if not upstream.is_file():
        raise ValueError(f"no pending upstream copy: {raw.as_posix()}.upstream")
    return raw, local, upstream


def diff(pack: Path, value: str) -> str:
    rel, local, upstream = _pair(pack, value)
    local_lines = local.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    upstream_lines = upstream.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        local_lines,
        upstream_lines,
        fromfile=f"{rel.as_posix()} (local)",
        tofile=f"{rel.as_posix()} (upstream)",
    ))


def accept(pack: Path, value: str) -> Path:
    rel, local, upstream = _pair(pack, value)
    temp = local.with_name(local.name + ".reconcile.tmp")
    try:
        shutil.copy2(upstream, temp)
        os.replace(temp, local)
        upstream.unlink()
    finally:
        temp.unlink(missing_ok=True)
    return rel


def keep(pack: Path, value: str) -> Path:
    rel, _local, upstream = _pair(pack, value)
    upstream.unlink()
    return rel


def merge(pack: Path, value: str, tool: str) -> tuple[Path | None, str | None]:
    rel, local, upstream = _pair(pack, value)
    command = shlex.split(tool or "")
    if not command:
        return None, "merge needs --merge-tool or OKENGINE_MERGE_TOOL"
    before = local.read_bytes()
    try:
        result = subprocess.run([*command, str(local), str(upstream)], check=False)
    except OSError as exc:
        return None, f"could not run merge tool: {exc}"
    if result.returncode != 0:
        return None, f"merge tool exited {result.returncode}; pending copy retained"
    if not local.is_file() or local.read_bytes() == before:
        return None, "merge tool did not change the local file; pending copy retained"
    upstream.unlink()
    return rel, None


def _validate(pack: Path) -> int:
    spec = importlib.util.spec_from_file_location(
        "framework_validate", ENGINE_ROOT / "scripts" / "framework_validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main([str(pack), "--quiet"])


def _finish(pack: Path, changed: bool, no_validate: bool) -> int:
    remaining = pending(pack)
    if remaining:
        print(f"  pending: {len(remaining)} file(s); validation waits until all are resolved")
        return 0
    print("  ✓ no pending .upstream files")
    if changed and not no_validate:
        print("  → validating reconciled pack")
        return _validate(pack)
    return 0


def _interactive(pack: Path, tool: str, no_validate: bool) -> int:
    changed = False
    for rel_upstream in pending(pack):
        rel = rel_upstream.with_name(rel_upstream.name.removesuffix(".upstream"))
        print(f"\n=== {rel.as_posix()} ===")
        print(diff(pack, rel.as_posix()) or "(files differ only in undecodable/binary content)")
        while True:
            action = input("[a]ccept upstream / [k]eep local / [m]erge / [s]kip / [q]uit: ").strip().lower()
            if action in {"a", "accept"}:
                accept(pack, rel.as_posix())
                print(f"  accepted: {rel}")
                changed = True
                break
            if action in {"k", "keep"}:
                keep(pack, rel.as_posix())
                print(f"  kept local: {rel}")
                changed = True
                break
            if action in {"m", "merge"}:
                merged, error = merge(pack, rel.as_posix(), tool)
                if error:
                    print(f"  ERROR: {error}", file=sys.stderr)
                    continue
                print(f"  merged: {merged}")
                changed = True
                break
            if action in {"s", "skip"}:
                break
            if action in {"q", "quit"}:
                return _finish(pack, changed, no_validate)
            print("  choose a, k, m, s, or q")
    return _finish(pack, changed, no_validate)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="framework reconcile", description=__doc__)
    parser.add_argument("pack", help="existing pack directory containing .upstream files")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--show", metavar="FILE", help="show the local vs upstream unified diff")
    actions.add_argument("--accept", metavar="FILE", help="replace local with upstream")
    actions.add_argument("--keep", metavar="FILE", help="keep local and discard upstream")
    actions.add_argument("--merge", metavar="FILE", help="merge with the configured two-file tool")
    actions.add_argument("--interactive", action="store_true", help="review every pending file")
    parser.add_argument("--merge-tool", default=os.environ.get("OKENGINE_MERGE_TOOL", ""),
                        help="command invoked as TOOL LOCAL UPSTREAM (or OKENGINE_MERGE_TOOL)")
    parser.add_argument("--no-validate", action="store_true",
                        help="do not validate after the final pending file is resolved")
    args = parser.parse_args(argv)

    pack = Path(args.pack).expanduser().resolve()
    if not pack.is_dir():
        print(f"ERROR: pack directory does not exist: {pack}", file=sys.stderr)
        return 2

    try:
        if args.show:
            print(diff(pack, args.show), end="")
            return 0
        if args.accept:
            rel = accept(pack, args.accept)
            print(f"  accepted upstream: {rel}")
            return _finish(pack, True, args.no_validate)
        if args.keep:
            rel = keep(pack, args.keep)
            print(f"  kept local: {rel}")
            return _finish(pack, True, args.no_validate)
        if args.merge:
            rel, error = merge(pack, args.merge, args.merge_tool)
            if error:
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"  merged: {rel}")
            return _finish(pack, True, args.no_validate)
        if args.interactive:
            return _interactive(pack, args.merge_tool, args.no_validate)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    files = pending(pack)
    print(f"pending upstream changes: {len(files)}")
    for rel in files:
        print(f"  - {rel}")
    if files:
        print("review with --show FILE or --interactive; resolve with --accept/--keep/--merge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
