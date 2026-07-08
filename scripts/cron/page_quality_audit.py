#!/usr/bin/env python3
"""page_quality_audit.py — per-page content-quality auditor.

The vault audits structure (lint), predictions, and outputs (critic-flagship),
but nothing scored individual entity/concept pages for SUBSTANCE. This is the
gap: ~20-25% of entity pages are thin stubs and nothing flagged them per-page.

Deterministic scorer over wiki/entities + wiki/concepts. For each page it
computes body_words / section_count(##) / source_count / days_since_update and
assigns a tier (rubric grounded in the actual 6k-entity / 3k-concept
distribution):

  STUB  <50 words, or 0 sections AND 0 sources
  THIN  <150 words AND <2 sections AND <3 sources
  OK    150-299 words, or 2-3 sections AND >=2 sources
  STRONG 300-499 words, or >=4 sections AND >=5 sources
  ENCYCLOPEDIC >=500 words, or >=6 sections AND >=8 sources

Type-aware: when the pack declares depth-critical types, a page of such a type
with <2 sections can't rank above THIN (no demotion when the pack declares none).
DELIBERATE non-content pages (link_stub:true, "Alias of"/"redirect target"
body markers) are classed `redirect` and EXCLUDED from the deficient queue —
they're correct as-is. Pure publisher ENTITIES (type:media, or `publisher`-
tagged and not a declared canonical type) are classed `publisher` and likewise
EXCLUDED: they're citation hubs cited BY their own articles, so enrichment
can't deepen them. Scoped to entities and the explicit `publisher` tag so
that concepts ABOUT media (content-farms, reporting-ethics) and sources that
also publish (e.g. a wire service or research lab) stay enrichable. 0-byte/empty-body
pages are a distinct HARD `empty` class. Writes a quality dashboard (tier distribution + a prioritized enrich
queue ranked by inbound-link count, so heavily-referenced thin pages surface
first) + a one-row/day snapshot read by kb-health. Script-only, no LLM.

Page types and the depth-critical subset are PACK inputs (schema.yaml), read at
runtime — the engine ships no domain taxonomy.

Env:  WIKI_PATH (default /opt/vault) ; PQ_QUEUE_SIZE (default 40)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
OP_DIR = WIKI / "operational"
DASH = WIKI / "dashboards" / "page-quality.md"
SNAP = OP_DIR / "page-quality-snapshots.md"
QUEUE_SIZE = int(os.environ.get("PQ_QUEUE_SIZE", "40"))

# Namespaces to audit = the vault's declared knowledge namespaces (the COMPOSED schema on a
# multipack vault) minus excluded/derived dirs — NOT a hardcoded ('entities','concepts'), which
# silently skipped every synthesized namespace on a composed vault (okcti: threat-actors,
# security-incidents, cves, …), understating stub/thin debt with no warning. Falls back to the pair
# only when the schema declares no knowledge namespaces.
_PQ_MS = schema_lib.merged_schema(VAULT)
AUDITED_DIRS = tuple(sorted(
    schema_lib.knowledge_namespaces(_PQ_MS) - schema_lib.excluded_dirs(_PQ_MS))) or ("entities", "concepts")
DEFICIENT = {"empty", "stub", "thin"}

# Page types are PACK inputs (schema.yaml), read at runtime.
#   ENRICHABLE_TYPES — analytical types we always want deepened, never
#     publisher-excluded (e.g. a wire service or research lab that publishes
#     research AND is itself a real analytical subject). Defaults to the pack's
#     declared canonical types; an empty set means "treat every type as
#     enrichable" (no publisher-exclusion-by-type).
#   DEPTH_CRITICAL — the subset where a thin body is a real deficiency even at a
#     moderate word count. Pack-supplied via schema `depth_critical_types:`;
#     default empty ⇒ no depth demotion.
_SCHEMA = schema_lib.governing_schema(VAULT)
ENRICHABLE_TYPES = schema_lib.canonical_types(_SCHEMA)
_REFPOL = schema_lib.reference_policy(_SCHEMA)  # reference-catalog imports → excluded from deficient
_dc = _SCHEMA.get("depth_critical_types")
DEPTH_CRITICAL = {str(x) for x in _dc} if isinstance(_dc, (list, tuple, set)) else set()

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_H1_RE = re.compile(r"^#\s+.+$", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_REDIRECT_RE = re.compile(
    r"alias of|redirect target|this page (?:exists as|is) a redirect|"
    r"link companion|link[- ]stub|companion to \[\[", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def read_fm_body(path: Path) -> tuple[dict, str]:
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return {}, ""
    m = _FM_RE.match(txt)
    if not m:
        return {}, txt
    try:
        fm = schema_lib.fast_load(m.group(1))
    except yaml.YAMLError:
        fm = None
    return (fm if isinstance(fm, dict) else {}), txt[m.end():]


def all_pages():
    for sub in AUDITED_DIRS:
        d = WIKI / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            if not p.is_file():
                continue
            if any(".bak." in part or part.startswith(".")
                   or part.startswith("_archive") or part.startswith("_")
                   for part in p.parts):
                continue
            yield sub, p


def _parse_date(v):
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        m = _DATE_RE.search(v)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def _body_words(body: str) -> int:
    b = _H1_RE.sub("", body, count=1)
    b = _WIKILINK_RE.sub(" ", b)
    b = re.sub(r"[#*`>_|\-\[\]]", " ", b)
    return len(b.split())


def build_inbound(pages: list) -> Counter:
    """Inbound wikilink count per page-stem, over the whole vault body text —
    used to prioritize the enrich queue (a heavily-cited thin page matters more)."""
    inbound: Counter = Counter()
    for p in WIKI.rglob("*.md"):
        if any(".bak." in part or part.startswith(".") or part.startswith("_archive")
               for part in p.parts):
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        m = _FM_RE.match(txt)
        body = txt[m.end():] if m else txt
        for tgt in _WIKILINK_RE.findall(body):
            inbound[tgt.strip().split("/")[-1]] += 1
    return inbound


def classify(sub: str, fm: dict, body: str, today: date) -> dict:
    words = _body_words(body)
    sections = len(_H2_RE.findall(body))
    src = fm.get("sources")
    nsrc = len(src) if isinstance(src, list) else 0
    ptype = str(fm.get("type") or "")
    upd = _parse_date(fm.get("updated"))
    age = (today - upd).days if upd else None

    is_redirect = bool(fm.get("link_stub")) or bool(_REDIRECT_RE.search(body[:400]))
    empty = not body.strip()

    # Pure publishers / news outlets are citation HUBS, not content pages —
    # heavily cited (high inbound) but their citing sources are articles BY them,
    # not material ABOUT them, so enrichment can't deepen them. Class as
    # `publisher` and exclude from the deficient queue, like redirects.
    # ENTITIES ONLY: a concept tagged `media` is ABOUT media (e.g. content-farms,
    # reporting-ethics) — a real analytical page, never a publisher. And require
    # the explicit `publisher` tag (or type:media), not the broad `media`/`news`
    # topical tags, so media-adjacent orgs aren't wrongly dropped.
    # When the pack declares no canonical types, ENRICHABLE_TYPES is empty and we
    # treat every type as enrichable — so a `publisher`-tagged page is only
    # publisher-excluded when its type is NOT one the pack declared as enrichable.
    tags = [str(t).lower() for t in fm.get("tags")] if isinstance(fm.get("tags"), list) else []
    is_publisher = sub == "entities" and (
        ptype == "media"
        or ("publisher" in tags and bool(ENRICHABLE_TYPES) and ptype not in ENRICHABLE_TYPES))

    # Reference-catalog imports (pack-declared: CVE / ATT&CK / encyclopedia records) are
    # deterministic reference data, not enrichable prose — a thin CVE record is correct as-is,
    # not a stub to deepen. Classed `reference` and excluded from the deficient queue.
    is_reference = schema_lib.is_reference_page(fm, _REFPOL)

    # Highest qualifying tier wins (checked strongest-first so the conditions
    # are monotonic). Each tier: word-count OR (structure AND sourcing).
    if empty:
        tier = "empty"
    elif is_redirect:
        tier = "redirect"
    elif is_publisher:
        tier = "publisher"
    elif is_reference:
        tier = "reference"
    elif words < 50:
        tier = "stub"
    elif sections == 0 and nsrc == 0:
        # prose but no structure AND no sources — needs sourcing/structure, not
        # creation. "thin" even when wordy (e.g. a 600-word page nobody cited).
        tier = "thin"
    elif words >= 500 or (sections >= 6 and nsrc >= 8):
        tier = "encyclopedic"
    elif words >= 300 or (sections >= 4 and nsrc >= 5):
        tier = "strong"
    elif words >= 150 or (sections >= 2 and nsrc >= 2):
        tier = "ok"
    else:
        tier = "thin"

    # type-aware: depth-critical entities can't exceed THIN with <2 sections
    if tier in ("ok", "strong", "encyclopedic") and ptype in DEPTH_CRITICAL and sections < 2:
        tier = "thin"

    return {"tier": tier, "words": words, "sections": sections, "sources": nsrc,
            "type": ptype, "age": age}


def main() -> int:
    today = datetime.now(timezone.utc).date()
    pages = list(all_pages())
    inbound = build_inbound(pages)

    by_tier = {"entities": Counter(), "concepts": Counter()}
    deficient = []  # (inbound, sub, stem, tier, words, sections, sources, type)
    for sub, p in pages:
        fm, body = read_fm_body(p)
        c = classify(sub, fm, body, today)
        by_tier[sub][c["tier"]] += 1
        if c["tier"] in DEFICIENT:
            deficient.append((inbound.get(p.stem, 0), sub, p.stem, c["tier"],
                              c["words"], c["sections"], c["sources"], c["type"]))
    deficient.sort(key=lambda r: (-r[0], r[3] != "empty", r[4]))

    tot = {s: sum(by_tier[s].values()) for s in AUDITED_DIRS}
    n_def = len(deficient)
    n_empty = sum(by_tier[s]["empty"] for s in AUDITED_DIRS)

    def pct(s, t):
        return 100 * by_tier[s][t] / tot[s] if tot[s] else 0.0

    # ── dashboard ───────────────────────────────────────────────────
    TIERS = ["empty", "stub", "thin", "ok", "strong", "encyclopedic", "redirect", "publisher", "reference"]
    L = ["---", "type: dashboard", "title: Page content quality",
         f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", "generator: scripts/cron/page_quality_audit.py",
         "---", "", "# Page content quality", "",
         "_Per-page content-depth audit of entity + concept pages. "
         "Tiers from body word count / `##` sections / `sources:` count, "
         "type-aware. Deliberate redirects/aliases are excluded from the enrich "
         "queue. The enrich queue is ranked by inbound links — a heavily-cited "
         "thin page is the highest-value page to deepen._", "",
         "## Tier distribution", "",
         "| tier | entities | % | concepts | % |", "|---|--:|--:|--:|--:|"]
    for t in TIERS:
        L.append(f"| {t} | {by_tier['entities'][t]} | {pct('entities', t):.0f}% "
                 f"| {by_tier['concepts'][t]} | {pct('concepts', t):.0f}% |")
    L.append("")
    L.append(f"**Deficient (empty/stub/thin; excl. redirect/publisher/reference):** {n_def} · "
             f"**empty (0-byte/no body):** {n_empty}")
    L.append("")
    L.append(f"## Enrich queue — top {QUEUE_SIZE} by inbound links")
    L.append("")
    L.append("Weakest pages that are actually referenced — deepen these first.")
    L.append("")
    L.append("| inbound | page | tier | words | ## | src |")
    L.append("|--:|---|---|--:|--:|--:|")
    for inb, sub, stem, tier, w, sec, ns, _ty in deficient[:QUEUE_SIZE]:
        L.append(f"| {inb} | [[{sub}/{stem}]] | {tier} | {w} | {sec} | {ns} |")
    L.append("")
    DASH.parent.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L) + "\n")

    # machine-readable enrich queue for a future enrichment drain
    (OP_DIR / "page-quality-queue.json").write_text(json.dumps(
        [{"page": f"{s}/{st}", "tier": ti, "inbound": ib, "words": w,
          "sections": sec, "sources": ns}
         for ib, s, st, ti, w, sec, ns, _t in deficient[:200]], indent=0))

    # ── snapshot ────────────────────────────────────────────────────
    OP_DIR.mkdir(parents=True, exist_ok=True)
    header = ("---\ntype: dashboard\ntitle: Page-quality snapshots\n---\n\n"
              "# Page-quality snapshots\n\n"
              "| date | deficient | empty | ent-stub% | ent-thin% | con-stub% | con-thin% |\n"
              "|---|---|---|---|---|---|---|\n")
    if not SNAP.exists():
        SNAP.write_text(header)
    row = (f"| {today.isoformat()} | {n_def} | {n_empty} "
           f"| {pct('entities','stub'):.0f} | {pct('entities','thin'):.0f} "
           f"| {pct('concepts','stub'):.0f} | {pct('concepts','thin'):.0f} |")
    lines = [ln for ln in SNAP.read_text(errors="replace").splitlines()
             if not ln.startswith(f"| {today.isoformat()} |")]
    SNAP.write_text("\n".join(lines).rstrip() + "\n" + row + "\n")

    print("=== page-quality-audit ===")
    for s in AUDITED_DIRS:
        dist = " ".join(f"{t}={by_tier[s][t]}" for t in TIERS if by_tier[s][t])
        print(f"  {s}: {tot[s]} pages — {dist}")
    print(f"  deficient: {n_def}  empty: {n_empty}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
