#!/usr/bin/env python3
"""reshard-oversized — split leaf buckets that exceed the OKF entry-count rule.

Config-driven (M2): instead of hardcoding sources/concepts, this reads each
domain-pack `schema.yaml` `partitioning` block (root + every wiki/*/schema.yaml)
and, for any namespace that declares a `reshard_by` directive, shards leaf dirs
exceeding `reshard_over` (default 500) one level deeper, link-preserving (reuses
okf_migrate's rewriter):
  - reshard_by: day            sources/{y}/{m}/ (>N) -> sources/{y}/{m}/{day}/
  - reshard_by: second-letter  concepts/{l}/  (>N)   -> concepts/{l}/{2nd}/
A namespace with `reshard_by: not-applicable` (or none) is skipped. A new domain
gets resharding for free by declaring `reshard_by` in its schema.

Idempotent: only dirs with >reshard_over direct .md files are touched; already-
sharded sub-dirs are left alone. Pure script / no_agent.

Usage: reshard_oversized.py [--dry-run]   (default applies)
Env: WIKI_PATH (default /opt/vault)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okf_migrate  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DEFAULT_MAX = 500

# reshard_by -> (glob suffix under the namespace dir locating leaf buckets,
#                shard-key function for a file in that leaf)
# Shard-key via okf_migrate.reshard_seg — THE single source shared with the reshelve drain, so a
# split here can't be reverted there (they computed the same segment two different ways before).
_RESHARD_BY = {
    "day": ("*/*", lambda f: okf_migrate.reshard_seg("day", f.stem, _fm(f))),
    "second-letter": ("*", lambda f: okf_migrate.reshard_seg("second-letter", f.stem, _fm(f))),
}


def _fm(p: Path) -> dict:
    try:
        m = okf_migrate._FM_RE.match(p.read_text(encoding="utf-8", errors="replace")[:4000])
    except OSError:
        return {}
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _reshardable_namespaces() -> list[tuple[str, str, int]]:
    """(full-namespace, reshard_by, reshard_over) for every namespace across all
    domain packs that declares a usable reshard_by directive."""
    out: list[tuple[str, str, int]] = []

    def add(schema_path: Path, prefix: str) -> None:
        try:
            sch = yaml.safe_load(schema_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        part = sch.get("partitioning") or {}
        over = int(part.get("reshard_over") or DEFAULT_MAX)
        for leaf, cfg in (part.get("namespaces") or {}).items():
            rb = (cfg or {}).get("reshard_by")
            if rb in _RESHARD_BY:
                out.append((f"{prefix}{leaf}", rb, over))

    if (VAULT / "schema.yaml").is_file():
        add(VAULT / "schema.yaml", "")
    for sd in sorted(WIKI.iterdir()):
        if sd.is_dir() and (sd / "schema.yaml").is_file():
            add(sd / "schema.yaml", f"{sd.name}/")
    return out


def _oversized(glob_pat: str, max_n: int):
    for d in sorted(WIKI.glob(glob_pat)):  # glob-ok: glob_pat targets the shard-leaf level (namespace/<suffix>)
        if not d.is_dir():
            continue
        files = [f for f in d.glob("*.md")  # glob-ok: d is a resolved shard-leaf dir
                 if f.name != "INDEX.md" and not f.name.startswith("_")]
        # Once a bucket has been split, every direct content file in that bucket is residual
        # non-canonical state even when the direct count has fallen below the original threshold.
        # Sweeping those stragglers prevents a fixed importer/migration from leaving a permanently
        # mixed one-level/two-level layout (#243).
        already_split = any(p.is_dir() for p in d.iterdir())
        if len(files) > max_n or (files and already_split):
            yield d, files


def _build_map(namespace: str, reshard_by: str, max_n: int) -> dict[str, str]:
    suffix, keyfn = _RESHARD_BY[reshard_by]
    leaves = _oversized(f"{namespace}/{suffix}", max_n)
    m: dict[str, str] = {}
    for d, files in leaves:
        base = d.relative_to(WIKI).as_posix()
        for f in files:
            m[f"{base}/{f.stem}"] = f"{base}/{keyfn(f)}/{f.stem}"
    return m


def _apply(namespace: str, reshard_by: str, max_n: int, apply: bool) -> int:
    mp = _build_map(namespace, reshard_by, max_n)
    if not mp:
        print(f"{namespace}: no oversized leaves (>{max_n})")
        return 0
    leaves = {v.rsplit("/", 2)[0] for v in mp.values()}
    print(f"{namespace}: {'resharding' if apply else 'would reshard'} {len(mp)} files "
          f"from {len(leaves)} oversized leaf(s) by {reshard_by}")
    if not apply:
        return 0
    pat, repl = okf_migrate.make_rewriter(mp, namespace)
    # Bare (non-wikilink) references — e.g. an assessment's `subject: entities/a/foo` frontmatter
    # scalar — go stale too when the target moves deeper; the wikilink rewriter never touches them
    # (#336). Rewrite both, and no longer skip files whose ONLY reference is
    # bare (the old `[[namespace/` gate dropped exactly the assessment records that broke).
    bpat, brepl = okf_migrate.make_path_rewriter(mp)
    rew = 0
    for p in WIKI.rglob("*.md"):
        if "/.git/" in p.as_posix():
            continue
        try:
            c = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"{namespace}/" not in c:
            continue
        nc, _ = pat.subn(repl, c)
        nc, _ = bpat.subn(brepl, nc)
        if nc != c:
            try:
                p.write_text(nc, encoding="utf-8")
                rew += 1
            except OSError:
                pass
    moved = 0
    for old, new in mp.items():
        src, dst = WIKI / (old + ".md"), WIKI / (new + ".md")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            moved += 1
        except OSError as e:
            print(f"  ! {old}: {e}", file=sys.stderr)
    print(f"{namespace}: rewrote links in {rew} files, moved {moved}")
    return moved


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="preview only; default applies (it's an idempotent maintainer drain)")
    args = ap.parse_args(argv)
    apply = not args.dry_run
    targets = _reshardable_namespaces()
    print(f"reshard-oversized: reshardable namespaces = "
          f"{[(ns, rb, n) for ns, rb, n in targets]}")
    total = sum(_apply(ns, rb, n, apply) for ns, rb, n in targets)
    print(f"reshard-oversized: {total} files {'resharded' if apply else '(dry-run)'}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
