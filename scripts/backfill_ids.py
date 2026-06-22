#!/usr/bin/env python3
"""backfill_ids — one-shot: stamp an immutable `id` on every page that lacks one.

Composable-okpacks P1: identity must exist before converge-on-write can key on it.
This pass derives each id-less page's id (id_lib.derive_id) from its frontmatter +
the governing schema's per-type authority binding, and **stamps it once**. It NEVER
recomputes an existing `id` (ids are immutable, RFC §5a), so re-running is a no-op
on already-stamped pages.

Derivation:
  - **authority id** when the page's type declares `id_authority` and the page
    carries the local id (`<id_field>`): e.g. `mitre:t1059`.
  - **minted slug** otherwise: scoped to the page's top-level namespace (frozen)
    from its natural key (title/name/stem): e.g. `entities:acme-corp`.

Collisions (a derived id already stamped on a *different* page):
  - **slug** → disambiguated with a path-stable hash (slug ids collide by design)
    and reported,
  - **authority** → left as-is and reported LOUDLY (two pages sharing an authority
    id are genuine duplicates → a merge, which is out of scope for this pass).

Default is a DRY RUN (reports only). Pass --apply to write.
Env: WIKI_PATH (vault root, default /opt/vault).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
import id_lib       # noqa: E402
import schema_lib   # noqa: E402

import yaml         # noqa: E402

_FM_RE = re.compile(r"\A(---\s*\n)(.*?\n)(---\s*(?:\n|\Z))", re.DOTALL)
_ID_LINE = re.compile(r"^id:\s", re.MULTILINE)
_RESERVED = {"index.md", "log.md", "agents.md", "hot.md", "bundle.md", "health.md"}


def _skip(p: Path) -> bool:
    n = p.name.lower()
    return (n in _RESERVED or p.name.startswith(("_", "."))
            or ".bak." in p.name or n == "schema.yaml")


def _parse_fm(text: str):
    m = _FM_RE.match(text)
    if not m:
        return None, None
    try:
        fm = yaml.safe_load(m.group(2))
    except Exception:
        return None, m
    return (fm if isinstance(fm, dict) else {}), m


def _insert_id(text: str, pid: str, m: "re.Match") -> str:
    """Insert `id: "<pid>"` as the first frontmatter line — surgical, preserves
    the rest of the file byte-for-byte."""
    return m.group(1) + f'id: "{pid}"\n' + m.group(2) + m.group(3) + text[m.end():]


def derive_for_page(vault: Path, rel: str, fm: dict) -> tuple[str, str]:
    """(id, kind) for a page at wiki-relative `rel` with frontmatter `fm`."""
    namespace = rel.split("/", 1)[0]
    stem = Path(rel).stem
    schema = schema_lib.merged_schema(vault, namespace)
    ptype = str(fm.get("type") or "").strip()
    authority, id_field = schema_lib.type_id_authority(schema, ptype)
    local_id = fm.get(id_field) if authority else None
    return id_lib.derive_id(authority=authority, local_id=local_id,
                            minted_scope=namespace,
                            slug_source=id_lib.natural_key(fm, fallback=stem))


def run(vault: Path, apply: bool) -> dict:
    wiki = vault / "wiki"
    claimed: dict[str, str] = {}     # id -> rel (first claimant this run + pre-existing)
    stamped = skipped = 0
    slug_collisions: list[tuple[str, str, str]] = []      # (id, new_rel, existing_rel)
    authority_collisions: list[tuple[str, str, str]] = []
    if not wiki.is_dir():
        return {"stamped": 0, "skipped": 0, "slug_collisions": [], "authority_collisions": []}
    base = wiki.resolve()

    # First pass: record ids already on disk so collisions are detected against them.
    for p in sorted(wiki.rglob("*.md")):
        if _skip(p):
            continue
        fm, _ = _parse_fm(p.read_text(encoding="utf-8", errors="replace"))
        if isinstance(fm, dict) and isinstance(fm.get("id"), str) and fm["id"].strip():
            claimed.setdefault(fm["id"].strip(), str(p.resolve().relative_to(base)))

    for p in sorted(wiki.rglob("*.md")):
        if _skip(p):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        fm, m = _parse_fm(text)
        if fm is None or m is None:
            skipped += 1            # no/invalid frontmatter — schema validation's job
            continue
        if isinstance(fm.get("id"), str) and fm["id"].strip():
            continue                # immutable — never recompute
        rel = str(p.resolve().relative_to(base))
        pid, kind = derive_for_page(vault, rel, fm)
        if pid in claimed and claimed[pid] != rel:
            if kind == "slug":
                ns = id_lib.parse_id(pid)[0]
                pid = id_lib.make_id(ns, f"{Path(rel).stem}-{id_lib._short_hash(rel, 6)}")
                slug_collisions.append((pid, rel, claimed.get(pid, "")))
            else:
                authority_collisions.append((pid, rel, claimed[pid]))
                # leave the duplicate id as-is; a merge is out of scope here
        claimed[pid] = rel
        stamped += 1
        if apply:
            p.write_text(_insert_id(text, pid, m), encoding="utf-8")

    return {"stamped": stamped, "skipped": skipped,
            "slug_collisions": slug_collisions, "authority_collisions": authority_collisions}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write ids (default: dry run)")
    ap.add_argument("--vault", default=os.environ.get("WIKI_PATH", "/opt/vault"))
    args = ap.parse_args(argv)
    res = run(Path(args.vault), args.apply)
    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"backfill-ids [{mode}]: stamped {res['stamped']}, skipped {res['skipped']} "
          f"(no/invalid frontmatter), slug-collisions {len(res['slug_collisions'])}, "
          f"authority-collisions {len(res['authority_collisions'])}")
    for pid, rel, other in res["authority_collisions"]:
        print(f"  ! AUTHORITY-ID DUPLICATE {pid}: {rel} vs {other} — needs a merge")
    for pid, rel, _ in res["slug_collisions"]:
        print(f"  · slug collision disambiguated -> {pid} ({rel})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
