#!/usr/bin/env python3
"""Consolidate strongly matched entity fragments onto one canonical record (#246).

Safety is deliberately asymmetric: a false merge corrupts knowledge, while a missed merge remains
visible to corpus-audit. A cluster is eligible only when every pair has the same primary identity
or at least two distinct shared name/alias identifiers. The best governed, grounded, reviewed page
wins; only additive list fields are merged. Loser prose is never blended into the canonical.

Dry-run by default. ``--apply`` additionally requires a reviewed YAML ``--approve`` mapping;
heuristics may propose a contaminated alias record and never authorize mutation by themselves.
Approved losers are tombstoned for audit history and point to the canonical record.
"""
from __future__ import annotations

import argparse
import itertools
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import entity_resolve  # noqa: E402
import schema_lib  # noqa: E402

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*\n?(.*)\Z", re.S)
_ADDITIVE = frozenset({"aliases", "sources", "related", "related_entities", "related_actors",
                       "tags", "merged_from"})
_MIN_KEY = 5


def _read(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    match = _FM.match(text)
    if not match:
        return {}, ""
    try:
        fm = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), match.group(2)


def _write(path: Path, fm: dict, body: str) -> None:
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{head}\n---\n{body}", encoding="utf-8")


def _values(fm: dict) -> tuple[str, set[str]]:
    primary = entity_resolve.normalize(str(fm.get("name") or fm.get("title") or ""))
    aliases = fm.get("aliases") or []
    aliases = aliases if isinstance(aliases, list) else [aliases]
    keys = {entity_resolve.normalize(str(v)) for v in aliases}
    if primary:
        keys.add(primary)
    return primary, {k for k in keys if len(k) >= _MIN_KEY}


def _strong(a: dict, b: dict) -> bool:
    ap, ak = _values(a)
    bp, bk = _values(b)
    return bool(ap and ap == bp) or len(ak & bk) >= 2


def clusters(records: dict[str, dict]) -> list[list[str]]:
    """Return only all-pairs-strong components; bridge-shaped components stay unresolved."""
    paths = sorted(records)
    identities = {p: _values(records[p]) for p in paths}
    by_key: dict[str, list[str]] = {}
    for p, (_primary, keys) in identities.items():
        for key in keys:
            by_key.setdefault(key, []).append(p)
    candidates = set()
    for claimants in by_key.values():
        candidates.update(itertools.combinations(sorted(claimants), 2))
    neighbors = {p: set() for p in paths}
    for a, b in candidates:
        ap, ak = identities[a]
        bp, bk = identities[b]
        if (ap and ap == bp) or len(ak & bk) >= 2:
            neighbors[a].add(b)
            neighbors[b].add(a)
    seen, out = set(), []
    for start in paths:
        if start in seen or not neighbors[start]:
            continue
        todo, component = [start], set()
        while todo:
            cur = todo.pop()
            if cur in component:
                continue
            component.add(cur)
            todo.extend(neighbors[cur] - component)
        seen |= component
        members = sorted(component)
        if len(members) > 1 and all(b in neighbors[a]
                                    for i, a in enumerate(members) for b in members[i + 1:]):
            out.append(members)
    return out


def _grounded(fm: dict) -> int:
    srcs = fm.get("sources") or []
    srcs = srcs if isinstance(srcs, list) else [srcs]
    return sum(1 for s in srcs if isinstance(s, str) and s.startswith("sources/") and "://" not in s)


def choose_winner(root: Path, members: list[str], records: dict[str, dict]) -> str:
    valid = set((schema_lib.merged_schema(root).get("types") or {}))

    def score(rel: str):
        fm = records[rel]
        return (1 if str(fm.get("type") or "") in valid else 0,
                _grounded(fm), 1 if fm.get("needs_review") is not True else 0,
                len(fm.get("aliases") or []), -len(rel))
    return max(members, key=lambda rel: (score(rel), rel))


def _union(winner: dict, losers: list[dict], loser_rels: list[str]) -> dict:
    out = dict(winner)
    for field in _ADDITIVE:
        values, seen = [], set()
        for fm in [winner, *losers]:
            raw = fm.get(field) or []
            raw = raw if isinstance(raw, list) else [raw]
            for value in raw:
                key = str(value).strip().casefold()
                if key and key not in seen:
                    seen.add(key)
                    values.append(value)
        if field == "merged_from":
            for rel in loser_rels:
                key = rel.casefold()
                if key not in seen:
                    seen.add(key)
                    values.append(rel)
        if values:
            out[field] = values
    return out


def _rewrite(root: Path, mapping: dict[str, str], apply: bool) -> int:
    changed = 0
    for p in (root / "wiki").rglob("*.md"):
        try:
            old = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        new = old
        for source, target in mapping.items():
            source, target = source.removesuffix(".md"), target.removesuffix(".md")
            # Rewrite address-bearing contexts only. A global string replacement would corrupt
            # the tombstone's own stable `id`, audit prose, or `merged_from` history.
            new = re.sub(r"(\[\[)" + re.escape(source) + r"(?=[\]|#])",
                         lambda match: match.group(1) + target, new)
            new = re.sub(r"(?m)^(\s*(?:-\s+|[A-Za-z_][\w-]*:\s+))" + re.escape(source)
                         + r"(?:\.md)?\s*$",
                         lambda match: match.group(1) + target, new)
        if new != old:
            changed += 1
            if apply:
                p.write_text(new, encoding="utf-8")
    return changed


def run(root: Path, apply: bool = False, approved: dict[str, str] | None = None) -> dict:
    wiki = root / "wiki"
    records, bodies, paths = {}, {}, {}
    for p in sorted((wiki / "entities").rglob("*.md")) if (wiki / "entities").is_dir() else []:
        if p.name.startswith(("_", ".", "INDEX")):
            continue
        try:
            fm, body = _read(p)
        except OSError:
            continue
        if not fm or str(fm.get("status") or "").lower() == "tombstoned":
            continue
        rel = p.relative_to(wiki).as_posix()[:-3]
        records[rel], bodies[rel], paths[rel] = fm, body, p
    mapping = {}
    results = []
    for members in clusters(records):
        winner = choose_winner(root, members, records)
        losers = [m for m in members if m != winner]
        mapping.update({loser: winner for loser in losers})
        results.append({"winner": winner, "losers": losers})
    if apply:
        if approved is None:
            raise ValueError("--apply requires a reviewed --approve source-to-canonical mapping")
        approved = {str(k).removesuffix(".md"): str(v).removesuffix(".md")
                    for k, v in approved.items()}
        invalid = {k: v for k, v in approved.items() if mapping.get(k) != v}
        if invalid:
            raise ValueError(f"approval does not match current candidates: {invalid}")
        mapping = dict(approved)
        results = [{"winner": winner, "losers": sorted([k for k, v in mapping.items() if v == winner])}
                   for winner in sorted(set(mapping.values()))]
    # Rewrite consumers while all source pages still carry their original identity. Winner merge
    # history and loser tombstones are authored afterward and therefore remain audit-accurate.
    rewritten = _rewrite(root, mapping, apply)
    if apply:
        for item in results:
            winner, losers = item["winner"], item["losers"]
            merged = _union(records[winner], [records[x] for x in losers], losers)
            merged["last_updated"] = date.today().isoformat()
            _write(paths[winner], merged, bodies[winner])
            for loser in losers:
                old = records[loser]
                tomb = {"type": old.get("type") or "entity", "id": old.get("id") or loser,
                        "status": "tombstoned", "tombstone_reason": f"duplicate of {winner}",
                        "redirect_to": winner, "last_updated": date.today().isoformat()}
                _write(paths[loser], tomb,
                       f"> **Tombstoned.** duplicate of [[{winner}]].\n")
    return {"clusters": results, "mapping": mapping, "files_rewritten": rewritten}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, default=Path(os.environ.get("WIKI_PATH", "/opt/vault")))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve", type=Path,
                        help="reviewed YAML mapping of duplicate entity paths to canonicals")
    args = parser.parse_args(argv)
    approved = None
    if args.approve:
        approved = yaml.safe_load(args.approve.read_text(encoding="utf-8")) or {}
        if not isinstance(approved, dict):
            parser.error("--approve must contain a YAML mapping")
    try:
        result = run(args.vault, args.apply, approved)
    except ValueError as exc:
        parser.error(str(exc))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"entity-converge: {mode} {len(result['clusters'])} candidate cluster(s), "
          f"{len(result['mapping'])} duplicate(s), {result['files_rewritten']} referring file(s)")
    for item in result["clusters"]:
        print(f"  {', '.join(item['losers'])} -> {item['winner']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
