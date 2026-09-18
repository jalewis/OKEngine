#!/usr/bin/env python3
"""Dedup pass for partition collisions (okengine#54).

When a partition-unaware writer put the same slug at more than one path (the flat root AND the
canonical shard, or a wrong-shaped shard like `security-incidents/YYYY/` vs the canonical
`YYYY/MM/`), this collapses every copy onto the ONE canonical page: union-merge the frontmatter,
keep the fullest body, delete the losers, and rewrite `[[links]]` that pointed at a dropped path.

This is the "dedup pass" that okf_migrate.build_map deliberately HOLDS collisions back for (it
refuses to move a page onto an occupied seat). The winning path is chosen by
okf_migrate.write_key — the SAME function direct importers use — so cleanup, importer, reshard,
and drain agree and the duplication loop
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
# Scalars that legitimately differ between copies of the same page (stamps, counters, the review
# flag itself). Any OTHER scalar that disagrees between LIVE copies is a material conflict:
# `status: live` vs `retracted`, two different `id`s. Depth must not decide those silently
# (okengine#663 — the assessments incident: 9 of 48 duplicated records disagreed about whether a
# judgment was live or retracted, and the answer depended on which directory a consumer walked).
_VOLATILE = frozenset({
    "updated", "last_updated", "created", "modified", "last_modified", "last_modified_by",
    "maintained_by", "discovered_by", "version", "page_version", "content_hash", "sha256",
    "needs_review", "reviewed_by", "reviewed_on", "reviewed_at", "tier", "activity_tier",
})


def _scalar_conflicts(copies: list[dict]) -> dict[str, list]:
    """{field: [distinct values]} for every non-volatile, non-union scalar that LIVE copies
    disagree on. Empty values never count as a disagreement."""
    seen: dict[str, list] = {}
    for fm in copies:
        for k, v in fm.items():
            if k in _UNION or k in _VOLATILE or isinstance(v, (list, dict)) or v in (None, ""):
                continue
            vals = seen.setdefault(k, [])
            if v not in vals:
                vals.append(v)
    return {k: v for k, v in seen.items() if len(v) > 1}


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
    return okf_migrate.partitioned_namespaces(root)


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
        key_of = lambda p: p.relative_to(wiki).as_posix()[:-3]  # noqa: E731
        # A tombstone never occupies a seat and never wins a merge (okengine#663) — the same rule
        # okf_migrate.build_map applies. Only LIVE copies are merge candidates.
        live = [t for t in parsed if not okf_migrate.is_tombstoned(t[1])]
        tombs = [t for t in parsed if okf_migrate.is_tombstoned(t[1])]
        if not live:
            continue        # duplicate tombstones: redirects are load-bearing, not this drain's call
        merged_fm = _merge_fm([fm for _p, fm, _b in live])
        conflicts = _scalar_conflicts([fm for _p, fm, _b in live])
        survivor_id = str(merged_fm.get("id") or "").strip()
        live_keys = {key_of(p) for p, _fm, _b in live}
        # A REDUNDANT tombstone points at the survivor (by path or id): once the live page holds
        # the seat the redirect is superfluous, so it is a loser like any other copy. This is the
        # exact call build_map defers to the dedup pass. Anything else the tombstone points at is
        # still load-bearing: never overwritten, never merged, only reported.
        redundant, blocking = [], []
        for p, fm, _b in tombs:
            sup = str(fm.get("superseded_by") or "").strip()
            hits_survivor = bool(sup) and (sup in live_keys or (bool(survivor_id) and sup == survivor_id))
            (redundant if hits_survivor else blocking).append(p)
        desired = okf_migrate.desired_key(root, ns, slug, merged_fm)
        if (wiki / (desired + ".md")) in {p for p in redundant}:
            canonical = desired                      # take the seat a redundant redirect held
        else:
            canonical = okf_migrate.write_key(root, ns, slug, merged_fm)   # writer/reshard contract
        # redundant tombstones ELSEWHERE point at the survivor too: retired, links rewritten below
        dest = wiki / (canonical + ".md")
        for p in blocking:
            print(f"   blocked: {ns}/{slug} — tombstone {key_of(p)} points elsewhere "
                  f"({(_read(p)[0].get('superseded_by') or '?')}); left in place", file=sys.stdout)
        if len(live) < 2 and not redundant:
            continue                                  # one live page + load-bearing redirect(s): nothing to collapse
        bodies = [b.strip() for _p, _fm, b in live if b.strip()]
        winner_body = max(bodies, key=len) if bodies else ""
        # materially different bodies among live copies -> do not silently drop; flag for review
        if len({b for b in bodies}) > 1:
            merged_fm["needs_review"] = True
            review.append(slug)
        if conflicts:
            merged_fm["needs_review"] = True
            review.append(f"{slug}[{','.join(sorted(conflicts))}]")
            print(f"   conflict: {ns}/{slug} live copies disagree on "
                  + "; ".join(f"{k}={vals}" for k, vals in sorted(conflicts.items())), file=sys.stdout)
        losers = [p for p, _fm, _b in live if p.resolve() != dest.resolve()] + redundant
        for p in losers:
            if key_of(p) != canonical:
                move_map[key_of(p)] = canonical
        if apply:
            merged_fm = {k: v for k, v in merged_fm.items() if v not in (None, "", [], {})}
            head = yaml.safe_dump(merged_fm, sort_keys=False, allow_unicode=True).rstrip()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"---\n{head}\n---\n\n{winner_body}\n", encoding="utf-8")
            for p in losers:
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
