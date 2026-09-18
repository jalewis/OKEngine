#!/usr/bin/env python3
"""Remove synthetic Qwen qualification artifacts from deployed pack corpora."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


# The pack set is OPERATOR INPUT, not an engine literal (okengine#510). The tuple this
# replaced held five of one operator's deployment names inside a domain-agnostic engine, and
# it was argparse `choices` — so on any other fleet `--pack` REJECTED every real pack name,
# and the default list matched nothing, deleting nothing and reporting success.
#
# Auto-discovery is deliberately NOT used here: "has pack.yaml" matches archived checkouts
# and pack source repos as well as deployments (13 directories on this host, only 5 of them
# targets). This script DELETES under --apply, so widening its default set is the most
# dangerous possible way to be wrong.


def requested_packs(cli_packs: list[str] | None) -> list[str]:
    """The packs to clean: repeated --pack, else OKENGINE_QUALIFICATION_PACKS (comma-separated)."""
    if cli_packs:
        return list(dict.fromkeys(p.strip() for p in cli_packs if p.strip()))
    env = os.environ.get("OKENGINE_QUALIFICATION_PACKS", "")
    return list(dict.fromkeys(p.strip() for p in env.split(",") if p.strip()))
SCOPES = (
    "raw/qualification",
    "wiki/concepts",
    "wiki/entities",
    "wiki/gaps",
    "wiki/predictions",
    "wiki/sources",
    "wiki/vendors",
    "wiki/operational",
)
FILE_SIGNATURE = re.compile(
    r"qwen-(?:final|qualification|readiness|page-quality|laboratory)|"
    r"(?:g\d|generation-).*(?:qwen-laboratory|verification-cooperative)",
    re.IGNORECASE,
)
CONTENT_SIGNATURE = re.compile(
    r"qualification\.invalid|qualification_fixture:\s*true",
    re.IGNORECASE,
)


def candidates(pack: Path, generation: str | None = None) -> list[Path]:
    token = generation.lower() if generation else None
    found: list[Path] = []
    for scope in SCOPES:
        root = pack / scope
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            relative = str(path.relative_to(pack)).lower()
            if token and token not in relative and token not in text.lower():
                continue
            # Content references are transitive: a genuine prediction or a
            # generated INDEX may link to a fixture. Only explicit fixture
            # markers/URLs or fixture-like filenames qualify for deletion.
            if FILE_SIGNATURE.search(path.name) or CONTENT_SIGNATURE.search(text):
                found.append(path)
    return sorted(set(found))


def main() -> int:
    parser = argparse.ArgumentParser()
    # No deployment default: the engine ships no operator filesystem layout.
    # This script DELETES with --apply, so a wrong inherited default would be
    # destructive; require the root explicitly.
    parser.add_argument("--pack-root", default=os.environ.get("OKENGINE_PACK_ROOT", ""))
    # No argparse `choices`: the valid set is fleet composition, not an engine constant.
    # Every requested pack is still validated as a real checkout under the root below.
    parser.add_argument("--pack", action="append",
                        help="pack directory name to clean; repeatable. Defaults to "
                             "OKENGINE_QUALIFICATION_PACKS (comma-separated).")
    parser.add_argument("--generation")
    parser.add_argument(
        "--apply", action="store_true",
        help="delete matched files; without this flag only print the plan",
    )
    args = parser.parse_args()
    if not args.pack_root.strip():
        raise SystemExit(
            "no pack root: pass --pack-root or set OKENGINE_PACK_ROOT to the "
            "directory containing the pack checkouts")
    root = Path(args.pack_root).resolve()
    packs = requested_packs(args.pack)
    if not packs:
        raise SystemExit(
            "no packs requested: pass --pack (repeatable) or set "
            "OKENGINE_QUALIFICATION_PACKS to a comma-separated list of pack directory "
            "names. There is deliberately no built-in list — this script deletes, and "
            "removing nothing must never look like a successful cleanup (okengine#510)")
    total = 0
    for name in packs:
        pack = (root / name).resolve()
        if pack.parent != root or not pack.is_dir():
            raise SystemExit(f"invalid pack path: {pack}")
        if not (pack / "pack.yaml").is_file():
            raise SystemExit(f"not a pack checkout (no pack.yaml): {pack}")
        paths = candidates(pack, args.generation)
        for path in paths:
            print(f"{'REMOVE' if args.apply else 'WOULD REMOVE'} {path}")
            if args.apply:
                path.unlink()
        print(f"{name}: {len(paths)} qualification artifact(s)")
        total += len(paths)
    print(f"total: {total} qualification artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
