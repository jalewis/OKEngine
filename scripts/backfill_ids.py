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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
import generated_page  # noqa: E402 — the shared derivation generated-page writers use (#933)

# The skip rule, frontmatter parse, id insertion and derivation live in generated_page so a
# regenerated dashboard/report writes the very id this pass would stamp (okengine#933).
_skip = generated_page.is_exempt
_parse_fm = generated_page.parse_frontmatter
_insert_id = generated_page.insert_id
derive_for_page = generated_page.derive_for_page


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
                pid = generated_page.disambiguate(pid, rel)
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
