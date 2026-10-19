#!/usr/bin/env python3
"""Plan/apply repair of whitespace or run-on page basenames (#240), and of content pages
sitting under a reserved or dot-leading basename (okengine#997).

Whitespace/run-on entity slugs use the page's human title/name as the replacement key, fall back
to the old stem, and delegate bounded/collision-safe normalization to id_lib.

Reserved/dot basenames (`entities/h/e/health.md`, `concepts/_/.net-x.md`) are found by
`okf_migrate.reserved_name_pages`, renamed to `okf_migrate.mintable_slug` — the same stand-in the
minting guard gives a new page, so the importer that minted it converges on the renamed page —
and re-seated where the partition strategy wants it (`desired_key`). Path-qualified wikilinks and
bare path references are rewritten with okf_migrate's whole-token rewriters (the reshelve drain's
own). A reserved-name TOMBSTONE is not renamed: its references go to its `superseded_by` target
and it is dropped; a pointer that does not resolve to a live page is reported instead. An
unqualified `[[.dot-stem]]` link is reported for review, never rewritten; an unqualified
`[[health]]`/`[[index]]` is not reported, because it resolves to the domain root's own structural
file, not to the moved page. The page's `id` is never changed.

Dry-run is the default; ``--apply`` renames pages and rewrites references.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import id_lib  # noqa: E402
import okf_migrate  # noqa: E402
import engine_package  # noqa: E402  — `okengine` importable when this script runs by path
engine_package.ensure()
from okengine import corpus_transaction  # noqa: E402

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


def _read(page: Path) -> str | None:
    """A page's text exactly as stored (`newline=""` keeps `\r\n`, so a rewrite touches only the
    references it replaces), or None when it cannot be read: a page can vanish mid-scan while a
    mover lane relocates it, and every caller skips it then."""
    try:
        with open(page, encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()
    except OSError:
        return None


def _write(page: Path, text: str) -> None:
    with open(page, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def plan_slugs(vault: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """The whitespace/run-on slug moves (old key, new key), and the collisions refused. Every move
    is planned before any page moves, so a target another move claims is a collision too."""
    wiki = vault / "wiki"
    moves, errors = [], []
    claimed: set[Path] = set()
    for old in sorted(wiki.rglob("*.md")):
        # Every candidate comes from wiki.rglob(), so it is necessarily relative to wiki.
        rel_parts = old.relative_to(wiki).parts
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
        if new in claimed or new.exists():  # the same file already `continue`d above
            errors.append(f"{old.relative_to(wiki)} -> collision at {new.relative_to(wiki)}")
            continue
        claimed.add(new)
        moves.append((old.relative_to(wiki).as_posix()[:-3], new.relative_to(wiki).as_posix()[:-3]))
    return moves, errors


def repair(vault: Path, apply: bool = False,
           rewritten: list | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """Plan (and with `apply`, perform) the whitespace/run-on slug repair (#240).

    The references are rewritten exactly as the reserved-basename repair rewrites them: one pass
    over every page with okf_migrate's whole-token rewriters, after every move. So a slug that is
    a prefix of another (`foo bar` / `foo bar baz`) never corrupts the longer one's links, a page's
    link to ITSELF follows its rename, and a page two moves both touch is one rewritten file.
    `rewritten`, when given, collects (file, references) keyed by each file's ORIGINAL path, so a
    dry run and an apply report the same files. An apply runs in one corpus transaction."""
    moves, errors = plan_slugs(vault)
    if not moves:
        return moves, errors
    wiki = vault / "wiki"
    if apply:
        with corpus_transaction.mutation(vault, writer="repair_malformed_slugs",
                                         operation="whitespace-slug-rename", tracking="touched"):
            for old, new in moves:
                src, dst = wiki / f"{old}.md", wiki / f"{new}.md"
                corpus_transaction.touch(src)
                corpus_transaction.touch(dst)
                src.rename(dst)
            found, _unqualified, rewrite_errors = rewrite_references(vault, moves, apply=True)
    else:
        found, _unqualified, rewrite_errors = rewrite_references(vault, moves)
    original = {f"{new}.md": f"{old}.md" for old, new in moves}
    if rewritten is not None:
        rewritten += [(original.get(rel, rel), count) for rel, count in found]
    return moves, errors + rewrite_errors


def _namespace(wiki: Path, key: str) -> str:
    """The qualified namespace of wiki-relative `key`: its first segment, or `<sub-domain>/<ns>`
    under a walk-up sub-domain (a directory holding its own schema.yaml)."""
    parts = key.split("/")
    i = 0
    while i < len(parts) - 2 and (wiki.joinpath(*parts[:i + 1]) / "schema.yaml").is_file():
        i += 1
    return "/".join(parts[:i + 1])


def _superseded_target(vault: Path, fm: dict) -> str | None:
    """The live page a tombstone's `superseded_by` designates (wiki-relative key), or None when the
    pointer is empty or does not resolve to a live page by path."""
    ref = str(fm.get("superseded_by") or "").strip().strip("[]").split("|")[0].split("#")[0]
    ref = ref.strip().removeprefix("wiki/").removesuffix(".md")
    if not ref or not (vault / "wiki" / f"{ref}.md").is_file():
        return None
    return None if okf_migrate.is_tombstoned(okf_migrate._fm_at(vault, ref)) else ref


def plan_reserved(vault: Path):
    """Plan the reserved/dot basename repair. Returns (moves, retirements, review, errors).

    A LIVE page is renamed: (old key, new key). A TOMBSTONE is never renamed — a stand-in name
    for a retired page is a redirect nobody links to. Its inbound references go straight to its
    `superseded_by` target and the tombstone is dropped: (old key, target key). A tombstone whose
    pointer does not resolve to a live page is reported, not touched. A live page in a SCAFFOLD
    shape (`_template.md`, `INDEX-x.md`, `index-p02.md`) is listed for review, never renamed: a
    pack's crons may read it by that exact name."""
    wiki = vault / "wiki"
    moves: list[tuple[str, str]] = []
    retirements: list[tuple[str, str]] = []
    review: list[str] = []
    errors: list[str] = []
    scaffold = okf_migrate._reserved_contract().is_engine_generated
    claimed: set[str] = set()
    for rel, tombstoned in okf_migrate.reserved_name_pages(vault):
        old = rel[:-3]
        fm = okf_migrate._fm_at(vault, old)
        if tombstoned:
            target = _superseded_target(vault, fm)
            if target is None:
                errors.append(f"{rel}: tombstone superseded_by {fm.get('superseded_by')!r} does "
                              f"not resolve to a live page; fix the pointer, then re-run")
            else:
                retirements.append((old, target))
            continue
        if scaffold(PurePosixPath(rel).name):
            review.append(rel)
            continue
        ns = _namespace(wiki, old)
        slug = PurePosixPath(old).name
        try:
            new_slug = okf_migrate.mintable_slug(vault, ns, slug, fm)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        new = (okf_migrate.desired_key(vault, ns, new_slug, fm)
               if okf_migrate.is_partitioned(vault, ns) else str(PurePosixPath(old).with_name(new_slug)))
        if new in claimed or (wiki / f"{new}.md").exists():
            errors.append(f"{rel} -> collision at {new}.md")
            continue
        claimed.add(new)
        moves.append((old, new))
    return moves, retirements, review, errors


def rewrite_references(vault: Path, moves: list[tuple[str, str]], apply: bool = False,
                       skip: frozenset = frozenset()):
    """Rewrite every path-qualified reference to the moved pages (`moves` is non-empty: old key ->
    new key). Pages whose key is in `skip` (tombstones about to be dropped) are not rewritten.

    Returns (per-file rewrite counts, unqualified-link findings, errors). Pages are read whatever
    `apply` says, so a dry run reports exactly what an apply would touch."""
    wiki = vault / "wiki"
    move_map = dict(moves)
    by_ns: dict[str, dict[str, str]] = {}
    for old, new in moves:
        by_ns.setdefault(_namespace(wiki, old), {})[old] = new
    rewriters = [okf_migrate.make_rewriter(m, ns) for ns, m in sorted(by_ns.items())]
    path_pat, path_repl = okf_migrate.make_path_rewriter(move_map)
    # An unqualified `[[stem]]` cannot be rewritten safely. For a RESERVED stem it is not even a
    # link to the moved page — `[[index]]`/`[[health]]` resolve to the domain root's own structural
    # file — so only dot-leading stems, which nothing else can own, are reported for review.
    stems = sorted({PurePosixPath(old).name for old in move_map
                    if PurePosixPath(old).name.startswith(".")}, key=len, reverse=True)
    bare = (re.compile(r"\[\[(" + "|".join(re.escape(s) for s in stems) + r")(?=[\]#|])")
            if stems else None)
    rewritten: list[tuple[str, int]] = []
    unqualified: list[str] = []
    errors: list[str] = []
    for page in sorted(wiki.rglob("*.md")):  # rglob-ok: whole-vault link rewrite, like the reshelve drain
        rel = page.relative_to(wiki).as_posix()
        if rel[:-3] in skip:
            continue
        text = _read(page)
        if text is None:
            continue
        new_text, total = text, 0
        for pat, repl in [*rewriters, (path_pat, path_repl)]:
            # count only references that CHANGE: the wikilink pattern also matches (and returns
            # unchanged) every other page in the namespace, e.g. a longer `health-isac` slug
            changed = [0]

            def counted(mt, repl=repl, changed=changed):
                out = repl(mt)
                changed[0] += out != mt.group(0)
                return out

            new_text = pat.sub(counted, new_text)
            total += changed[0]
        if bare is not None:
            unqualified += [f"{rel}: [[{mt.group(1)}]]" for mt in bare.finditer(new_text)]
        if not total:
            continue
        if apply:
            corpus_transaction.touch(page)
            try:
                _write(page, new_text)
            except OSError:
                errors.append(f"could not rewrite reference in {rel}")
                continue  # an error, not a rewrite: the report must not count it
        rewritten.append((rel, total))
    return rewritten, unqualified, errors


def repair_reserved(vault: Path, apply: bool = False):
    """Plan (and with `apply`, perform) the reserved/dot basename repair. Returns
    (moves, retirements, review, rewritten files, unqualified links, errors).

    An apply runs inside one corpus transaction (the fence every canonical writer, the MCP write
    path included, takes), journaling exactly the pages it renames, rewrites and drops, so it
    serializes with a live gateway's writes and leaves a mutation receipt."""
    moves, retirements, review, errors = plan_reserved(vault)
    if not (moves or retirements):
        return moves, retirements, review, [], [], errors
    retired = frozenset(old for old, _ in retirements)
    if not apply:
        rewritten, unqualified, rewrite_errors = rewrite_references(
            vault, moves + retirements, skip=retired)
        return moves, retirements, review, rewritten, unqualified, errors + rewrite_errors
    with corpus_transaction.mutation(vault, writer="repair_malformed_slugs",
                                     operation="reserved-basename-rename", tracking="touched"):
        for old, new in moves:
            src, dst = vault / "wiki" / f"{old}.md", vault / "wiki" / f"{new}.md"
            corpus_transaction.touch(src)
            corpus_transaction.touch(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        rewritten, unqualified, rewrite_errors = rewrite_references(
            vault, moves + retirements, apply=True, skip=retired)
        for old in sorted(retired):
            tomb = vault / "wiki" / f"{old}.md"
            corpus_transaction.touch(tomb)
            tomb.unlink()
    return moves, retirements, review, rewritten, unqualified, errors + rewrite_errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, default=Path("/opt/vault"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    slug_rewritten: list[tuple[str, int]] = []
    moves, errors = repair(args.vault, args.apply, rewritten=slug_rewritten)
    verb = "MOVE" if args.apply else "WOULD MOVE"
    for old, new in moves:
        print(f"{verb} {old} -> {new}")
    for rel, count in slug_rewritten:
        print(f"{'REWROTE' if args.apply else 'WOULD REWRITE'} {rel}: {count} reference(s)")
    r_moves, retired, review, rewritten, unqualified, r_errors = repair_reserved(
        args.vault, args.apply)
    for old, new in r_moves:
        print(f"{verb} {old} -> {new} (reserved basename)")
    for old, target in retired:
        print(f"{'DROP' if args.apply else 'WOULD DROP'} tombstone {old} "
              f"(references -> its superseded_by {target})")
    for rel in review:
        print(f"REVIEW {rel} — a scaffold-shaped name (`_`/INDEX-); not renamed automatically")
    for rel, count in rewritten:
        print(f"{'REWROTE' if args.apply else 'WOULD REWRITE'} {rel}: {count} reference(s)")
    for finding in unqualified:
        print(f"UNQUALIFIED {finding} — not rewritten; review by hand")
    errors += r_errors
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    print(f"malformed-slug-repair: {len(moves)} move(s), {len(r_moves)} reserved-basename "
          f"move(s), {len(retired)} tombstone(s) dropped, {len(review)} for review, {len({rel for rel, _n in slug_rewritten + rewritten})} file(s) with rewritten references, "
          f"{len(unqualified)} unqualified link(s), {len(errors)} collision/error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
