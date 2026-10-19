#!/usr/bin/env python3
"""okengine.lacuna wake-gate (okengine#145).

Picks ONE field to run the 6-step lacuna procedure over, from the vault's REAL concept graph
— defeating the raw prompt's main weakness (mapping the field from a model's averaged recall).

A "field" is a concept cluster: the set of pages that link `[[concepts/<slug>]]`. Its
**density** (distinct referencing pages) is the measurable signal step 6 needs — a thick
fabric means the geometry is well-sampled (a gap there is a strong inference), a thin patch
means extrapolation. We surface only clusters dense enough to map (`min_density`) and not
already analyzed within `reanalyze_days` (rotation across the graph), densest first.

Inquiry nominations (okengine#854): an open okengine.inquiry page may declare the concept `fields`
it bears on; its dossier lane writes them to `inquiry_nominations_path`. Those rank FIRST — the
vault said what it is researching — and may sit below `min_density` down to `inquiry_min_density`,
mapped with the explicit EXTRAPOLATION warning (the same contract as the operator FOCUS override).
They still need a real concept page and respect `reanalyze_days`. Material-source nominations are
unchanged: routing hints that only reorder fields already dense enough.

Prints a human-readable digest (with each cluster's density + namespace breakdown, so the
agent can record `surround_density`), then a final `{"wakeAgent": bool}` line (the cron-plus
wake-gate protocol). LOCAL-ONLY; no writes here — the agent writes the `lacuna/<slug>` page
(and any prediction candidate) via the okengine-write MCP path.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

WIKI = Path(os.environ.get("WIKI_PATH", "/opt/vault")) / "wiki"
MIN_DENSITY = int(os.environ.get("OKENGINE_LACUNA_MIN_DENSITY", "8"))      # config.min_density
REANALYZE_DAYS = int(os.environ.get("OKENGINE_LACUNA_REANALYZE_DAYS", "90"))  # config.reanalyze_days
BATCH = int(os.environ.get("OKENGINE_LACUNA_BATCH_SIZE", "3"))            # config.batch_size
NOMINATIONS_PATH = os.environ.get(
    "OKENGINE_LACUNA_NOMINATIONS_PATH", ".okengine/lacuna-nominations.json"
).strip()
INQUIRY_NOMINATIONS_PATH = os.environ.get(
    "OKENGINE_LACUNA_INQUIRY_NOMINATIONS_PATH", ".okengine/inquiry/lacuna-nominations.json"
).strip()
INQUIRY_MIN_DENSITY = int(os.environ.get("OKENGINE_LACUNA_INQUIRY_MIN_DENSITY", "3"))
NOMINATION_MAX_AGE_DAYS = int(
    os.environ.get("OKENGINE_LACUNA_NOMINATION_MAX_AGE_DAYS", "7")
)
# OPERATOR TOPIC OVERRIDE (config.focus): pin lacuna to ONE concept field this run, bypassing the
# density rank + the reanalyze rotation. Accepts a bare slug, a `concepts/<shard>/<slug>` path, or a
# `[[concepts/…]]` wikilink (all fold to the slug). UNSET (default) keeps the autonomous behavior:
# the densest unanalyzed field, everything considered. A focused field still needs a real concept
# page + at least one referencing page; below min_density it maps with an extrapolation WARNING.
FOCUS = os.environ.get("OKENGINE_LACUNA_FOCUS", "").strip()
# Namespaces whose links are DERIVED views, not evidence, and so never count toward a field's
# density (okengine#855). Density is the signal step 6 uses to call a result inference or
# extrapolation; on one live vault 4 of a field's 7 "referencing pages" were generated dashboards
# and its evidence density was 1. Vault-root pages (index.md, HOT.md, log.md, …) and structural
# names (INDEX*.md, _*.md) are excluded unconditionally.
DENSITY_EXCLUDE = {n.strip() for n in os.environ.get(
    "OKENGINE_LACUNA_DENSITY_EXCLUDE_NAMESPACES", "lacuna,dashboards,operational,briefings"
).split(",") if n.strip()} | {"lacuna"}
_STRUCTURAL_PAGE = re.compile(r"^(?:index(?:-p\d+)?\.md|_.*)\Z", re.IGNORECASE)

# Match `[[concepts/<slug>]]` AND sharded `[[concepts/<shard>/.../<slug>]]` (vaults shard a large
# namespace by leading char, e.g. concepts/s/supply-chain-compromise), capturing the final slug so
# both link forms fold into one cluster. (okengine#145 follow-up — flat-only regex missed sharding.)
_CONCEPT_LINK = re.compile(r"\[\[concepts/(?:[a-z0-9][a-z0-9-]*/)*([a-z0-9][a-z0-9-]*)(?:[#|][^\]]*)?\]\]")
_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def _today() -> str:
    return os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat()


def _cutoff() -> str:
    return (date.fromisoformat(_today()) - timedelta(days=REANALYZE_DAYS)).isoformat()


def _ns(md: Path) -> str:
    """The top-level wiki namespace a page lives in (entities/sources/concepts/…)."""
    rel = md.relative_to(WIKI).parts
    return rel[0] if len(rel) > 1 else ""


def _read_fm(md: Path) -> dict:
    try:
        text = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    m = _FM.match(text)
    if not m:
        return {}
    try:
        import yaml
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_TOMBSTONED = re.compile(r"^status:\s*['\"]?tombstoned\b", re.M | re.I)


def _tombstoned(text: str) -> bool:
    """A retired page is not evidence and not a seat. Tombstones keep their files, so a scanner
    that only reads links would keep counting them: after 18 leaked test-fixture pages were
    tombstoned on a live vault, their links still made a fixture concept the densest field."""
    m = _FM.match(text)
    return bool(m and _TOMBSTONED.search(m.group(1)))


def _slug_of(ref: str) -> str:
    """Reduce a concept reference to its bare slug. Handles the sharded path the schema stores
    (`concepts/s/supply-chain-compromise`), a `[[concepts/…]]` wikilink, or a plain slug — all
    fold to `supply-chain-compromise`. Returns '' for anything that isn't a concept ref."""
    s = str(ref).strip().strip("[]").split("|", 1)[0].split("#", 1)[0].strip()
    if "concepts/" in s:
        s = s.split("concepts/", 1)[1]
    return s.rstrip("/").split("/")[-1]


def _recently_analyzed() -> set[str]:
    """Concept slugs a `lacuna` page has already MAPPED within REANALYZE_DAYS (rotation).

    The analyzed field is declared authoritatively in the page's REQUIRED `field_mapped`
    frontmatter — read THAT, not every `[[concepts/…]]` the page happens to cite. Those
    secondary links are context (a mapped field routinely cites neighbouring concepts), and
    treating them as "analyzed" both (a) let the *mapped* field slip back into the batch when
    it was recorded only as a bare `field_mapped:` path, not a bracketed link, and (b) retired
    unrelated dense fields for 90 days, starving the candidate pool. Legacy pages without
    `field_mapped` fall back to their bracketed links; undated pages fall back to the file
    mtime so the rotation window can actually elapse."""
    cutoff = _cutoff()
    covered: set[str] = set()
    ldir = WIKI / "lacuna"
    if not ldir.is_dir():
        return covered
    for md in ldir.rglob("*.md"):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = _read_fm(md)
        if str(fm.get("type", "")).strip() != "lacuna" or _tombstoned(text):
            continue                       # a retired analysis does not hold its field for 90 days
        when = str(fm.get("updated") or fm.get("created") or "")[:10]
        if not when:                       # undated page: fall back to when the file was written
            try:
                when = date.fromtimestamp(md.stat().st_mtime).isoformat()
            except OSError:
                when = _today()            # unreadable stat: treat as recent (stay excluded)
        if when < cutoff:
            continue                       # old enough to refresh — leave the field eligible
        mapped = fm.get("field_mapped")
        if mapped:
            for ref in (mapped if isinstance(mapped, list) else [mapped]):
                slug = _slug_of(ref)
                if slug:
                    covered.add(slug)
        else:                              # legacy page (no field_mapped): best-effort from links
            covered |= set(_CONCEPT_LINK.findall(text))
    return covered


def _clusters() -> tuple[dict[str, set[str]], dict[str, Counter]]:
    """Walk the vault once. Returns (slug -> referencing page rels, slug -> namespace counts),
    counting a concept only from EVIDENCE: not the lacuna namespace or other derived views
    (DENSITY_EXCLUDE), not vault-root or structural pages, and not the concept's own page (none of
    those should seed the field it belongs to)."""
    refs: dict[str, set[str]] = defaultdict(set)
    by_ns: dict[str, Counter] = defaultdict(Counter)
    for md in WIKI.rglob("*.md"):
        ns = _ns(md)
        if not ns or ns in DENSITY_EXCLUDE or _STRUCTURAL_PAGE.match(md.name):
            continue                       # derived views and structure are not evidence
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _tombstoned(text):
            continue                       # retired pages are not evidence
        rel = md.relative_to(WIKI).as_posix()
        for slug in set(_CONCEPT_LINK.findall(text)):
            if ns == "concepts" and md.stem == slug:
                continue                   # the concept page doesn't count toward its own density
            refs[slug].add(rel)
            by_ns[slug][ns or "(root)"] += 1
    return refs, by_ns


def _has_concept_page(slug: str) -> bool:
    """A LIVE concept page for `slug` exists (a tombstoned one is not the field's seat)."""
    if not (WIKI / "concepts").is_dir():
        return False
    for md in sorted((WIKI / "concepts").rglob(f"{slug}.md")):  # deterministic: same answer every run
        try:
            if not _tombstoned(md.read_text(encoding="utf-8", errors="ignore")):
                return True
        except OSError:
            continue
    return False


def _density_str(counts: Counter) -> str:
    """e.g. '23 links · entities 18 · sources 4 · concepts 1' — the surround_density signal."""
    total = sum(counts.values())
    parts = [f"{ns} {n}" for ns, n in counts.most_common()]
    return f"{total} links · " + " · ".join(parts)


def _fresh_nominations(nominations_path: str | None = None) -> dict[str, dict[str, str]]:
    """Return the newest valid nomination for each concept slug.

    Nominations are operational routing hints, not evidence and not permission to weaken the
    density or review gates.  A pack may write the queue after classifying recent material;
    Lacuna still independently requires a real, dense, not-recently-analyzed concept field.
    """
    source = NOMINATIONS_PATH if nominations_path is None else nominations_path
    if not source:
        return {}
    path = Path(source)
    if not path.is_absolute():
        path = WIKI.parent / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    rows = payload.get("nominations", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    cutoff = date.fromisoformat(_today()) - timedelta(days=NOMINATION_MAX_AGE_DAYS)
    selected: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = _slug_of(row.get("field", ""))
        nominated_at = str(row.get("nominated_at", ""))[:10]
        try:
            when = date.fromisoformat(nominated_at)
        except ValueError:
            continue
        if not slug or when < cutoff or when > date.fromisoformat(_today()):
            continue
        candidate = {
            "field": slug,
            "source": str(row.get("source", "")).strip(),
            "reason": str(row.get("reason", "")).strip(),
            "nominated_at": nominated_at,
        }
        if slug not in selected or nominated_at > selected[slug]["nominated_at"]:
            selected[slug] = candidate
    return selected


def main() -> int:
    if not WIKI.is_dir():
        print(json.dumps({"wakeAgent": False}))
        return 0

    refs, by_ns = _clusters()
    recently = _recently_analyzed()
    nominations = _fresh_nominations()
    inquiry_noms = _fresh_nominations(INQUIRY_NOMINATIONS_PATH)

    cands = []
    for slug, pages in refs.items():
        density = len(pages)
        # An inquiry-declared field may be thinner than the autonomous floor (mapped with the
        # extrapolation warning below); everything else needs the full density.
        if density < (INQUIRY_MIN_DENSITY if slug in inquiry_noms else MIN_DENSITY):
            continue
        if slug in recently:
            continue
        if not _has_concept_page(slug):
            continue                       # require a real, named field to map (not a dangling link)
        cands.append((density, slug))
    cands.sort(key=lambda c: (0 if c[1] in inquiry_noms else 1 if c[1] in nominations else 2,
                              -c[0], c[1]))

    print("=== lacuna field-selection wake-gate ===")
    print(f"  vault: {WIKI}")
    print(f"  concept clusters >= {MIN_DENSITY} refs: "
          f"{sum(1 for s, p in refs.items() if len(p) >= MIN_DENSITY and _has_concept_page(s))}")
    print(f"  excluded (analyzed since {_cutoff()}): {len(recently)}")
    print(f"  eligible fields: {len(cands)}")
    eligible_nominations = sum(1 for _, slug in cands if slug in nominations)
    print(f"  fresh material nominations: {len(nominations)} ({eligible_nominations} eligible)")
    eligible_inquiry = sum(1 for _, slug in cands if slug in inquiry_noms)
    print(f"  inquiry field nominations: {len(inquiry_noms)} ({eligible_inquiry} eligible, "
          f"floor {INQUIRY_MIN_DENSITY})")

    # FOCUS override: map an operator-pinned field, bypassing the density rank + rotation. Unset
    # leaves the autonomous "densest unanalyzed field" path below untouched (everything considered).
    if FOCUS:
        focus = _slug_of(FOCUS)
        density = len(refs.get(focus, ()))
        print(f"\n  FOCUS: concepts/{focus} (operator override — bypasses density rank + "
              f"{REANALYZE_DAYS}d rotation)")
        if not focus or not _has_concept_page(focus):
            print(f"  → SKIP: no concept page for '{focus or FOCUS}' — create the field anchor "
                  f"(concepts/**/{focus or '<slug>'}.md) first; lacuna maps a NAMED field, not a "
                  "dangling link.")
            print(json.dumps({"wakeAgent": False}))
            return 0
        if density == 0:
            print(f"  → SKIP: '{focus}' has a page but NO referencing pages — the field is empty. "
                  f"Tag pages with [[concepts/{focus}]] to build it, then re-run.")
            print(json.dumps({"wakeAgent": False}))
            return 0
        if density < MIN_DENSITY:
            print(f"  ⚠ density {density} < min_density {MIN_DENSITY}: thinly sampled — step 6 will be "
                  "EXTRAPOLATION, not inference. Proceeding because FOCUS is set.")
        print(f"  field density: {density} referencing pages\n")
        print("=== field to analyze (FOCUS) ===")
        print("Run the 6-step lacuna procedure over THIS field. Record its density as "
              "`surround_density`. DEFER only if you genuinely cannot name a force keeping a cell empty.\n")
        print(f"## concept: {focus}  ({density} referencing pages)")
        print(f"  field anchor: `[[concepts/{focus}]]`  ·  "
              f"surround_density: `{_density_str(by_ns.get(focus, Counter()))}`\n")
        print(json.dumps({"wakeAgent": True}))
        return 0

    if not cands:
        print("  → SKIP: no dense, unanalyzed field to map")
        print(json.dumps({"wakeAgent": False}))
        return 0

    chosen = cands[:BATCH]
    print(f"  batch: {len(chosen)} of {len(cands)}\n")
    print("=== fields to analyze ===")
    print("Run the 6-step lacuna procedure over ONE of the fields below (the densest you can "
          "genuinely map). Record the cluster's density as `surround_density`. DEFER the whole "
          "field if you cannot name a real force keeping a cell empty — coverage is not a goal.\n")
    for i, (density, slug) in enumerate(chosen, 1):
        print(f"## {i}. concept: {slug}  ({density} referencing pages)")
        print(f"  field anchor: `[[concepts/{slug}]]`  ·  surround_density: `{_density_str(by_ns[slug])}`\n")
        inquiry = inquiry_noms.get(slug)
        if inquiry:
            print(f"  inquiry nomination — {inquiry['source']}: {inquiry['reason']}")
            if density < MIN_DENSITY:
                print(f"  ⚠ density {density} < min_density {MIN_DENSITY}: thinly sampled — step 6 "
                      "will be EXTRAPOLATION, not inference. Mapped because an open inquiry "
                      "declared this field; state the extrapolation in the page.")
            print()
        nomination = nominations.get(slug)
        if nomination:
            print("  material-source nomination (routing context only; verify it independently):")
            print(f"    nominated_at: {nomination['nominated_at']}")
            if nomination["source"]:
                print(f"    source: {nomination['source']}")
            if nomination["reason"]:
                print(f"    reason: {nomination['reason']}")
            print()

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
