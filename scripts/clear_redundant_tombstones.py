#!/usr/bin/env python3
"""Free a canonical seat held by a provably-redundant tombstone (okengine#520 follow-up).

`okf_migrate` will not move a live page onto an occupied path, so a retired duplicate sitting
in the survivor's canonical seat holds that survivor off-shard permanently. The mover cannot
fix it: freeing the seat means removing a page, and that is a dedup decision, not a placement
one. This is the dedup-side half.

REDUNDANT means one specific, checkable thing: the occupant is `status: tombstoned` and its
`superseded_by` **designates the very page whose canonical seat it occupies** — by path (what
this engine writes: `dedup_sources_by_url` stores `survivor_rel`, and `write_server._tombstone`
sanitises the value as a path) or, for the small minority written that way, by id. A tombstone
exists to redirect its path to a survivor; once that survivor occupies the path itself, a link
landing there reaches the survivor directly and the redirect has nothing left to do. Removing it
loses no resolution — it removes a hop.

WHAT IT REFUSES, because "redundant" has to be proven and not assumed:
  * an occupant designating any OTHER page — that redirect is still load-bearing. On the live
    vault two such remain: one whose `superseded_by` names a different entity entirely, and one
    pointing at a different article;
  * an occupant that is not tombstoned, or a mover that is;
  * an empty pointer, so empty never matches empty;
  * anything where the mover's canonical key is not the occupant's exact path.

Removed files are ARCHIVED outside the vault before deletion (`--archive`, default
`~/okengine-backups/redundant-tombstones-<ts>/`), so this is reversible. It also appends one
line per removal to the vault log.

The move itself is left to reshelve: this only frees the seat. Run reshelve afterwards.

Dry-run is the default. `--apply` writes.

Usage:
  python3 scripts/clear_redundant_tombstones.py --vault /path/to/pack
  python3 scripts/clear_redundant_tombstones.py --vault /path/to/pack --apply
"""
from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "cron"))
import okf_migrate  # noqa: E402


def _fm(root: Path, key: str) -> dict:
    return okf_migrate._fm_at(root, key)


def plan(root: Path, namespace: str) -> tuple[list[tuple[str, str]], dict]:
    """(removable, skipped-by-reason). `removable` is (occupant_key, mover_key)."""
    _moves, collisions = okf_migrate.build_map(root, namespace)
    kinds = okf_migrate.classify_collisions(root, collisions)
    removable: list[tuple[str, str]] = []
    skipped = {"points_elsewhere": len(kinds["blocked_by_tombstone"]),
               "live_conflict": len(kinds["live_conflict"]),
               "failed_recheck": 0}

    for mover_key, seat_key in kinds["redundant_tombstone"]:
        occupant, mover = _fm(root, seat_key), _fm(root, mover_key)
        mover_id = str(mover.get("id") or "").strip()
        superseded = str(occupant.get("superseded_by") or "").strip()
        # Re-prove independently of the classifier rather than trusting its verdict: this is
        # the only step that deletes, so it re-derives its own precondition. Path OR id, matching
        # the classifier — the corpus carries both and neither is schema-defined.
        designates_mover = bool(superseded) and (
            superseded == mover_key or (bool(mover_id) and superseded == mover_id))
        ok = (okf_migrate.is_tombstoned(occupant)
              and not okf_migrate.is_tombstoned(mover)
              and designates_mover
              and okf_migrate.canonical_key(root, namespace, Path(mover_key).name, mover)
              == seat_key)
        if ok:
            removable.append((seat_key, mover_key))
        else:
            skipped["failed_recheck"] += 1
    return removable, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True, help="pack/vault root (contains wiki/)")
    ap.add_argument("--namespace", default="sources")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--archive", default="", help="where removed pages are copied first")
    args = ap.parse_args(argv)

    root = Path(args.vault)
    if not (root / "wiki").is_dir():
        print(f"clear-redundant-tombstones: no wiki/ under {root}", file=sys.stderr)
        return 2

    removable, skipped = plan(root, args.namespace)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = Path(args.archive) if args.archive else (
        Path.home() / "okengine-backups" / f"redundant-tombstones-{stamp}")

    print(f"=== clear-redundant-tombstones ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    print(f"  vault              : {root}")
    print(f"  namespace          : {args.namespace}")
    print(f"  redundant seats    : {len(removable)}")
    print(f"  points elsewhere   : {skipped['points_elsewhere']} (left alone — still redirecting)")
    print(f"  live-vs-live        : {skipped['live_conflict']} (genuine duplicate slug)")
    print(f"  failed re-check    : {skipped['failed_recheck']}")
    for seat, mover in removable[:10]:
        print(f"   free  {seat}\n         so     {mover}  can take it")
    if len(removable) > 10:
        print(f"   … and {len(removable) - 10} more")

    if not args.apply:
        print("  (dry run — nothing removed; pass --apply)")
        return 0
    if not removable:
        return 0

    archive.mkdir(parents=True, exist_ok=True)
    log = root / "wiki" / "log.md"
    today = _dt.date.today().isoformat()
    done = 0
    for seat, mover in removable:
        src = root / "wiki" / (seat + ".md")
        dest = archive / (seat.replace("/", "__") + ".md")
        try:
            shutil.copy2(src, dest)      # archive BEFORE unlink, never after
            src.unlink()
        except OSError as exc:
            print(f"  ! {seat}: {exc}", file=sys.stderr)
            continue
        done += 1
        try:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"- {today} clear-redundant-tombstone {seat} — superseded by the page "
                         f"taking that seat ({mover}); archived\n")
        except OSError:
            pass
    print(f"  removed            : {done}  (archived to {archive})")
    print("  next: run reshelve so the freed seats are taken.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
