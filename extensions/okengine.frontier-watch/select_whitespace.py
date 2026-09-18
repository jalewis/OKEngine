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

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml


WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
# The lane VERIFIES its work against a selection manifest, and this selector never wrote one, so
# every run failed its completion receipt with "selection manifest unavailable" no matter what the
# agent did. That is the same shape as the six lanes that demanded a receipt against a manifest
# their selectors never wrote — 171 guaranteed failures — and it is invisible from the lane's own
# output, which looked like a healthy wake-gate right up to the receipt check.
SELECTION_MANIFEST = Path(os.environ.get(
    "OKENGINE_SELECTION_MANIFEST",
    str(Path(os.environ.get("HERMES_HOME",
                            str(Path(os.environ.get("WIKI_PATH", "/opt/vault")) / ".hermes-data")))
        / "cron-plus" / "selections" / "okengine.frontier-watch:whitespace-sweep.json"),
))
MIN_DEMAND = int(os.environ.get(
    "OKENGINE_FRONTIER_MIN_DEMAND", os.environ.get("OKENGINE_FRONTIER_WATCH_MIN_DEMAND", "5")
))
MAX_SUPPLY = int(os.environ.get(
    "OKENGINE_FRONTIER_MAX_SUPPLY", os.environ.get("OKENGINE_FRONTIER_WATCH_MAX_SUPPLY", "2")
))
REANALYZE_DAYS = int(os.environ.get(
    "OKENGINE_FRONTIER_REANALYZE_DAYS",
    os.environ.get("OKENGINE_FRONTIER_WATCH_REANALYZE_DAYS", "60"),
))
BATCH = int(os.environ.get(
    "OKENGINE_FRONTIER_BATCH_SIZE", os.environ.get("OKENGINE_FRONTIER_WATCH_BATCH_SIZE", "5")
))

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


# A frontmatter value naming a page, e.g. `entities/a/i/ai-security` or `[[concepts/x/y]]`.
_DECLARED_REF = re.compile(
    r"\[?\[?(?:concepts|entities)/(?:[a-z0-9][a-z0-9-]*/)*([a-z0-9][a-z0-9-]*)\]?\]?$")
# Fields whose values are IDENTIFIERS or provenance, not statements of association. `id` contains
# the page's own slug; `sources` cites where a claim came from. Counting either inflates supply
# with pages that merely mention a name.
_NOT_A_RELATION = {"id", "slug", "sources", "raw", "url", "website", "canonical_source",
                   "source", "source_refs", "created_from_sources"}


def _declared_slugs(front: str) -> set[str]:
    """Concept/segment slugs an entity's frontmatter DECLARES a relation to."""
    try:
        fm = yaml.safe_load(front)
    except Exception:
        return set()
    if not isinstance(fm, dict):
        return set()
    out = set()
    for key, value in fm.items():
        if key in _NOT_A_RELATION:
            continue
        for item in (value if isinstance(value, list) else [value]):
            m = _DECLARED_REF.match(str(item).strip())
            if m:
                out.add(m.group(1))
    return out


def _demand_supply() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Walk once. (slug -> source pages referencing it, slug -> entity pages referencing it).
    A concept's own page never counts toward its demand/supply.

    SUPPLY COUNTS DECLARED RELATIONS, NOT ONLY PROSE LINKS. It previously counted entity pages
    containing a `[[concepts/<slug>]]` body wikilink, which is how vendors are almost never
    associated with a market here: the relation lives in frontmatter (`segment`, importer-set
    fields), and most vendor pages contain no prose at all. On one vault `ai-security` scored
    supply=2 against 15 entities declaring the relation.

    The undercount is not cosmetic. Supply is the numerator of "thin supply", so it manufactures
    whitespace: the lane surfaced ai-security as an empty market and the model had to reject it
    from its own knowledge of the vendors ("supply=2 is a counting artifact"). A selector that
    hands the model false candidates spends model time on rejection and teaches nothing.
    """
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
        if ns == "sources":
            for slug in set(_CONCEPT_LINK.findall(text)):
                demand[slug].add(rel)
            continue
        for slug in set(_CONCEPT_LINK.findall(text)):
            supply[slug].add(rel)
        m = _FM.match(text)
        if m:
            for slug in _declared_slugs(m.group(1)):
                supply[slug].add(rel)
    return demand, supply


def _has_concept_page(slug: str) -> bool:
    return any((WIKI / "concepts").rglob(f"{slug}.md")) if (WIKI / "concepts").is_dir() else False


def write_selection_manifest(selected: list[str], default_path: Path) -> dict:
    """Write the selected keys and their contract identity, atomically.

    INLINED, not imported, on purpose. This selector is required to be self-contained
    (test_selector_is_self_contained): extension selectors are staged standalone into
    /opt/data/scripts/<id>/ where the engine's shared libs are NOT importable at the repo-relative
    path. Importing the shared helper passed every test in the checkout and raised
    ModuleNotFoundError on the gateway — trading one failing lane for a differently-failing lane.

    Kept byte-compatible with scripts/cron/selection_manifest.write_selection_manifest; the receipt
    checker reads `selected` and `input_digest`, so those two must not drift.
    """
    items = [str(item) for item in selected]
    manifest = {
        "api": 1,
        "selected": items,
        "input_digest": "sha256:" + hashlib.sha256(
            json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "lane_id": os.environ.get("OKENGINE_LANE_ID", ""),
        "contract_digest": os.environ.get("OKENGINE_CONTRACT_DIGEST", ""),
    }
    path = Path(os.environ.get("OKENGINE_SELECTION_MANIFEST", str(default_path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n")
    temp.replace(path)
    return manifest


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
    # written BEFORE the digest, so the receipt has something to verify against even if the agent
    # turn dies partway
    write_selection_manifest([f"concepts/{slug}" for _d, _s, slug in chosen], SELECTION_MANIFEST)
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
