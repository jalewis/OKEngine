#!/usr/bin/env python3
"""Daily refresh of wiki/dashboards/source-staleness.md — closes okengine#11.

Walks every source page in `wiki/sources/`, computes an effective_score
that decays the frozen source rating by source_kind-specific half-life
(see `source_decay` library). Then walks entity/concept/prediction pages
and flags those whose *primary citations* are ALL stale (effective_score
< 0.5) — the "stale anchors" surface called for in the issue.

Primary citations:
  - Concepts / entities: first PRIMARY_CITATION_DEPTH entries of `sources:`
  - Predictions:        all entries of `basis:` (predictions don't have
                        a "primary" subset — every basis source is an anchor)

Pure script — wakeAgent=false. Idempotent on a given day.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from source_decay import (  # type: ignore[import]
    STALE_THRESHOLD, compute_for, HALF_LIVES, DEFAULT_HALF_LIFE, scale_from_enum,
)
import schema_lib  # noqa: E402
import tz_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
_SCHEMA = schema_lib.governing_schema(VAULT)
# Pack-declared canonical entity types (schema.yaml). Empty ⇒ accept any type.
ENTITY_TYPES = schema_lib.canonical_types(_SCHEMA)


def _ordered_enum(schema: dict, field: str) -> list:
    """The ORDERED grading vocabulary a pack declares for `field` via the schema
    `field_enums` -> `enums` indirection (or an inline list). [] if none is declared — source_decay
    then falls back to the engine Admiralty default. base-schema states the reliability/credibility
    scheme is a PACK enum, so this is how a non-Admiralty pack's grades get scored correctly instead
    of silently laundered to neutral (invariant-audit #351)."""
    fe = (schema.get("field_enums") or {}).get(field)
    ev = fe.get("enum") if isinstance(fe, dict) else (fe if isinstance(fe, list) else None)
    if isinstance(ev, list):
        return [str(v) for v in ev]
    if isinstance(ev, str):
        named = (schema.get("enums") or {}).get(ev)
        return [str(v) for v in named] if isinstance(named, list) else []
    return []


# Grading scales driven by the governing schema; None -> source_decay uses the engine Admiralty
# default (byte-identical to the pre-#351 behavior for packs that declare no reliability/credibility enum).
_REL_SCALE = scale_from_enum(_ordered_enum(_SCHEMA, "reliability")) or None
_CRED_SCALE = scale_from_enum(_ordered_enum(_SCHEMA, "credibility")) or None
DASH_PATH = VAULT / "wiki" / "dashboards" / "source-staleness.md"
PRIMARY_CITATION_DEPTH = int(os.environ.get("DECAY_PRIMARY_CITATION_DEPTH", "3"))
TOP_PAGES_PER_SECTION = int(os.environ.get("DECAY_TOP_PAGES", "30"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)$", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]*)?\]\]")


@dataclass
class SourceScore:
    rel_path: str           # "sources/2026-05-05-foo"
    effective_score: float
    is_stale: bool
    source_kind: str | None
    age_days: int
    base_score: float
    reliability_oov: bool = False   # grade present but outside the active vocabulary (scored neutral)
    credibility_oov: bool = False


@dataclass
class Anchor:
    rel_path: str           # "concepts/foo" / "entities/bar" / "predictions/baz"
    title: str
    segment: str            # concepts | entities | predictions
    citations: list[str]    # source rel_paths cited as primary
    citation_scores: list[float]
    file_updated: date | None


# ─── helpers ──────────────────────────────────────────────────────────


def parse_fm_and_body(path: Path) -> tuple[dict, str]:
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return {}, ""
    m = _FM_RE.match(txt)
    if not m:
        return {}, txt
    body = txt[m.end():]
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, body
    return (fm if isinstance(fm, dict) else {}), body


def to_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def normalize_link(s) -> str | None:
    """Canonical `<namespace>/<slug>` key for a citation — collapses the by-date / by-letter
    partition segments (`sources/<yr>/<mo>/<slug>`, `entities/<L>/<slug>`) to the SAME stem key the
    score map is built with (`score_all_sources` keys `sources/<stem>`). Without the collapse a
    citation written as the full partition path (`sources/2026/07/foo`) never matched the
    `sources/foo` score entry, so staleness was silently never applied to any date-partitioned
    source — the common case. Stem-keying is partition-independent: a flat OR a partitioned citation
    to the same slug both resolve."""
    if not isinstance(s, str):
        return None
    s = s.strip().strip('"').strip("'")
    m = WIKILINK_RE.search(s)
    if m:
        s = m.group(1)
    s = s.strip()
    if s.endswith(".md"):
        s = s[:-3]
    parts = [seg for seg in s.split("/") if seg]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[-1]}"       # <namespace>/<slug>; partition/shard middle dropped
    return (parts[0] if parts else None)


# ─── source scoring ──────────────────────────────────────────────────


def score_all_sources(today: date) -> dict[str, SourceScore]:
    sources_dir = VAULT / "wiki" / "sources"
    if not sources_dir.is_dir():
        return {}
    out: dict[str, SourceScore] = {}
    for p in sources_dir.rglob("*.md"):
        if p.name.startswith("_"):
            continue
        fm, _ = parse_fm_and_body(p)
        if fm.get("type") != "source":
            continue
        # Effective-date for decay: prefer published over ingested. We take
        # min(filename_date, published, ingested) for freshness — the
        # earliest meaningful date so retroactive imports of old material
        # decay properly.
        candidates = [to_date(fm.get("published")), to_date(fm.get("ingested"))]
        # Filename-date heuristic for sources missing both
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", p.stem)
        if m:
            candidates.append(to_date(m.group(1)))
        valid = [d for d in candidates if d]
        source_date = min(valid) if valid else None

        rel_path = f"sources/{p.stem}"
        score = compute_for(
            reliability=fm.get("reliability"),
            credibility=fm.get("credibility"),
            source_kind=fm.get("source_kind"),
            source_date=source_date,
            today=today,
            reliability_scale=_REL_SCALE,
            credibility_scale=_CRED_SCALE,
        )
        out[rel_path] = SourceScore(
            rel_path=rel_path,
            effective_score=score["effective_score"],
            is_stale=score["is_stale"],
            source_kind=score["source_kind"],
            age_days=score["age_days"],
            base_score=score["base_score"],
            reliability_oov=score["reliability_oov"],
            credibility_oov=score["credibility_oov"],
        )
    return out


# ─── anchor pages ─────────────────────────────────────────────────────


def _primary_citations(fm: dict, segment: str) -> list[str]:
    """Return rel_paths of the page's primary citations."""
    if segment == "predictions":
        raw = fm.get("basis") or []
    else:
        raw = fm.get("sources") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for s in raw:
        link = normalize_link(s)
        if link and link.startswith("sources/"):
            out.append(link)
    return out[:PRIMARY_CITATION_DEPTH] if segment != "predictions" else out


def discover_anchors(scores: dict[str, SourceScore]) -> list[Anchor]:
    out: list[Anchor] = []
    for segment in ("concepts", "entities", "predictions"):
        d = VAULT / "wiki" / segment
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):   # rglob: entities/concepts may be sharded (entities/<type>/<letter>/)
            if p.name.startswith("_") or p.name == "INDEX.md" or p.name.startswith("INDEX-"):
                continue
            fm, body = parse_fm_and_body(p)
            t = fm.get("type")
            if segment == "predictions" and t != "prediction":
                continue
            # Entity/concept type filter is PACK-driven: ENTITY_TYPES is the pack's
            # declared canonical type set (empty ⇒ accept any). Either way, skip
            # pure-text pages whose `type` isn't a string.
            if segment in ("entities", "concepts"):
                if ENTITY_TYPES and t not in ENTITY_TYPES:
                    if not isinstance(t, str):
                        continue
                elif not isinstance(t, str):
                    continue
            cites = _primary_citations(fm, segment)
            if not cites:
                continue
            scored_cites = [scores.get(c) for c in cites]
            scored_cites = [s for s in scored_cites if s is not None]
            if not scored_cites:
                continue
            h1 = _H1_RE.search(body)
            title = (h1.group(1).strip() if h1 else p.stem.replace("-", " ").title())
            out.append(Anchor(
                rel_path=f"{segment}/{p.stem}",
                title=title,
                segment=segment,
                citations=[s.rel_path for s in scored_cites],
                citation_scores=[s.effective_score for s in scored_cites],
                file_updated=to_date(fm.get("updated")),
            ))
    return out


# ─── render ──────────────────────────────────────────────────────────


def _wikilink(rel_path: str, title: str | None = None) -> str:
    # Pipe escaped for markdown-table-cell safety. Both current call sites
    # are table rows; if you add a non-table caller, the `\|` will still
    # render correctly as a wikilink in Obsidian.
    if title:
        return f"[[{rel_path}\\|{title}]]"
    return f"[[{rel_path}]]"


def _band(score: float) -> str:
    if score >= 0.85:
        return "0.85-1.00 (fresh, high-quality)"
    if score >= 0.70:
        return "0.70-0.85 (fresh)"
    if score >= 0.50:
        return "0.50-0.70 (current)"
    if score >= 0.30:
        return "0.30-0.50 (decaying)"
    if score >= 0.10:
        return "0.10-0.30 (stale)"
    return "0.00-0.10 (very stale)"


def render_dashboard(scores: dict[str, SourceScore], anchors: list[Anchor], today: date) -> str:
    n_total = len(scores)
    n_stale = sum(1 for s in scores.values() if s.is_stale)
    oov = sorted(s.rel_path for s in scores.values() if s.reliability_oov or s.credibility_oov)
    band_counts: Counter = Counter(_band(s.effective_score) for s in scores.values())
    by_kind: dict[str, list[float]] = defaultdict(list)
    for s in scores.values():
        by_kind[s.source_kind or "(unset)"].append(s.effective_score)

    # Stale anchors: ALL primary citations < STALE_THRESHOLD
    stale_anchors = [
        a for a in anchors
        if a.citation_scores and all(score < STALE_THRESHOLD for score in a.citation_scores)
    ]
    weak_anchors = [
        a for a in anchors
        if a.citation_scores
        and not all(score < STALE_THRESHOLD for score in a.citation_scores)
        and (sum(a.citation_scores) / len(a.citation_scores)) < STALE_THRESHOLD
    ]

    L: list[str] = []
    L.append("---")
    L.append("type: dashboard")
    L.append("title: Source staleness — effective-score decay")
    L.append(f"created: {today.isoformat()}")
    L.append(f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append("---")
    L.append("")
    L.append("# Source staleness — effective-score decay")
    L.append("")
    L.append(
        "Source-quality decay applied to every page in `wiki/sources/`. The "
        "frozen source rating (`reliability` A-F, `credibility` 1-6) stays "
        "correct AT INGEST TIME — what this dashboard computes is the "
        "*effective_score for current claims*, which decays exponentially with "
        "age via a half-life chosen by `source_kind`. "
        "Refreshed daily by `scripts/cron/select_source_staleness.py`."
    )
    L.append("")
    L.append("**Decay function:** `effective_score = base × 0.5^(age_days / half_life)`. "
             "`base` is the mean of reliability and credibility factors mapped 0-1 "
             "(A→1.00, B→0.85, …, F→0.25; 1→1.00, 2→0.85, …, 6→0.30). "
             "`age_days` is computed from `min(published, ingested, filename_date)` to handle "
             "retroactive bulk imports correctly.")
    L.append("")
    if oov:
        # Grades present on the page but OUTSIDE the active vocabulary (the pack's declared enum, or
        # the engine Admiralty default when the pack declares none) score NEUTRAL — surfaced here
        # rather than silently laundered to 'average' (invariant-audit #351). Fix: declare the pack's
        # reliability/credibility `field_enums`, or correct the grade.
        L.append(f"> ⚠ **{len(oov)} source(s) carry an unrecognized reliability/credibility grade** "
                 f"(scored neutral 0.5). Declare the pack's grading `field_enums` or fix the grade: "
                 + ", ".join(f"`{r}`" for r in oov[:10]) + (" …" if len(oov) > 10 else ""))
        L.append("")
    L.append("## Half-lives in use")
    L.append("")
    L.append("| `source_kind` | half-life (days) | half-life (years) |")
    L.append("|---|---:|---:|")
    for kind, hl in sorted(HALF_LIVES.items(), key=lambda kv: -kv[1]):
        L.append(f"| `{kind}` | {hl} | {hl/365:.2f} |")
    L.append(f"| _default (unknown source_kind)_ | {DEFAULT_HALF_LIFE} | {DEFAULT_HALF_LIFE/365:.2f} |")
    L.append("")
    L.append("Override via `DECAY_HL_FILING`, `DECAY_HL_REPORT`, … env vars on the cron job. "
             "`DECAY_STALE_THRESHOLD` (default 0.5) sets the stale cutoff.")
    L.append("")

    # Distribution
    L.append("## Effective-score distribution")
    L.append("")
    L.append(f"**{n_total}** scored sources · **{n_stale}** below {STALE_THRESHOLD:.2f} threshold "
             f"({n_stale*100//max(1,n_total)}%).")
    L.append("")
    L.append("| Band | Count |")
    L.append("|---|---:|")
    band_order = [
        "0.85-1.00 (fresh, high-quality)",
        "0.70-0.85 (fresh)",
        "0.50-0.70 (current)",
        "0.30-0.50 (decaying)",
        "0.10-0.30 (stale)",
        "0.00-0.10 (very stale)",
    ]
    for band in band_order:
        L.append(f"| {band} | {band_counts.get(band, 0)} |")
    L.append("")

    # By source_kind
    L.append("## Average effective_score by `source_kind`")
    L.append("")
    L.append("| `source_kind` | n | avg effective | avg base | mean age (days) |")
    L.append("|---|---:|---:|---:|---:|")
    for kind in sorted(by_kind.keys()):
        kind_sources = [s for s in scores.values() if (s.source_kind or "(unset)") == kind]
        avg_eff = sum(s.effective_score for s in kind_sources) / len(kind_sources)
        avg_base = sum(s.base_score for s in kind_sources) / len(kind_sources)
        avg_age = sum(s.age_days for s in kind_sources) / len(kind_sources)
        L.append(f"| `{kind}` | {len(kind_sources)} | {avg_eff:.3f} | {avg_base:.3f} | {avg_age:.0f} |")
    L.append("")

    # Stale anchors — the headline finding
    L.append("## Stale anchors — pages whose primary citations are all stale")
    L.append("")
    L.append(
        f"Pages where every primary citation has effective_score < {STALE_THRESHOLD:.2f}. "
        f"For concepts/entities, *primary* = first {PRIMARY_CITATION_DEPTH} entries of "
        f"`sources:`; for predictions, every entry of `basis:`. These are the pages most "
        "at risk of carrying outdated claims as if they were current — operator should "
        "refresh anchors with newer material or move the page into a historical / "
        "archived context."
    )
    L.append("")
    L.append(f"**Total: {len(stale_anchors)} pages.**")
    L.append("")
    by_segment_stale: dict[str, list[Anchor]] = defaultdict(list)
    for a in stale_anchors:
        by_segment_stale[a.segment].append(a)
    for segment in ("concepts", "entities", "predictions"):
        seg_items = by_segment_stale.get(segment, [])
        L.append(f"### `{segment}` ({len(seg_items)})")
        L.append("")
        if not seg_items:
            L.append(f"_No stale anchors in `{segment}`._")
            L.append("")
            continue
        L.append("| Page | Primary citations | Avg effective | Last updated |")
        L.append("|---|---:|---:|---|")
        seg_items.sort(key=lambda a: sum(a.citation_scores) / max(1, len(a.citation_scores)))
        for a in seg_items[:TOP_PAGES_PER_SECTION]:
            avg = sum(a.citation_scores) / len(a.citation_scores)
            updated = a.file_updated.isoformat() if a.file_updated else "—"
            L.append(f"| {_wikilink(a.rel_path, a.title)} | {len(a.citation_scores)} | {avg:.3f} | {updated} |")
        if len(seg_items) > TOP_PAGES_PER_SECTION:
            L.append(f"| _… and {len(seg_items) - TOP_PAGES_PER_SECTION} more_ | | | |")
        L.append("")

    # Weak anchors — average below threshold but not all-stale
    L.append("## Weak anchors — average primary-citation score below threshold")
    L.append("")
    L.append(
        f"Pages where the *mean* primary-citation effective_score is below {STALE_THRESHOLD:.2f} "
        "but at least one citation is still fresh. Lower priority than stale anchors but "
        "worth refreshing when material newer information lands."
    )
    L.append("")
    L.append(f"**Total: {len(weak_anchors)} pages** (showing top {TOP_PAGES_PER_SECTION}).")
    L.append("")
    if not weak_anchors:
        L.append("_None._")
    else:
        L.append("| Page | Segment | Citations | Avg effective | Last updated |")
        L.append("|---|---|---:|---:|---|")
        weak_anchors.sort(key=lambda a: sum(a.citation_scores) / max(1, len(a.citation_scores)))
        for a in weak_anchors[:TOP_PAGES_PER_SECTION]:
            avg = sum(a.citation_scores) / len(a.citation_scores)
            updated = a.file_updated.isoformat() if a.file_updated else "—"
            L.append(f"| {_wikilink(a.rel_path, a.title)} | `{a.segment}` | {len(a.citation_scores)} | {avg:.3f} | {updated} |")
    L.append("")

    L.append("## How to read this dashboard")
    L.append("")
    L.append("1. **Stale anchors** is the actionable section. A concept page whose first 3 sources are all from 2015-2018 is carrying claims as if they were current. Refresh with newer material or archive the page.")
    L.append("2. **Weak anchors** is one tier softer: at least one fresh citation present, but the average is below threshold. Re-prioritize to fresh when the underlying topic is alive.")
    L.append("3. **Distribution by band** is a corpus-health snapshot. If `0.85-1.00` (fresh + high-quality) is shrinking quarter-over-quarter, the wiki is becoming a historical archive rather than a forward-looking intelligence layer.")
    L.append("4. **Operator override**: if a specific old source is load-bearing for a current claim despite the decay (e.g. a still-canonical primary doc), call that out inline on the citing page — the decay is an automatic *prior*, not a final judgment.")
    L.append("")
    L.append("## Convention")
    L.append("")
    L.append("Vault `CLAUDE.md` documents the per-`source_kind` half-lives. The frozen source-rating score on each source page is correct at ingest and never modified; this dashboard computes the *current-claim* effective score on every refresh. To override the decay for a specific claim, cite the source explicitly on the citing page with a note like `**Anchored to [[sources/...]] (effective_score=X.XX) but operator-confirmed still load-bearing as of YYYY-MM-DD.**` — the decay system flags candidates for review; operator decides.")
    L.append("")
    L.append("## Source")
    L.append("")
    L.append("- **Refresh script:** `scripts/cron/select_source_staleness.py` (cron-plus, daily 02:30 UTC)")
    L.append("- **Library:** `scripts/cron/source_decay.py` — half-lives + scoring math")
    L.append("- **Related:** [[prediction-backtest]] (calibration), [[contradictions]] (current self-disagreement).")
    return "\n".join(L)


def main() -> int:
    today = tz_lib.deployment_today()   # okengine#301: staleness ages compare against deployment-TZ content dates
    print("=== select-source-staleness ===")
    print(f"  vault: {VAULT}")
    print(f"  stale threshold: {STALE_THRESHOLD}")
    print(f"  primary-citation depth: {PRIMARY_CITATION_DEPTH}")

    scores = score_all_sources(today)
    print(f"  sources scored: {len(scores)}")
    n_stale = sum(1 for s in scores.values() if s.is_stale)
    print(f"  sources below threshold: {n_stale} ({n_stale*100//max(1,len(scores))}%)")

    anchors = discover_anchors(scores)
    print(f"  anchor pages with primary citations: {len(anchors)}")
    stale_n = sum(1 for a in anchors if a.citation_scores and all(s < STALE_THRESHOLD for s in a.citation_scores))
    print(f"  pages with ALL primary citations stale: {stale_n}")

    DASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_dashboard(scores, anchors, today)
    if DASH_PATH.exists() and DASH_PATH.read_text() == rendered:
        print(f"  dashboard: unchanged ({DASH_PATH.relative_to(VAULT)})")
    else:
        DASH_PATH.write_text(rendered)
        print(f"  dashboard: updated ({DASH_PATH.relative_to(VAULT)})")

    print()
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
