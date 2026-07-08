#!/usr/bin/env python3
"""okengine.frontier-watch wake-gate — pick demand/supply whitespace to thesis on (okengine#147).

Whitespace = a capability the market clearly WANTS but few players SUPPLY. We measure that
directly from the vault graph, per concept (= capability):

  demand = distinct SOURCE pages that reference `[[concepts/<slug>]]`   (the market talking about it)
  supply = distinct ENTITY pages that reference `[[concepts/<slug>]]`   (players/products providing it)

A whitespace candidate has demand >= MIN_DEMAND and supply <= MAX_SUPPLY (wanted, under-served).
We require a real concept page (a named capability, not a dangling link), skip capabilities a
recent `frontier/` thesis already covers (rotation), and surface the highest-demand candidates.

Prints a human digest (each candidate's demand/supply) then a final `{"wakeAgent": bool}` line
(the cron-plus wake-gate protocol). LOCAL-ONLY; no writes — the agent writes the
`frontier/<slug>` whitespace-thesis page via the okengine-write MCP path.

Self-contained: stdlib + yaml only (it runs from its own staged dir; see the extension
self-containment guard in tests/extensions/test_first_party.py).
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
MIN_DEMAND = int(os.environ.get("OKENGINE_FRONTIER_MIN_DEMAND", "5"))       # config.min_demand
MAX_SUPPLY = int(os.environ.get("OKENGINE_FRONTIER_MAX_SUPPLY", "2"))       # config.max_supply
REANALYZE_DAYS = int(os.environ.get("OKENGINE_FRONTIER_REANALYZE_DAYS", "60"))  # config.reanalyze_days
BATCH = int(os.environ.get("OKENGINE_FRONTIER_BATCH_SIZE", "5"))            # config.batch_size

# Match `[[concepts/<slug>]]` AND sharded `[[concepts/<shard>/.../<slug>]]`, capturing the final
# slug so both link forms fold into one capability (the okengine#145 sharding lesson).
_CONCEPT_LINK = re.compile(r"\[\[concepts/(?:[a-z0-9][a-z0-9-]*/)*([a-z0-9][a-z0-9-]*)(?:[#|][^\]]*)?\]\]")
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _today() -> str:
    return os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat()


def _cutoff() -> str:
    return (date.fromisoformat(_today()) - timedelta(days=REANALYZE_DAYS)).isoformat()


def _ns(md: Path) -> str:
    rel = md.relative_to(WIKI).parts
    return rel[0] if len(rel) > 1 else ""


def _read_fm(md: Path) -> dict:
    try:
        m = _FM.match(md.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return {}
    if not m:
        return {}
    try:
        import yaml
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _recently_thesised() -> set[str]:
    """Capability slugs a `whitespace-thesis` has already MAPPED within REANALYZE_DAYS (rotation).

    The analyzed capability is declared authoritatively in the thesis's REQUIRED `capability`
    frontmatter (a single `[[concepts/<slug>]]`) — read THAT, not every `[[concepts/…]]` the thesis
    happens to cite. A thesis routinely references adjacent concepts (see_also, body comparisons);
    treating those as "thesised" starved genuinely-un-thesised demand-rich/supply-thin
    capabilities out of discovery for REANALYZE_DAYS (the same okengine.lacuna precedent). Legacy
    theses without `capability` fall back to their bracketed links."""
    covered: set[str] = set()
    fdir = WIKI / "frontier"
    if not fdir.is_dir():
        return covered
    cutoff = _cutoff()
    for md in fdir.rglob("*.md"):
        fm = _read_fm(md)
        if str(fm.get("type", "")).strip() != "whitespace-thesis":
            continue
        when = str(fm.get("updated") or fm.get("created") or "")[:10]
        if when and when < cutoff:
            continue
        cap = fm.get("capability")
        if cap:
            for ref in (cap if isinstance(cap, list) else [cap]):
                covered |= set(_CONCEPT_LINK.findall(str(ref)))
        else:                              # legacy thesis w/o the field — best-effort from links
            try:
                covered |= set(_CONCEPT_LINK.findall(md.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
    return covered


def _demand_supply() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Walk once. (slug -> source pages referencing it, slug -> entity pages referencing it).
    A concept's own page never counts toward its demand/supply."""
    demand: dict[str, set[str]] = defaultdict(set)
    supply: dict[str, set[str]] = defaultdict(set)
    for md in WIKI.rglob("*.md"):
        ns = _ns(md)
        if ns not in ("sources", "entities"):
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = md.relative_to(WIKI).as_posix()
        for slug in set(_CONCEPT_LINK.findall(text)):
            (demand if ns == "sources" else supply)[slug].add(rel)
    return demand, supply


def _has_concept_page(slug: str) -> bool:
    return any((WIKI / "concepts").rglob(f"{slug}.md")) if (WIKI / "concepts").is_dir() else False


def main() -> int:
    if not WIKI.is_dir():
        print(json.dumps({"wakeAgent": False}))
        return 0

    demand, supply = _demand_supply()
    recent = _recently_thesised()

    cands = []
    for slug, dpages in demand.items():
        d = len(dpages)
        s = len(supply.get(slug, ()))
        if d < MIN_DEMAND or s > MAX_SUPPLY:
            continue
        if slug in recent or not _has_concept_page(slug):
            continue
        cands.append((d, s, slug))
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))     # highest demand, then thinnest supply

    print("=== frontier-watch whitespace wake-gate ===")
    print(f"  vault: {WIKI}")
    print(f"  whitespace candidates (demand >= {MIN_DEMAND}, supply <= {MAX_SUPPLY}): {len(cands)}")
    print(f"  excluded (thesised since {_cutoff()}): {len(recent)}")

    if not cands:
        print("  -> SKIP: no demand-rich, supply-thin capability to thesis")
        print(json.dumps({"wakeAgent": False}))
        return 0

    chosen = cands[:BATCH]
    print(f"  batch: {len(chosen)} of {len(cands)}\n")
    print("=== whitespace candidates ===")
    print("Write ONE whitespace-thesis for the capability you can most honestly ground (highest "
          "demand, thinnest supply). DEFER a candidate whose 'thin supply' is just missing data, "
          "not a real market gap. Record demand/supply as `frontier_density`.\n")
    for i, (d, s, slug) in enumerate(chosen, 1):
        print(f"## {i}. capability: {slug}  (demand {d} sources · supply {s} entities)")
        print(f"  anchor: `[[concepts/{slug}]]`  ·  frontier_density: `demand {d} · supply {s}`\n")

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
