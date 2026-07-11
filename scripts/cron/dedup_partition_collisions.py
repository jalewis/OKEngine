#!/usr/bin/env python3
"""Dedup pass for partition collisions (okengine#54).

When a partition-unaware writer put the same slug at more than one path (the flat root AND the
canonical shard, or a wrong-shaped shard like `security-incidents/YYYY/` vs the canonical
`YYYY/MM/`), this collapses every copy onto the ONE canonical page: union-merge the frontmatter,
keep the fullest body, delete the losers, and rewrite `[[links]]` that pointed at a dropped path.

This is the "dedup pass" that okf_migrate.build_map deliberately HOLDS collisions back for (it
refuses to move a page onto an occupied seat). The winning path is chosen by
okf_migrate.canonical_key — the SAME function the reshelve drain and the importers
(_okf_write.write_page) use — so cleanup, importer, and drain agree and the duplication loop
cannot re-open. deployment_validate.check_partition_dups() FAILs until this has run.

Safety: bodies are union-merged by keeping the LONGEST; if two copies carry materially different
bodies the merged page is stamped `needs_review: true` and logged, so nothing is silently dropped.
Pure script / no_agent. Idempotent (a second run is a no-op). Dry-run by DEFAULT.

Env: WIKI_PATH (default /opt/vault).
Usage: dedup_partition_collisions.py [--namespace NS] [--apply]
       (no --namespace = every partitioned namespace declared by root + sub-domain schemas)
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
import okf_migrate  # noqa: E402  — single source of the canonical-path logic

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.S)
_UNION = frozenset({"aliases", "tags", "related", "sources", "platforms", "related_actors"})
_SKIP = {"index", "log", "README"}


def _is_content(slug: str) -> bool:
    # generated per-directory artifacts (INDEX, paginated INDEX-p02/03, _* scaffolding) are NOT
    # addressable content — the same stem legitimately recurs in every shard dir, so they must
    # never be treated as duplicate content (matches okf_migrate.build_map's skip list).
    return not (slug.startswith("_") or slug.startswith("INDEX") or slug in _SKIP)


def _read(p: Path) -> tuple[dict, str]:
    m = _FM_RE.match(p.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return {}, ""
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        fm = None
    return (fm if isinstance(fm, dict) else {}), (m.group(2) or "")


def _merge_fm(copies: list[dict]) -> dict:
    """Union of every copy's frontmatter: first non-empty scalar wins; list fields in _UNION are
    unioned (order-preserving). Deterministic — copies are pre-sorted by the caller."""
    out: dict = {}
    for fm in copies:
        for k, v in fm.items():
            if v in (None, "", [], {}):
                continue
            if k in _UNION and isinstance(v, list):
                seen = {str(x).lower() for x in out.get(k, [])}
                out.setdefault(k, list(out.get(k, [])))
                for x in v:
                    if str(x).lower() not in seen:
                        seen.add(str(x).lower())
                        out[k].append(x)
            elif k not in out:
                out[k] = v
    return out


def _namespaces(root: Path, only: str | None) -> list[str]:
    if only:
        return [only]
    nss: list[str] = []

    def add(sp: Path, prefix: str):
        try:
            sch = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        for leaf, cfg in ((sch.get("partitioning") or {}).get("namespaces") or {}).items():
            if (cfg or {}).get("strategy", "flat") != "flat":
                nss.append(f"{prefix}{leaf}")

    if (root / "schema.yaml").is_file():
        add(root / "schema.yaml", "")
    wiki = root / "wiki"
    if wiki.is_dir():
        for sd in sorted(p for p in wiki.iterdir() if p.is_dir()):
            if (sd / "schema.yaml").is_file():
                add(sd / "schema.yaml", f"{sd.name}/")
    return nss


def dedup_namespace(root: Path, ns: str, apply: bool) -> tuple[dict[str, str], list[str], int]:
    """Collapse same-slug collisions in <ns>. Returns (move_map old_key->canonical_key for link
    rewrite, list of review-flagged slugs, count of files removed). Writes only when apply=True."""
    wiki = root / "wiki"
    base = wiki / ns
    if not base.is_dir():
        return {}, [], 0
    by_slug: dict[str, list[Path]] = {}
    for p in base.rglob("*.md"):
        if not _is_content(p.stem):
            continue
        by_slug.setdefault(p.stem, []).append(p)

    move_map: dict[str, str] = {}
    review: list[str] = []
    removed = 0
    for slug, paths in by_slug.items():
        if len(paths) < 2:
            continue
        # deterministic order: deepest (most-canonical-looking) first, then lexical
        paths = sorted(paths, key=lambda p: (-len(p.parts), p.as_posix()))
        parsed = [(p, *_read(p)) for p in paths]
        merged_fm = _merge_fm([fm for _p, fm, _b in parsed])
        canonical = okf_migrate.canonical_key(root, ns, slug, merged_fm)   # the ONE true path
        dest = wiki / (canonical + ".md")
        bodies = [b.strip() for _p, _fm, b in parsed if b.strip()]
        winner_body = max(bodies, key=len) if bodies else ""
        # materially different bodies among copies -> do not silently drop; flag for review
        if len({b for b in bodies}) > 1:
            merged_fm["needs_review"] = True
            review.append(slug)
        for p, _fm, _b in parsed:
            key = p.relative_to(wiki).as_posix()[:-3]
            if key != canonical:
                move_map[key] = canonical
        if apply:
            merged_fm = {k: v for k, v in merged_fm.items() if v not in (None, "", [], {})}
            head = yaml.safe_dump(merged_fm, sort_keys=False, allow_unicode=True).rstrip()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"---\n{head}\n---\n\n{winner_body}\n", encoding="utf-8")
            for p, _fm, _b in parsed:
                if p.resolve() != dest.resolve():
                    try:
                        p.unlink()
                        removed += 1
                    except OSError as e:
                        print(f"  ! remove failed {p}: {e}", file=sys.stderr)
    return move_map, review, removed


def _rewrite_links(root: Path, ns: str, move_map: dict[str, str], apply: bool) -> int:
    if not move_map:
        return 0
    pat, repl = okf_migrate.make_rewriter(move_map, ns)
    changed = 0
    for p in (root / "wiki").rglob("*.md"):
        if "/.git/" in p.as_posix():
            continue
        try:
            c = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"[[{ns}/" not in c:
            continue
        # Count ACTUAL rewrites, not `re.subn`'s match count: make_rewriter's repl returns the link
        # UNCHANGED for any target not in move_map (every `[[ns/…]]` link is a match), so subn's tally
        # counted untouched links too — it reported ~19675 "rewrites" when 6 links changed. Tally only
        # matches whose replacement text actually differs.
        n = [0]

        def _repl(m, _r=repl, _n=n):
            out = _r(m)
            if out != m.group(0):
                _n[0] += 1
            return out

        new_c = pat.sub(_repl, c)
        if n[0] and new_c != c:
            changed += n[0]
            if apply:
                try:
                    p.write_text(new_c, encoding="utf-8")
                except OSError as e:
                    print(f"  ! link rewrite failed {p}: {e}", file=sys.stderr)
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--root", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    args = ap.parse_args(argv)
    root = Path(args.root)
    mode = "APPLY" if args.apply else "DRY-RUN"

    total_dups = total_removed = total_links = 0
    all_review: list[str] = []
    for ns in _namespaces(root, args.namespace.strip() or None):
        move_map, review, removed = dedup_namespace(root, ns, args.apply)
        links = _rewrite_links(root, ns, move_map, args.apply)
        if move_map:
            print(f"{mode} {ns}: {len(move_map)} duplicate copy(ies) -> canonical"
                  f"{f', {removed} removed' if args.apply else ''}, {links} link(s) rewritten"
                  f"{f', {len(review)} flagged needs_review' if review else ''}")
            for k in list(move_map)[:6]:
                print(f"   {k}  ->  {move_map[k]}")
        total_dups += len(move_map)
        total_removed += removed
        total_links += links
        all_review += [f"{ns}/{s}" for s in review]
    print(f"dedup-partition-collisions: {mode} — {total_dups} duplicate copy(ies)"
          f"{f', {total_removed} removed' if args.apply else ' (would remove)'}, "
          f"{total_links} link(s) rewritten, {len(all_review)} needs_review")
    if all_review:
        print("  review (bodies differed, merged page stamped needs_review): "
              + ", ".join(all_review[:20]) + (" …" if len(all_review) > 20 else ""))
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
