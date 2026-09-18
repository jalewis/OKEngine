#!/usr/bin/env python3
"""Retire duplicate source pages that share a URL (okengine#516).

A source document is identified by its `url`. 16,064 pairs of source pages on one deployment
hold the same url — the same document stored twice — and the id-index cannot see them: the two
copies have different titles, so they slug to different ids and never collide. Every existing
detector reports clean.

For each URL group this picks ONE survivor, folds anything the losers know into it, retires the
losers as tombstones, and repoints every reference. Dry-run is the default.

WHAT IT WILL NOT DO
  * delete a page. Losers are TOMBSTONED (`status: tombstoned` + `superseded_by`), the shape
    the write path uses and the id-index already understands. A bare unlink would discard
    provenance and strand every inbound reference.
  * choose the survivor by FILENAME. The `-link` half is the smaller one ~99% of the time, but
    in 45 of 4,922 measured pairs it is LARGER — a filename rule would silently destroy content
    in those. Selection is by evidence, and ties break deterministically so repeated runs agree.
  * touch a group where any member has malformed frontmatter — mirroring `_tombstone`, which
    refuses rather than silently wiping it.
  * overwrite a field the survivor already has. The merge is strictly ADDITIVE: a loser can
    contribute a field the survivor lacks, never replace one it has.

Link repointing reuses okf_migrate's rewriters rather than a local regex — `make_rewriter` for
`[[wikilinks]]` and `make_path_rewriter` for BARE path references in frontmatter scalars and
prose. Fixing only wikilinks is how a previous migration left bare references dangling.

Usage:
  python3 scripts/dedup_sources_by_url.py --vault /path/to/pack
  python3 scripts/dedup_sources_by_url.py --vault /path/to/pack --apply --limit 50
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

_FM = re.compile(r"\A---\n(.*?\n)---\n", re.S)
NAMESPACE = "sources"


def _load_okf_migrate():
    here = Path(__file__).resolve().parent / "cron"
    sys.path.insert(0, str(here))
    spec = importlib.util.spec_from_file_location("okf_migrate", here / "okf_migrate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["okf_migrate"] = module
    spec.loader.exec_module(module)
    return module


_REAL_URL = re.compile(r"^https?://[^\s/]+", re.I)


def norm_url(value: object) -> str:
    """Whitespace and ONE trailing slash; no case folding. Must match
    write_server._norm_url and reid_sources_by_url.norm_url — three readers, one rule."""
    text = str(value or "").strip()
    if not text or not _REAL_URL.match(text):
        return ""
    return text[:-1] if text.endswith("/") and len(text) > 1 else text


def read_page(path: Path) -> tuple[dict | None, str, str]:
    """(frontmatter, body, raw). frontmatter is None when it is missing or malformed —
    the caller must refuse that group rather than guess."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "", ""
    match = _FM.match(raw)
    if not match:
        return None, raw, raw
    try:
        fm = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None, raw[match.end():], raw
    if not isinstance(fm, dict):
        return None, raw[match.end():], raw
    return fm, raw[match.end():], raw


def _snapshot_key(fm: dict) -> tuple[str, str] | None:
    """Stable identity for versioned reference-data snapshots, when declared."""
    dataset = str(fm.get("dataset") or fm.get("retrieved_via") or "").strip()
    record = str(
        fm.get("dataset_record_id")
        or fm.get("upstream_record_id")
        or fm.get("record_id")
        or ""
    ).strip()
    return (dataset, record) if dataset and record else None


def _freshness(fm: dict) -> float:
    """Newest declared observation time as a comparable UTC timestamp."""
    for field in ("collection_timestamp", "retrieved_at", "last_updated", "created"):
        value = fm.get(field)
        if not value:
            continue
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
    return 0.0


def _score(
    rel: str,
    fm: dict,
    body: str,
    canonical_key: str | None,
    *,
    snapshot_group: bool = False,
) -> tuple:
    """Survivor ranking — higher wins. Evidence first, then stability; never the filename.

    1. a real body (a stub companion carries "no extractable claims")
    2. more citations/links, i.e. more of the corpus depends on it
    3. sitting in its canonical partition seat already (no move needed)
    4. shorter path, then lexical — arbitrary but DETERMINISTIC, so two runs agree
    """
    def reference_count(value) -> int:
        if isinstance(value, list):
            return len(value)
        return 1 if value else 0

    citations = sum(reference_count(fm.get(field))
                    for field in ("sources", "concepts", "entities"))
    evidence = (
        len(body.strip()),
        citations,
        1 if (canonical_key and rel == canonical_key) else 0,
        -len(rel),
        rel,
    )
    # A repeated dataset record is a sequence of observations, not two independent
    # captures of a static article. Keep the latest record state before considering
    # body size; otherwise equal-body structured snapshots resolve by arbitrary hash path
    # and can preserve stale checksum/reliability/credibility values.
    return (_freshness(fm), *evidence) if snapshot_group else evidence


def plan(vault: Path) -> dict:
    okf = _load_okf_migrate()
    wiki = vault / "wiki"
    base = wiki / NAMESPACE
    groups: dict[str, list[Path]] = defaultdict(list)
    malformed: list[str] = []

    if base.is_dir():
        for path in sorted(base.rglob("*.md")):
            fm, body, _raw = read_page(path)
            if fm is None:
                malformed.append(path.relative_to(wiki).as_posix())
                continue
            if str(fm.get("type") or "").strip() != "source":
                continue
            if str(fm.get("status") or "").strip().lower() == "tombstoned":
                continue                    # already retired
            url = norm_url(fm.get("url"))
            if url:
                groups[url].append(path)

    actions: list[dict] = []
    skipped_malformed = 0
    for url, paths in sorted(groups.items()):
        if len(paths) < 2:
            continue
        members = [read_page(path) for path in paths]
        snapshot_keys = {_snapshot_key(fm or {}) for fm, _body, _raw in members}
        snapshot_group = len(snapshot_keys) == 1 and None not in snapshot_keys
        ranked = []
        for p, (fm, body, _raw) in zip(paths, members, strict=True):
            rel = p.relative_to(wiki).as_posix()[:-3]
            try:
                canon = okf.canonical_key(vault, NAMESPACE, p.stem, fm or {})
            except Exception:
                canon = None
            ranked.append((
                _score(rel, fm or {}, body, canon, snapshot_group=snapshot_group),
                rel,
                p,
                fm or {},
            ))
        ranked.sort(reverse=True)
        survivor = ranked[0]
        losers = ranked[1:]
        actions.append({
            "url": url,
            "survivor": survivor[1],
            "losers": [(r[1], r[3]) for r in losers],
            "survivor_fm": survivor[3],
        })
    return {"actions": actions, "groups": len(groups), "malformed": malformed,
            "skipped_malformed": skipped_malformed,
            "malformed_dirs": {m.rsplit("/", 1)[0] for m in malformed}}


def _additive_merge(survivor_fm: dict, loser_fm: dict) -> dict:
    """Fields the loser has and the survivor lacks. Never replaces; never touches identity."""
    protected = {"id", "type", "url", "status", "superseded_by", "version"}
    add = {}
    for key, value in loser_fm.items():
        if key in protected or key in survivor_fm:
            continue
        if value in (None, "", [], {}):
            continue
        add[key] = value
    return add


def _load_governed_writer(vault: Path):
    """Load the gateway's governed write boundary for this operator migration."""
    repo = Path(__file__).resolve().parent.parent
    for location in (repo, repo / "src", repo / "scripts" / "cron", repo / "okengine-mcp"):
        value = str(location)
        if value not in sys.path:
            sys.path.insert(0, value)
    os.environ["WIKI_PATH"] = str(vault.resolve())
    catalog = repo / "config" / "policy" / "catalog.yaml"
    if catalog.is_file():
        os.environ["OKENGINE_POLICY_CATALOG"] = str(catalog)
    spec = importlib.util.spec_from_file_location(
        f"okengine_dedup_writer_{abs(hash(vault.resolve()))}",
        repo / "okengine-mcp" / "write_server.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load governed write server")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_actions(vault: Path, actions: list[dict]) -> dict:
    okf = _load_okf_migrate()
    writer = _load_governed_writer(vault)
    wiki = vault / "wiki"
    move_map: dict[str, str] = {}
    eligible: list[dict] = []
    errors: list[str] = []
    tombstoned = merged = 0

    # Converge through the same boundary as the gateway. A refused group remains
    # wholly live and is excluded from both relinking and retirement.
    for act in actions:
        survivor_rel = act["survivor"]
        survivor_path = wiki / (survivor_rel + ".md")
        survivor_fm, _body, _raw = read_page(survivor_path)
        if survivor_fm is None:
            errors.append(f"{survivor_rel}: survivor became unreadable")
            continue
        pending_add: dict = {}
        for _loser_rel, loser_fm in act["losers"]:
            add = _additive_merge({**survivor_fm, **pending_add}, loser_fm)
            if add:
                pending_add.update(add)
                merged += 1
        merge_fm = {"type": survivor_fm.get("type") or "source", **pending_add}
        if isinstance(survivor_fm.get("id"), str):
            merge_fm["id"] = survivor_fm["id"]
        result = writer._converge(survivor_rel, merge_fm)
        if not result.startswith("converged"):
            errors.append(f"{survivor_rel}: governed converge failed: {result}")
            continue
        eligible.append(act)
        for loser_rel, _loser_fm in act["losers"]:
            move_map[loser_rel] = survivor_rel

    # Repoint before retiring. If a tombstone is later refused, the still-live group
    # remains visible to the next plan; tombstoning first could make repair non-retryable.
    relinked_files = link_rewrites = 0
    relink_safe = True
    if move_map:
        rewriters = [okf.make_rewriter(move_map, NAMESPACE), okf.make_path_rewriter(move_map)]
        for path in wiki.rglob("*.md"):
            rel = path.relative_to(wiki).as_posix()[:-3]
            if rel in move_map:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{rel}: cannot verify/rewrite references: {exc}")
                relink_safe = False
                continue
            new = text
            counter = {"n": 0}

            def _counting(repl, counter=counter):
                def wrapped(mt):
                    out = repl(mt)
                    if out != mt.group(0):
                        counter["n"] += 1
                    return out
                return wrapped

            for pat, repl in rewriters:
                new = pat.sub(_counting(repl), new)
            if new != text:
                try:
                    path.write_text(new, encoding="utf-8")
                except OSError as exc:
                    errors.append(f"{rel}: reference rewrite failed: {exc}")
                    relink_safe = False
                    continue
                relinked_files += 1
                link_rewrites += counter["n"]

    for act in eligible if relink_safe else []:
        survivor_rel = act["survivor"]
        for loser_rel, _loser_fm in act["losers"]:
            reason = str(_loser_fm.get("tombstone_reason") or "").strip() or (
                f"duplicate of {survivor_rel} (same url) — okengine#516"
            )
            result = writer._tombstone(
                loser_rel,
                reason,
                survivor_rel,
            )
            if result.startswith("tombstoned"):
                tombstoned += 1
            else:
                errors.append(f"{loser_rel}: governed tombstone failed: {result}")
    return {
        "tombstoned": tombstoned,
        "merged": merged,
        "link_files": relinked_files,
        "link_rewrites": link_rewrites,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--limit", type=int, default=0, help="cap groups this run (0 = all)")
    args = ap.parse_args(argv)

    vault = Path(args.vault)
    if not (vault / "wiki").is_dir():
        print(f"dedup: no wiki/ under {vault}", file=sys.stderr)
        return 2

    result = plan(vault)
    actions = result["actions"]
    if args.limit:
        actions = actions[:args.limit]

    losers = sum(len(a["losers"]) for a in actions)
    print(f"=== dedup-sources-by-url ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    print(f"  vault              : {vault}")
    print(f"  distinct urls      : {result['groups']}")
    print(f"  duplicate groups   : {len(actions)}"
          + (f" (of {len(result['actions'])}, --limit)" if args.limit else ""))
    print(f"  pages to retire    : {losers}")
    print(f"  groups skipped     : {result['skipped_malformed']} (a member has malformed frontmatter)")
    print(f"  malformed pages    : {len(result['malformed'])}")
    for act in actions[:6]:
        print(f"   {act['url']}")
        print(f"      KEEP    {act['survivor']}")
        for rel, _fm in act["losers"]:
            print(f"      RETIRE  {rel}")
    if len(actions) > 6:
        print(f"   … and {len(actions) - 6} more groups")

    if not args.apply:
        print("  (dry run — nothing written; pass --apply)")
        return 0
    stats = apply_actions(vault, actions)
    print(f"  tombstoned         : {stats['tombstoned']}")
    print(f"  survivors enriched : {stats['merged']}")
    print(f"  files relinked     : {stats['link_files']} "
          f"({stats['link_rewrites']} references repointed)")
    for error in stats["errors"][:20]:
        print(f"  ERROR: {error}", file=sys.stderr)
    if len(stats["errors"]) > 20:
        print(f"  ERROR: … and {len(stats['errors']) - 20} more", file=sys.stderr)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
