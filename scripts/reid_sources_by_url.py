#!/usr/bin/env python3
"""Re-identify legacy source pages by their URL (okengine#515 item 4).

A source page's identity is its `url`, not its title. The write path now mints
`sources:url-<sha256(url)[:20]>` for new writes, but the existing corpus carries
title-derived slugs — 44,480 of them against 105 strong ids on one deployment. Until those
are re-identified they keep colliding: the same document arriving on a second path slugs the
same and the write is refused (or, before the write-path fix, a genuinely different document
was refused for sharing a title).

This rewrites only the `id:` line, and only when it is unambiguous.

WHAT IT WILL NOT DO
  * merge or delete anything — dedup is converge's job, not a re-id pass;
  * touch a page with no `url` (identity undecidable — guessing would fuse records);
  * touch a page whose id is already the URL-derived form (idempotent);
  * re-id a page when ANOTHER page already holds the target id. That is a genuine duplicate
    pair and is REPORTED for the dedup pass instead. Rewriting both to one id would create
    two live pages claiming one identity — an id-index collision, which is the bug rather
    than the fix.

REFERENCE SAFETY: source ids can be referenced by live frontmatter. The migration discovers
those pointers from the exact old->new re-id map and rewrites them in the same coordinated
apply as the source ids. Bodies and append-only `log.md` / `_review-queue.md` history are not
rewritten. `--check-references` reports the affected pointers during a dry run; apply stages
every changed file before replacing any of them.

Dry-run is the default. `--apply` writes.

Usage:
  python3 scripts/reid_sources_by_url.py --vault /path/to/pack
  python3 scripts/reid_sources_by_url.py --vault /path/to/pack --apply
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

import yaml

_FM = re.compile(r"\A---\n(.*?\n)---\n", re.S)
_ID_LINE = re.compile(r"^id:[ \t].*$", re.M)
_URL_SOURCE_ID = re.compile(r"^sources:url-[0-9a-f]{20}$")
_SOURCE_ID_TOKEN = re.compile(r"sources:[A-Za-z0-9._:-]{4,}")


_REAL_URL = re.compile(r"^https?://[^\s/]+", re.I)


def norm_url(value: object) -> str:
    """Whitespace and ONE trailing slash. No case folding — a URL path is
    case-significant on many hosts, and an over-eager normalizer would fuse genuinely
    different pages. Must match write_server._norm_url."""
    text = str(value or "").strip()
    if not text or not _REAL_URL.match(text):
        return ""
    return text[:-1] if text.endswith("/") and len(text) > 1 else text


def source_url_id(url: object) -> str:
    """Must stay byte-identical to write_server.source_url_id, or the mover and the write
    path would disagree about the same document's identity."""
    text = norm_url(url)
    if not text:
        return ""
    return f"sources:url-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]}"


def read_fm(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    match = _FM.match(text)
    if not match:
        return {}, text
    try:
        fm = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    return (fm if isinstance(fm, dict) else {}), text


def plan(vault: Path) -> dict:
    """Decide, without writing. Returns the plan plus every reason a page was skipped, so a
    dry-run explains itself rather than just printing a number."""
    sources = vault / "wiki" / "sources"
    rewrite: list[tuple[Path, str, str]] = []          # (path, old_id, new_id)
    dup_pairs: list[tuple[str, str, str]] = []         # (target_id, holder_rel, other_rel)
    skipped = {"already_strong": 0, "no_url": 0, "not_source": 0, "no_frontmatter": 0,
               "tombstoned": 0}
    claimed: dict[str, Path] = {}                      # target id -> first page claiming it

    if not sources.is_dir():
        return {"rewrite": rewrite, "dup_pairs": dup_pairs, "skipped": skipped, "total": 0}

    pages = sorted(p for p in sources.rglob("*.md") if p.is_file())
    for path in pages:
        fm, _text = read_fm(path)
        if not fm:
            skipped["no_frontmatter"] += 1
            continue
        if str(fm.get("type") or "").strip() != "source":
            skipped["not_source"] += 1
            continue
        if str(fm.get("status") or "").strip().lower() == "tombstoned":
            # A retired duplicate keeps its `url`, so without this it still maps to the
            # survivor's id and is reported as an unresolved duplicate pair forever — after
            # the dedup had already resolved it. `dedup_sources_by_url` skips tombstoned
            # pages for the same reason; the two tools must agree on what is live
            # (okengine#516).
            skipped["tombstoned"] += 1
            continue
        current = str(fm.get("id") or "").strip()
        if _URL_SOURCE_ID.match(current):
            skipped["already_strong"] += 1
            claimed.setdefault(current, path)
            continue
        target = source_url_id(fm.get("url"))
        if not target:
            skipped["no_url"] += 1
            continue
        prior = claimed.get(target)
        if prior is not None:
            # Two live pages, one URL. A genuine duplicate: report for dedup, never re-id
            # both to the same id (that manufactures an index collision).
            dup_pairs.append((target, _rel(vault, prior), _rel(vault, path)))
            continue
        claimed[target] = path
        rewrite.append((path, current, target))
    return {"rewrite": rewrite, "dup_pairs": dup_pairs, "skipped": skipped,
            "total": len(pages)}


def _rel(vault: Path, path: Path) -> str:
    try:
        return path.relative_to(vault / "wiki").as_posix()
    except ValueError:
        return str(path)


def structured_reference_rewrites(
        vault: Path, id_map: dict[str, str]) -> list[tuple[Path, dict[str, str]]]:
    """Return frontmatter files and old->new source-id pointers they contain.

    Logs are excluded on purpose: `_review-queue.md` and `log.md` are append-only records of
    past events, and rewriting history to match a new id would falsify them. A reference
    inside frontmatter is a live pointer and must move with its target.
    """
    if not id_map:
        return []
    hits: list[tuple[Path, dict[str, str]]] = []
    for path in (vault / "wiki").rglob("*.md"):
        if path.name in ("log.md", "_review-queue.md"):
            continue
        fm, text = read_fm(path)
        if not fm:
            continue
        match = _FM.match(text)
        if not match:
            continue
        # Exclude the page's own identity line; apply_rewrites owns that edit. Searching the
        # remaining raw YAML preserves nested/list/string references without reserializing FM.
        frontmatter = _ID_LINE.sub("", match.group(1), count=0)
        replacements = {
            token: id_map[token]
            for token in set(_SOURCE_ID_TOKEN.findall(frontmatter))
            if token in id_map
        }
        if replacements:
            hits.append((path, replacements))
    return hits


def _replace_id(text: str, new: str) -> str | None:
    match = _FM.match(text)
    if not match:
        return None
    head, rest = text[:match.end()], text[match.end():]
    if _ID_LINE.search(head):
        head = _ID_LINE.sub(f"id: {new}", head, count=1)
    else:
        head = head.replace("---\n", f"---\nid: {new}\n", 1)
    return head + rest


def _replace_frontmatter_refs(text: str, replacements: dict[str, str]) -> str | None:
    match = _FM.match(text)
    if not match:
        return None
    head, rest = text[:match.end()], text[match.end():]

    def replace_line(line: str) -> str:
        if line.startswith("id:"):
            return line
        return _SOURCE_ID_TOKEN.sub(lambda m: replacements.get(m.group(0), m.group(0)), line)

    return "".join(replace_line(line) for line in head.splitlines(keepends=True)) + rest


def apply_rewrites(
        rewrite: list[tuple[Path, str, str]],
        reference_rewrites: list[tuple[Path, dict[str, str]]] | None = None) -> int:
    """Stage and apply source-id and structured-reference changes as one migration.

    Every output is rendered and written to a sibling temporary file before the first live
    path is replaced. This prevents a read/render/write failure from leaving half a migration.
    """
    desired: dict[Path, str] = {}
    originals: dict[Path, str] = {}
    planned_ids: dict[Path, tuple[str, str]] = {}
    for path, old, new in rewrite:
        prior = planned_ids.get(path)
        if prior is not None and prior != (old, new):
            # A contradictory plan cannot be applied deterministically. Refuse it before
            # reading or staging anything rather than letting list order choose an identity.
            return 0
        planned_ids[path] = (old, new)

    for path, (old, new) in planned_ids.items():
        # Source plans are deduplicated above and this is the first pass that can populate
        # ``desired``.  Read the snapshot directly; consulting ``desired`` here created an
        # unreachable false branch that obscured the migration's real preflight paths.
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return 0
        originals[path] = text
        fm_match = _FM.match(text)
        if fm_match is None:
            return 0
        try:
            fm = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError:
            return 0
        if not isinstance(fm, dict):
            return 0
        actual = str(fm.get("id") or "").strip()
        if actual != old:
            # The migration plan is a snapshot. If the identity changed after planning,
            # applying the stale target could overwrite a concurrent correction.
            return 0
        updated = _replace_id(text, new)
        if updated is None:
            return 0
        desired[path] = updated
    for path, replacements in reference_rewrites or []:
        text = desired.get(path)
        if text is None:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return 0
            originals[path] = text
        fm_match = _FM.match(text)
        if fm_match is None:
            return 0
        try:
            fm = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError:
            return 0
        if not isinstance(fm, dict) or not replacements:
            return 0
        frontmatter = _ID_LINE.sub("", fm_match.group(1), count=0)
        present = set(_SOURCE_ID_TOKEN.findall(frontmatter))
        if not set(replacements).issubset(present):
            # Reference plans are snapshots too. Refuse a stale or partially matching plan;
            # otherwise the file would be counted as migrated while a pointer remained old.
            return 0
        updated = _replace_frontmatter_refs(text, replacements)
        if updated is None:
            return 0
        desired[path] = updated

    staged: dict[Path, Path] = {}
    try:
        for path, text in desired.items():
            tmp = path.with_suffix(path.suffix + ".reid-tmp")
            tmp.write_text(text, encoding="utf-8")
            staged[path] = tmp
    except OSError:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)
        return 0

    done = 0
    try:
        for path, tmp in staged.items():
            tmp.replace(path)
            done += 1
    except OSError:
        # Best-effort rollback from the in-memory originals. A subsequent dry run remains
        # authoritative if the host itself becomes unwritable during rollback.
        for path, text in originals.items():
            rollback = path.with_suffix(path.suffix + ".reid-rollback-tmp")
            rollback.write_text(text, encoding="utf-8")
            rollback.replace(path)
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)
        return 0
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True, help="pack/vault root (contains wiki/)")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--limit", type=int, default=0, help="cap rewrites this run (0 = all)")
    ap.add_argument("--check-references", action="store_true",
                    help="report structured references that will move with changed ids")
    args = ap.parse_args(argv)

    vault = Path(args.vault)
    if not (vault / "wiki").is_dir():
        print(f"reid: no wiki/ under {vault}", file=sys.stderr)
        return 2

    result = plan(vault)
    all_rewrites = result["rewrite"]
    rewrite = all_rewrites
    if args.limit:
        rewrite = rewrite[:args.limit]

    print(f"=== reid-sources-by-url ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    print(f"  vault            : {vault}")
    print(f"  source pages     : {result['total']}")
    print(f"  to re-identify   : {len(rewrite)}"
          + (f" (of {len(result['rewrite'])}, --limit)" if args.limit else ""))
    print(f"  already strong   : {result['skipped']['already_strong']}")
    print(f"  no url (skipped) : {result['skipped']['no_url']}")
    print(f"  not type source  : {result['skipped']['not_source']}")
    print(f"  no frontmatter   : {result['skipped']['no_frontmatter']}")
    print(f"  tombstoned       : {result['skipped']['tombstoned']} (already retired by the dedup)")
    print(f"  duplicate pairs  : {len(result['dup_pairs'])}  (reported, NOT re-identified)")
    for target, a, b in result["dup_pairs"][:10]:
        print(f"      {target}\n        {a}\n        {b}")
    if len(result["dup_pairs"]) > 10:
        print(f"      … and {len(result['dup_pairs']) - 10} more")
    for path, old, new in rewrite[:8]:
        print(f"   {_rel(vault, path)}\n      {old or '(no id)'}  ->  {new}")
    if len(rewrite) > 8:
        print(f"   … and {len(rewrite) - 8} more")

    targets_by_old: dict[str, set[str]] = {}
    # Ambiguity is a property of the full corpus plan, not of this batch. Computing it from
    # the --limit slice can make a duplicate legacy id look unique and redirect a live
    # reference to whichever identity happened to sort into the first batch.
    for _path, old, new in all_rewrites:
        if old:
            targets_by_old.setdefault(old, set()).add(new)
    ambiguous = {old: targets for old, targets in targets_by_old.items() if len(targets) > 1}
    selected_old_ids = {old for _path, old, _new in rewrite if old}
    id_map = {old: next(iter(targets)) for old, targets in targets_by_old.items()
              if len(targets) == 1 and old in selected_old_ids}
    scanned_references = structured_reference_rewrites(
        vault, {**id_map, **{old: old for old in ambiguous}})
    ambiguous_references: list[tuple[Path, dict[str, str]]] = []
    reference_rewrites: list[tuple[Path, dict[str, str]]] = []
    for path, replacements in scanned_references:
        unsafe = {old: new for old, new in replacements.items() if old in ambiguous}
        safe = {old: new for old, new in replacements.items() if old not in ambiguous}
        if unsafe:
            ambiguous_references.append((path, unsafe))
        if safe:
            reference_rewrites.append((path, safe))
    print(f"  ambiguous old ids: {len(ambiguous)}")
    if ambiguous_references:
        print("reid: REFUSING — a structured reference uses a legacy id that maps to "
              "multiple URL identities", file=sys.stderr)
        for path, replacements in ambiguous_references[:10]:
            print(f"      {_rel(vault, path)}: {', '.join(sorted(replacements))}",
                  file=sys.stderr)
        return 1
    if args.check_references:
        ref_count = sum(len(replacements) for _path, replacements in reference_rewrites)
        print(f"  structured refs  : {ref_count} across {len(reference_rewrites)} page(s)")
        for path, replacements in reference_rewrites[:10]:
            print(f"      {_rel(vault, path)}: {', '.join(sorted(replacements))}")

    if not args.apply:
        print("  (dry run — nothing written; pass --apply)")
        return 0
    done = apply_rewrites(rewrite, reference_rewrites)
    expected = len({path for path, _old, _new in rewrite} |
                   {path for path, _replacements in reference_rewrites})
    if done != expected:
        print(f"reid: REFUSING — staged migration applied {done} of {expected} files", file=sys.stderr)
        return 1
    print(f"  files changed    : {done} ({len(rewrite)} source ids, "
          f"{len(reference_rewrites)} reference pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
