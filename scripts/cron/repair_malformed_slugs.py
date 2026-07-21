#!/usr/bin/env python3
"""Plan/apply repair of whitespace or run-on page basenames (#240).

Uses the page's human title/name as the replacement key, falls back to the old
stem, and delegates bounded/collision-safe normalization to id_lib. Dry-run is
the default; ``--apply`` renames pages and rewrites exact wikilink targets.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import id_lib  # noqa: E402

MAX_ENTITY_SLUG_LEN = 80
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---", re.S)


def malformed(path: Path) -> bool:
    return any(ch.isspace() for ch in path.stem) or len(path.stem) > MAX_ENTITY_SLUG_LEN


def bounded_slug(value: str) -> str:
    slug = id_lib.normalize_key(value)
    if len(slug) <= MAX_ENTITY_SLUG_LEN:
        return slug
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=4).hexdigest()
    return slug[: MAX_ENTITY_SLUG_LEN - len(digest) - 1].rstrip("-") + "-" + digest


def repair(vault: Path, apply: bool = False) -> tuple[list[tuple[str, str]], list[str]]:
    wiki = vault / "wiki"
    moves, errors = [], []
    pages = list(wiki.rglob("*.md"))
    for old in pages:
        try:
            rel_parts = old.relative_to(wiki).parts
        except ValueError:
            continue
        if not rel_parts or rel_parts[0] != "entities" or not malformed(old):
            continue
        try:
            text = old.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _FM.match(text)
        try:
            fm = yaml.safe_load(match.group(1)) if match else {}
        except yaml.YAMLError:
            fm = {}
        human = (fm or {}).get("name") or (fm or {}).get("title") or old.stem
        new = old.with_name(bounded_slug(str(human)) + ".md")
        if new.resolve() == old.resolve():
            continue
        if new.exists() and new.resolve() != old.resolve():
            errors.append(f"{old.relative_to(wiki)} -> collision at {new.relative_to(wiki)}")
            continue
        old_rel = old.relative_to(wiki).as_posix()[:-3]
        new_rel = new.relative_to(wiki).as_posix()[:-3]
        moves.append((old_rel, new_rel))
        if not apply:
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)
        for page in pages:
            if page == old or not page.exists():
                continue
            try:
                body = page.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if old_rel in body:
                try:
                    page.write_text(body.replace(old_rel, new_rel), encoding="utf-8")
                except OSError:
                    errors.append(f"could not rewrite reference in {page.relative_to(wiki)}")
    return moves, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=Path("/opt/vault"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    moves, errors = repair(args.vault, args.apply)
    for old, new in moves:
        print(f"{'MOVE' if args.apply else 'WOULD MOVE'} {old} -> {new}")
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    print(f"malformed-slug-repair: {len(moves)} move(s), {len(errors)} collision/error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
