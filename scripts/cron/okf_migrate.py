#!/usr/bin/env python3
"""OKF hierarchical migration — bulk, link-preserving namespace reorganization.

Replicates `iwe rename`'s semantics (move file + rewrite every [[reference]]) but
as a SINGLE O(n) pass over the corpus, because per-file `iwe rename` rebuilds the
graph each call (~13s on 40k = infeasible at scale). `iwe stats` is the validator
(run before/after: reference count must be unchanged), not the mover.

Target layout is CONFIG-DRIVEN: the governing schema.yaml's `partitioning` block
(inherited from base-schema — entities: by-letter `entities/{L}/{slug}`, sources:
by-date). The mover normalizes BOTH flat pages and pages nested in a
NON-CANONICAL layout (okengine#165: a vault imported through the old by-type
Phase-1 target carries `entities/{type}/[{L}/]{slug}` trees — those re-nest to
the canonical key). Only canonical-typed pages move; ambiguous/typeless ones
stay put for the classify drains. Oversized letter buckets are reshard_oversized's
job afterwards, not this mover's.

Collision guard (okengine#165): a page whose canonical destination ALREADY holds a
different file does NOT move — it is reported as a duplicate-slug collision for
the dedup pass (true-dup merge or slug disambiguation) and the migration stays
safe to run in any order relative to dedup.

Link forms handled: [[entities/slug]], [[entities/slug|alias]],
[[entities/slug#heading]], [[entities/slug#heading|alias]] — the key segment is
rewritten, alias/heading preserved.

Usage:
  okf_migrate.py --namespace entities [--apply] [--root /opt/vault]
Default is dry-run. Writes are best-effort per file; PermissionError files are
reported (handle non-owned files in a second pass as their owner).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.S)

_SCHEMA_CACHE: dict[str, dict] = {}


def _governing_schema(root: Path, namespace: str) -> dict:
    """The domain-pack schema.yaml governing wiki/<namespace>/ — found by walking UP
    (a sub-domain's own, e.g. wiki/<subdomain>/schema.yaml, else the vault root's). Cached."""
    cur = root / "wiki" / namespace
    while True:
        sp = cur / "schema.yaml"
        if sp.is_file():
            k = str(sp)
            if k not in _SCHEMA_CACHE:
                try:
                    _SCHEMA_CACHE[k] = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
                except Exception:
                    _SCHEMA_CACHE[k] = {}
            return _SCHEMA_CACHE[k]
        if cur == root or cur.parent == cur:
            return {}
        cur = cur.parent


def _partition_cfg(schema: dict, namespace: str) -> dict:
    # config keys are leaf namespace names (entities/sources/...), even for sub-domains.
    leaf = namespace.split("/")[-1]
    return ((schema.get("partitioning") or {}).get("namespaces") or {}).get(
        leaf, {"strategy": "flat"})


def _letter(slug: str) -> str:
    c = slug[0].lower() if slug else "_"
    return c if c.isalpha() else ("0-9" if c.isdigit() else "_")


def _ym(slug: str, fm: dict, date_field: str = "published") -> tuple[str, str] | None:
    """(year, month) from the configured date field (preferred) or filename date.
    None if no real date — those pages stay flat (no fabrication)."""
    pub = str((fm or {}).get(date_field) or "")
    for cand, pat in ((pub, r"\D*(\d{4})-(\d{2})"),
                      (slug, r"^(\d{4})-(\d{2})"),
                      (slug, r"(?:^|-)(\d{4})-(\d{2})(?:-|$)")):
        m = re.search(pat, cand)
        if m:
            y, mo = m.group(1), m.group(2)
            if "2000" <= y <= "2099" and "01" <= mo <= "12":
                return y, mo
    return None


def _new_key(namespace: str, slug: str, fm: dict, pcfg: dict, canonical: set) -> str | None:
    """Config-driven target key for a page, per the namespace's partition strategy."""
    strat = pcfg.get("strategy", "flat")
    if strat == "flat":
        return None
    if strat == "by-letter":
        return f"{namespace}/{_letter(slug)}/{slug}"
    if strat == "by-date":
        ym = _ym(slug, fm, pcfg.get("date_field", "published"))
        return f"{namespace}/{ym[0]}/{ym[1]}/{slug}" if ym else None
    if strat == "by-type":
        t = str((fm or {}).get("type") or "").strip()
        if t not in canonical:
            return None  # unknown/typeless — stays flat for classification
        if t in set(pcfg.get("sharded_types") or []):
            return f"{namespace}/{t}/{_letter(slug)}/{slug}"
        return f"{namespace}/{t}/{slug}"
    # An UNRECOGNIZED strategy (a typo like `by_date`, or an invented `by-year`) silently degraded to
    # flat here while every 'is this partitioned?' matcher tests `strategy != "flat"` and treats it as
    # partitioned — so the drain reshelved by one rule and the flat fallback wrote by another, forking
    # canonicals (invariant-audit #25). Fail LOUD; framework_validate gates it before deploy.
    raise ValueError(f"unknown partition strategy {strat!r} for namespace {namespace!r} "
                     f"(valid: flat, by-letter, by-date, by-type)")


# ── public helpers for no_agent importers (okengine#54) ──────────────────────
# A no_agent importer must write each page where the reshelve drain would file it, and merge
# against the page WHEREVER it currently sits — not probe only the flat root and re-create a
# stale duplicate every cycle (the KEV/NVD double-count). Both share okf_migrate as the SINGLE
# source of the path logic, so importer and drain can never disagree and re-open the loop.

def is_partitioned(root: Path, namespace: str) -> bool:
    """Whether <namespace> declares a non-flat partition strategy in its governing schema — so a
    shared writer can route partitioned pages through canonical_key while leaving flat namespaces'
    paths (which may legitimately carry sub-segments) untouched."""
    return _partition_cfg(_governing_schema(root, namespace), namespace).get(
        "strategy", "flat") != "flat"


def canonical_key(root: Path, namespace: str, slug: str, fm: dict | None = None) -> str:
    """Wiki-relative key (no .md) where <namespace>/<slug> canonically lives, per the governing
    schema.yaml `partitioning`. Falls back to the flat `namespace/slug` exactly where reshelve
    leaves an un-bucketable page (flat strategy, or by-date with no usable date_field) — so the
    importer's destination always agrees with the drain."""
    schema = _governing_schema(root, namespace)
    pcfg = _partition_cfg(schema, namespace)
    canonical = set((schema.get("types") or {}).keys())
    return _new_key(namespace, slug, fm or {}, pcfg, canonical) or f"{namespace}/{slug}"


def find_page(root: Path, namespace: str, slug: str) -> Path | None:
    """The existing page file for <namespace>/<slug>, wherever it currently sits under the
    namespace — the canonical shard OR a stale flat/non-canonical location — so an importer can
    merge in place instead of duplicating (and so NVD enrichment can actually FIND a sharded KEV
    page). None if absent. When more than one copy exists (the okengine#54 bug), the DEEPEST path
    wins deterministically: the sharded canonical outranks a flat root copy, so repeated runs
    converge on the canonical seat rather than ping-ponging."""
    base = root / "wiki" / namespace
    if not base.is_dir():
        return None
    hits = [p for p in base.rglob(f"{slug}.md") if p.stem == slug]
    if not hits:
        return None
    return sorted(hits, key=lambda p: (-len(p.parts), p.as_posix()))[0]


def _day(slug: str, fm: dict) -> str:
    """Day-of-month reshard segment (reshard_by: day) — published date, else filename date, else 00."""
    m = re.search(r"\d{4}-\d{2}-(\d{2})", str((fm or {}).get("published") or "")) \
        or re.match(r"\d{4}-\d{2}-(\d{2})", slug)
    return m.group(1) if m else "00"


def _second(slug: str, fm: dict | None = None) -> str:
    """Second-letter reshard segment (reshard_by: second-letter). `fm` unused — uniform keyfn sig."""
    s = slug.lower()
    return s[1] if len(s) > 1 and s[1].isalnum() else "_"


_RESHARD_SEG = {"day": _day, "second-letter": _second}


def reshard_seg(reshard_by: str, slug: str, fm: dict) -> "str | None":
    """The segment reshard_oversized inserts between the canonical bucket and the slug when it splits
    an oversized leaf. THE single source of the reshard-key logic — build_map and reshard_oversized
    both call it, so a valid split can't be silently reverted by the drain (okengine#54 spirit).
    None for a namespace with no/unknown reshard_by."""
    fn = _RESHARD_SEG.get(reshard_by)
    return fn(slug, fm) if fn else None


def write_key(root: Path, namespace: str, slug: str, fm: dict | None = None) -> str:
    """Wiki-relative key (no .md) a direct writer should use.

    Existing pages are updated in place so an importer never forks a page merely because a
    reshard/migration has not run yet. New pages include the configured ``reshard_by`` segment
    immediately. That distinction matters once an oversized namespace has been split: continuing
    to mint pages in the base bucket makes the daily reshard job and importer recreate opposite
    sides of a partition collision forever (okengine#243).

    ``canonical_key`` remains the base partition key used by the migration/oversize detector;
    direct writers and collision cleanup should use this helper.
    """
    root = Path(root)
    fm = fm or {}
    base_key = canonical_key(root, namespace, slug, fm)
    pcfg = _partition_cfg(_governing_schema(root, namespace), namespace)
    seg = reshard_seg(pcfg.get("reshard_by"), slug, fm)
    desired = f"{base_key.rsplit('/', 1)[0]}/{seg}/{slug}" if seg else base_key
    desired_path = root / "wiki" / f"{desired}.md"
    if desired_path.is_file():
        return desired

    existing = find_page(root, namespace, slug)
    if existing is not None:
        return existing.relative_to(root / "wiki").as_posix()[:-3]
    return desired


def _is_reshard_bucket(cur: str, new: str, pcfg: dict, slug: str, fm: dict) -> bool:
    """True when `cur` is the canonical key `new` split ONE level deeper into a VALID reshard
    sub-bucket (`<canonical-prefix>/<reshard-seg>/<slug>`), so build_map leaves it in place instead
    of reverting a legitimate reshard (which caused reshard@00:45 ↔ reshelve@02:35 churn forever)."""
    seg = reshard_seg(pcfg.get("reshard_by"), slug, fm)
    if seg is None:
        return False
    cp, np = cur.split("/"), new.split("/")
    return (len(cp) == len(np) + 1 and cp[-1] == np[-1]
            and cp[:-2] == np[:-1] and cp[-2] == seg)


def build_map(root: Path, namespace: str, only_types: set[str] | None = None,
              only_year: str | None = None) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(old-key -> new-key, collisions) for EVERY page under the namespace whose current
    key differs from its canonical key per the governing schema.yaml `partitioning`
    config (domain-agnostic). Covers flat pages AND pages nested in a non-canonical
    layout (okengine#165). Collisions — a destination already occupied by a different
    file, or two sources mapping to one destination — are excluded from the map and
    returned for the dedup pass. only_types / only_year: staged-pilot filters."""
    schema = _governing_schema(root, namespace)
    pcfg = _partition_cfg(schema, namespace)
    canonical = set((schema.get("types") or {}).keys())
    base = root / "wiki" / namespace
    m: dict[str, str] = {}
    collisions: list[tuple[str, str]] = []
    for p in sorted(base.rglob("*.md")):
        slug = p.stem
        if slug.startswith("_") or slug in ("index", "INDEX", "log", "README") \
                or slug.startswith("INDEX-p"):
            continue
        try:
            fmm = _FM_RE.match(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if not fmm:
            continue
        try:
            fm = yaml.safe_load(fmm.group(1))
        except Exception:
            continue
        if not isinstance(fm, dict):
            continue
        if only_types is not None and str(fm.get("type") or "").strip() not in only_types:
            continue
        cur = p.relative_to(root / "wiki").as_posix()[:-3]
        new = _new_key(namespace, slug, fm, pcfg, canonical)
        if new and only_year and new.startswith(f"{namespace}/") \
                and not new.startswith(f"{namespace}/{only_year}/"):
            new = None
        if not new or new == cur or _is_reshard_bucket(cur, new, pcfg, slug, fm):
            continue                       # already canonical, OR a valid reshard sub-bucket (don't revert)
        # collision guard: the canonical seat is already taken by a DIFFERENT file
        if (root / "wiki" / (new + ".md")).is_file():
            collisions.append((cur, new))
            continue
        if new in set(m.values()):                # two sources -> one destination
            other = next(k for k, v in m.items() if v == new)
            del m[other]
            collisions.append((other, new))
            collisions.append((cur, new))
            continue
        m[cur] = new
    return m, collisions


def make_rewriter(move_map: dict[str, str], namespace: str):
    # Match [[<namespace>/<slug>(#heading)?(|alias)?]] — capture key + remainder.
    pat = re.compile(r"\[\[(" + re.escape(namespace) + r"/[^\]|#\n]+)([\]#|])")

    def repl(mt):
        key, delim = mt.group(1), mt.group(2)
        new = move_map.get(key)
        return f"[[{new}{delim}" if new else mt.group(0)

    return pat, repl


def make_path_rewriter(move_map: dict[str, str]):
    """Rewrite BARE path references to a moved page — not [[wikilinks]] (make_rewriter's job).

    A page can point at another by its plain wiki-relative path in a frontmatter scalar
    (e.g. an assessment's `subject: entities/a/foo`) or in prose, with no `[[ ]]` around it.
    make_rewriter never sees those, so a reshard that only fixes wikilinks leaves every bare
    reference dangling — the exact break that stranded assessment subjects when big `entities/`
    buckets split a level deeper. This matches each old key as a WHOLE path token: the
    boundaries keep `entities/a/foo` from matching inside a longer slug (`entities/a/foobar`)
    or an already-deeper path, and the `[`/`/` lookbehind leaves `[[…]]` links (and paths that
    are themselves segments of a longer path) to the wikilink rewriter. Longest-key-first so a
    key that is a prefix of another can't shadow it."""
    if not move_map:
        return re.compile(r"(?!x)x"), (lambda mt: mt.group(0))   # matches nothing
    alt = "|".join(re.escape(k) for k in sorted(move_map, key=len, reverse=True))
    pat = re.compile(r"(?<![\w/\-\[])(" + alt + r")(?![\w/\-])")

    def repl(mt):
        return move_map.get(mt.group(1), mt.group(0))

    return pat, repl


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", default="entities")
    ap.add_argument("--types", default="", help="comma-list: restrict to these types (entity staged pilot)")
    ap.add_argument("--year", default="", help="restrict to this publication year (sources staged pilot)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--root", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    args = ap.parse_args(argv)
    root = Path(args.root)
    ns = args.namespace
    only = set(t.strip() for t in args.types.split(",") if t.strip()) or None
    only_year = args.year.strip() or None

    move_map, collisions = build_map(root, ns, only, only_year)
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} namespace={ns}: {len(move_map)} files to move"
          f", {len(collisions)} duplicate-slug collision(s) held back")
    # sample
    for k in list(move_map)[:6]:
        print(f"   {k}  ->  {move_map[k]}")
    if collisions:
        print(f"  ! collisions (dedup first — true-dup merge or slug disambiguation, okengine#165):")
        for cur, new in collisions[:40]:
            print(f"      {cur}  ~X~>  {new}")
        if len(collisions) > 40:
            print(f"      ... and {len(collisions) - 40} more")

    # --- link rewrite across the whole wiki ---
    pat, repl = make_rewriter(move_map, ns)
    wiki = root / "wiki"
    rewritten = links_changed = perm_errors = 0
    perm_files: list[str] = []
    for p in wiki.rglob("*.md"):
        sp = p.as_posix()
        if "/.git/" in sp:
            continue
        try:
            c = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f"[[{ns}/" not in c:
            continue
        new_c, n = pat.subn(repl, c)
        if n == 0 or new_c == c:
            continue
        links_changed += n
        rewritten += 1
        if args.apply:
            try:
                p.write_text(new_c, encoding="utf-8")
            except PermissionError:
                perm_errors += 1
                perm_files.append(p.relative_to(root).as_posix())
    print(f"link rewrite: {links_changed} links in {rewritten} files"
          f"{' (PENDING)' if not args.apply else ''}")
    if perm_errors:
        print(f"  ! {perm_errors} files not writable (handle as owner):")
        for f in perm_files[:50]:
            print(f"      {f}")

    # --- move files ---
    moved = move_errors = 0
    if args.apply:
        for old, new in move_map.items():
            src = root / "wiki" / (old + ".md")   # keys are wiki-relative (e.g. entities/acme-corp)
            dst = root / "wiki" / (new + ".md")
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
                moved += 1
            except OSError as e:
                move_errors += 1
                print(f"  ! move failed {old} -> {new}: {e}", file=sys.stderr)
        print(f"moved: {moved} files; move errors: {move_errors}")
    else:
        print(f"would move: {len(move_map)} files")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
