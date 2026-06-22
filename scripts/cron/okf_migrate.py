#!/usr/bin/env python3
"""OKF hierarchical migration — bulk, link-preserving namespace reorganization.

Replicates `iwe rename`'s semantics (move file + rewrite every [[reference]]) but
as a SINGLE O(n) pass over the corpus, because per-file `iwe rename` rebuilds the
graph each call (~13s on 40k = infeasible at scale). `iwe stats` is the validator
(run before/after: reference count must be unchanged), not the mover.

Phase 1 target: entities/ (flat) -> entities/{type}/[{letter}/]{slug}, where the
dominant types (e.g. type-a/type-b, each >500) are letter-sharded to
keep buckets under the OKF 500-entry rule. Only canonical-typed entities move;
ambiguous/typeless ones stay at entities/ root for classify-drain.

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
    return None


def build_map(root: Path, namespace: str, only_types: set[str] | None = None,
              only_year: str | None = None) -> dict[str, str]:
    """old-key -> new-key for flat files directly under the namespace dir, driven by
    the governing schema.yaml's `partitioning` config (domain-agnostic).
    only_types / only_year: staged-pilot filters (migration-time)."""
    schema = _governing_schema(root, namespace)
    pcfg = _partition_cfg(schema, namespace)
    canonical = set((schema.get("types") or {}).keys())
    base = root / "wiki" / namespace
    m: dict[str, str] = {}
    for p in sorted(base.glob("*.md")):           # glob-ok: migrates only flat top-level keys (already-nested = done)
        slug = p.stem
        if slug.startswith("_") or slug in ("index", "INDEX", "log", "README"):
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
        new = _new_key(namespace, slug, fm, pcfg, canonical)
        if new and only_year and new.startswith(f"{namespace}/") \
                and not new.startswith(f"{namespace}/{only_year}/"):
            new = None
        if new and new != f"{namespace}/{slug}":
            m[f"{namespace}/{slug}"] = new
    return m


def make_rewriter(move_map: dict[str, str], namespace: str):
    # Match [[<namespace>/<slug>(#heading)?(|alias)?]] — capture key + remainder.
    pat = re.compile(r"\[\[(" + re.escape(namespace) + r"/[^\]|#\n]+)([\]#|])")

    def repl(mt):
        key, delim = mt.group(1), mt.group(2)
        new = move_map.get(key)
        return f"[[{new}{delim}" if new else mt.group(0)

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

    move_map = build_map(root, ns, only, only_year)
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} namespace={ns}: {len(move_map)} files to move")
    # sample
    for k in list(move_map)[:6]:
        print(f"   {k}  ->  {move_map[k]}")

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
