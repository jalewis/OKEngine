#!/usr/bin/env python3
"""Daily refresh of wiki/dashboards/contradictions.md — closes GitLab #9.

Walks the vault for `## Contradictions` (and `## Contradictions & Open
Questions` / `## Contradictions and Open Questions` / similar variants)
sections, classifies each as ACTIVE / EMPTY / RESOLVED, and renders a
ranked dashboard surfacing where the wiki currently disagrees with
itself. Companion view to the calibration dashboards (prediction-backtest
+ trend-acceleration) — those measure where the wiki's predictions land
vs reality; this measures where the wiki's *current* claims land vs
each other.

Section classification:
  - EMPTY    — section text starts with "none" / "n/a" / "no contradictions"
                (case-insensitive); operators write these on pages where the
                review pass concluded the literature is internally consistent.
  - RESOLVED — ALL of the section's enumerated contradictions carry an inline
                `**RESOLVED YYYY-MM-DD:**` marker, OR the page has a
                separate `## Contradiction resolution` section dated within
                the past 365 days. (Pages are reclassified as their
                dated-resolution sections are added.)
  - ACTIVE   — has substantive content and no full-coverage resolution.

Per-segment density metric: count of ACTIVE pages under each top-level
wiki/ subdirectory (entities, concepts, predictions, sources, etc.).
Concepts where active-contradiction-density / total-pages exceeds the
corpus baseline are surfaced as "destabilizing" — those are the areas
where the wiki's understanding is in flux.

Pure script — wakeAgent=false. Idempotent on a given day (re-runs leave
the dashboard byte-identical when no source pages changed).
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
DASH_PATH = VAULT / "wiki" / "dashboards" / "contradictions.md"

# Permissive: any H2 whose words include "Contradiction(s)". Catches:
#   "## Contradictions", "## Contradictions & Open Questions",
#   "## Contradictions / Tensions", "## Open contradictions",
#   "## Critical Contradictions", "## Contradictions with Other Sources", etc.
# The dashboard path is excluded at walk time, so the script doesn't match
# its own section headers.
CONTRADICTION_HEADER_RE = re.compile(
    r"^##\s[^\n]*?\bContradictions?\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
RESOLUTION_HEADER_RE = re.compile(
    r"^##\s+Contradiction\s+resolutions?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
RESOLVED_INLINE_RE = re.compile(r"\*\*\s*RESOLVED\s+(\d{4}-\d{2}-\d{2})\s*:?\s*\*\*", re.IGNORECASE)
DATE_INLINE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")

EMPTY_PHRASES = (
    "none currently",
    "none.",
    "none\n",
    "n/a",
    "no contradictions",
    "no active contradictions",
)

# How many pages per segment to list in the dashboard
TOP_PER_SEGMENT = int(os.environ.get("CONTRADICTIONS_TOP_PER_SEGMENT", "15"))
# Resolution decay window: a resolution section dated > N days ago no longer
# counts toward "fully resolved" — the contradiction may have re-emerged.
RESOLUTION_HORIZON_DAYS = int(os.environ.get("CONTRADICTIONS_RESOLUTION_HORIZON_DAYS", "365"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)


@dataclass
class Contradiction:
    path: Path
    rel_path: str
    segment: str            # top-level dir under wiki/
    title: str
    classification: str     # ACTIVE | EMPTY | RESOLVED
    section_chars: int      # length of contradictions section body
    section_h3_count: int   # rough count of enumerated items
    file_updated: date | None
    last_dated_note: date | None     # latest [YYYY-MM-DD] marker found in section
    last_resolved_note: date | None  # latest RESOLVED YYYY-MM-DD or resolution-section date


def parse_fm(txt: str) -> dict:
    m = _FM_RE.match(txt)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
        return fm if isinstance(fm, dict) else {}
    except yaml.YAMLError:
        return {}


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


def extract_section(body: str, header_re: re.Pattern[str]) -> str | None:
    m = header_re.search(body)
    if not m:
        return None
    after = body[m.end():]
    nm = NEXT_H2_RE.search(after)
    return after[:nm.start()] if nm else after


def classify_section(section: str, resolution_section: str | None, today: date) -> tuple[str, date | None, date | None]:
    """Return (classification, last_dated_note, last_resolved_note)."""
    s = section.strip()
    if not s:
        return "EMPTY", None, None
    s_lc = s.lower()
    if any(s_lc.startswith(p) for p in EMPTY_PHRASES):
        return "EMPTY", None, None

    # Find dated notes inside the contradictions section
    dates_found = [to_date(d) for d in DATE_INLINE_RE.findall(s)]
    dates_found = [d for d in dates_found if d is not None]
    last_dated = max(dates_found) if dates_found else None

    # Find inline RESOLVED markers
    resolved_inline = [to_date(d) for d in RESOLVED_INLINE_RE.findall(s)]
    resolved_inline = [d for d in resolved_inline if d is not None]
    last_inline_resolved = max(resolved_inline) if resolved_inline else None

    # Find dates in dedicated resolution section
    last_section_resolved: date | None = None
    if resolution_section:
        rs_dates = [to_date(d) for d in DATE_INLINE_RE.findall(resolution_section)]
        rs_dates = [d for d in rs_dates if d is not None]
        if rs_dates:
            last_section_resolved = max(rs_dates)

    last_resolved = max(filter(None, [last_inline_resolved, last_section_resolved]), default=None)

    # Heuristic: if there's a recent resolution section AND no dated
    # contradiction note newer than it, treat as RESOLVED.
    horizon_cutoff = today - timedelta(days=RESOLUTION_HORIZON_DAYS)
    if last_section_resolved and last_section_resolved >= horizon_cutoff:
        if last_dated is None or last_dated <= last_section_resolved:
            return "RESOLVED", last_dated, last_resolved

    # If every numbered subheading carries an inline RESOLVED marker,
    # call it RESOLVED. Crude proxy: count "**Contradiction" subheadings
    # vs "**RESOLVED " markers — equal-or-more resolveds means all covered.
    item_count = len(re.findall(r"^\s*[-*]?\s*\*\*\s*(?:Contradiction|Question)\s+\d", s, re.IGNORECASE | re.MULTILINE))
    if item_count > 0 and len(resolved_inline) >= item_count:
        return "RESOLVED", last_dated, last_resolved

    return "ACTIVE", last_dated, last_resolved


def discover_contradictions(today: date) -> list[Contradiction]:
    out: list[Contradiction] = []
    if not (VAULT / "wiki").is_dir():
        return out
    for p in (VAULT / "wiki").rglob("*.md"):
        # Skip lint reports + operational outputs + the dashboard itself
        if p.name.startswith("lint-") or p.name.startswith("_"):
            continue
        rel = p.relative_to(VAULT)
        rel_str = str(rel).replace("\\", "/")
        if rel_str.startswith("wiki/operational/") or rel_str.startswith("wiki/dashboards/"):
            continue
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        # Strip frontmatter for body matching
        m = _FM_RE.match(txt)
        body = txt[m.end():] if m else txt
        section = extract_section(body, CONTRADICTION_HEADER_RE)
        if section is None:
            continue
        resolution_section = extract_section(body, RESOLUTION_HEADER_RE)
        fm = parse_fm(txt)

        classification, last_dated, last_resolved = classify_section(section, resolution_section, today)
        s_clean = section.strip()
        h3_count = max(
            len(re.findall(r"^\s*[-*]?\s*\*\*\s*(?:Contradiction|Question)\s+\d", s_clean, re.IGNORECASE | re.MULTILINE)),
            len(re.findall(r"^###\s", s_clean, re.MULTILINE)),
        )

        parts = rel_str.split("/")
        segment = parts[1] if len(parts) >= 2 and parts[0] == "wiki" else "(other)"

        h1_match = re.search(r"^#\s+(.+?)$", body, re.MULTILINE)
        title = (
            fm.get("title") or fm.get("name") or
            (h1_match.group(1).strip() if h1_match else None) or
            p.stem.replace("-", " ").title()
        )

        out.append(Contradiction(
            path=p,
            rel_path=rel_str,
            segment=segment,
            title=title,
            classification=classification,
            section_chars=len(s_clean),
            section_h3_count=h3_count,
            file_updated=to_date(fm.get("updated")),
            last_dated_note=last_dated,
            last_resolved_note=last_resolved,
        ))
    return out


# ─── dashboard render ────────────────────────────────────────────────


def _wikilink(rel_path: str, title: str) -> str:
    # rel_path is wiki/<segment>/<slug>.md. The pipe is backslash-escaped so
    # the wikilink survives inside a markdown table cell — without the
    # escape, the cell terminates at `|` and the alias becomes the next
    # column. All current call sites are table rows; if you add a
    # non-table caller, drop the backslash there.
    inner = rel_path[len("wiki/"):] if rel_path.startswith("wiki/") else rel_path
    inner = inner[:-3] if inner.endswith(".md") else inner
    return f"[[{inner}\\|{title}]]"


def render_dashboard(items: list[Contradiction], today: date) -> str:
    by_class: dict[str, list[Contradiction]] = defaultdict(list)
    for c in items:
        by_class[c.classification].append(c)
    active = by_class.get("ACTIVE", [])
    resolved = by_class.get("RESOLVED", [])
    empty = by_class.get("EMPTY", [])

    # Per-segment density on ACTIVE
    seg_active: Counter = Counter()
    seg_total: Counter = Counter()
    for c in items:
        seg_total[c.segment] += 1
        if c.classification == "ACTIVE":
            seg_active[c.segment] += 1

    L: list[str] = []
    L.append("---")
    L.append("type: dashboard")
    L.append("title: Contradictions")
    L.append(f"created: {today.isoformat()}")
    L.append(f"updated: {today.isoformat()}")
    L.append("---")
    L.append("")
    L.append("# Contradictions")
    L.append("")
    L.append(
        "Where the wiki currently disagrees with itself. Per vault `CLAUDE.md` § Ingest "
        "rule 5: \"Contradictions are signal, not noise.\" Tracked in `## Contradictions` "
        "(and `## Contradictions & Open Questions`) sections on each page; resolution "
        "appended as `## Contradiction resolution` or inline `**RESOLVED YYYY-MM-DD:**` "
        "marker per the resolution convention."
    )
    L.append("")
    L.append(
        "Refreshed daily by `scripts/cron/select_contradictions.py`. "
        "Companion to [[prediction-backtest]] (calibration at the prediction level) and "
        "[[trend-acceleration]] (status-transition lifecycle)."
    )
    L.append("")
    L.append(f"**Counts:** {len(active)} active · {len(resolved)} resolved · {len(empty)} empty (page reviewed but no contradiction found) · {len(items)} pages with a Contradictions section total.")
    L.append("")

    # Active by segment
    L.append("## Active contradictions by segment")
    L.append("")
    L.append("| Segment | Active | Total pages with a section | Active rate |")
    L.append("|---|---:|---:|---:|")
    for seg in sorted(seg_active.keys() | seg_total.keys()):
        tot = seg_total[seg]
        act = seg_active[seg]
        rate = f"{act/tot:.0%}" if tot else "—"
        L.append(f"| `{seg}` | {act} | {tot} | {rate} |")
    L.append("")

    # Top destabilizing concepts: rank ACTIVE pages within concepts/ by section size
    L.append("## Destabilizing concepts (active, ranked by section weight)")
    L.append("")
    L.append(
        "Pages with substantive active contradictions, ranked by section length × "
        "enumerated-item count. Heavy contradiction sections on a concept page = the "
        "wiki's understanding of that concept is materially in flux."
    )
    L.append("")
    concepts_active = [c for c in active if c.segment == "concepts"]
    concepts_active.sort(key=lambda c: -(c.section_chars * (c.section_h3_count or 1)))
    L.append("| Concept | Items | Section chars | Last dated note | Last resolved note | File `updated:` |")
    L.append("|---|---:|---:|---|---|---|")
    for c in concepts_active[:TOP_PER_SEGMENT]:
        L.append(
            f"| {_wikilink(c.rel_path, c.title)} "
            f"| {c.section_h3_count or '—'} "
            f"| {c.section_chars} "
            f"| {c.last_dated_note.isoformat() if c.last_dated_note else '—'} "
            f"| {c.last_resolved_note.isoformat() if c.last_resolved_note else '—'} "
            f"| {c.file_updated.isoformat() if c.file_updated else '—'} |"
        )
    if not concepts_active:
        L.append("| _no active contradictions on concept pages_ | | | | | |")
    L.append("")

    # All-active table — useful for completeness
    L.append("## All active contradictions")
    L.append("")
    L.append(
        "Sorted by file `updated:` desc — the most recently touched pages float to the top, "
        "since those are most likely to have just-introduced or just-strengthened contradictions."
    )
    L.append("")
    L.append("| Page | Segment | Items | Section chars | Last dated note | File `updated:` |")
    L.append("|---|---|---:|---:|---|---|")
    active_sorted = sorted(active, key=lambda c: (c.file_updated or date.min), reverse=True)
    for c in active_sorted:
        L.append(
            f"| {_wikilink(c.rel_path, c.title)} "
            f"| `{c.segment}` "
            f"| {c.section_h3_count or '—'} "
            f"| {c.section_chars} "
            f"| {c.last_dated_note.isoformat() if c.last_dated_note else '—'} "
            f"| {c.file_updated.isoformat() if c.file_updated else '—'} |"
        )
    if not active:
        L.append("| _no active contradictions_ | | | | | |")
    L.append("")

    # Resolved
    L.append("## Resolved contradictions (history)")
    L.append("")
    L.append(
        "Past contradictions where one side won and the other moved to historical note. "
        "Kept visible — past contradictions are informative even when resolved (they show "
        "where the wiki's understanding was unstable; useful retrospective for similar "
        "future debates)."
    )
    L.append("")
    L.append("| Page | Segment | Resolved on | Items |")
    L.append("|---|---|---|---:|")
    resolved_sorted = sorted(resolved, key=lambda c: (c.last_resolved_note or date.min), reverse=True)
    for c in resolved_sorted:
        L.append(
            f"| {_wikilink(c.rel_path, c.title)} "
            f"| `{c.segment}` "
            f"| {c.last_resolved_note.isoformat() if c.last_resolved_note else '—'} "
            f"| {c.section_h3_count or '—'} |"
        )
    if not resolved:
        L.append("| _no resolved contradictions yet_ | | | |")
    L.append("")

    # Convention reminder
    L.append("## Convention")
    L.append("")
    L.append(
        "On any page that develops a `## Contradictions` section:"
    )
    L.append("")
    L.append(
        "1. Each contradiction gets a numbered subheading (`**Contradiction N: <title>**`) "
        "and a dated note in `[YYYY-MM-DD]` format on the line stating which side is "
        "currently believed and why."
    )
    L.append(
        "2. When a contradiction resolves, append a `## Contradiction resolution` section "
        "dated `[YYYY-MM-DD]` describing which side won and what evidence settled it. "
        "Do NOT delete the contradiction — past contradictions are historical record."
    )
    L.append(
        "3. Alternatively, mark a single resolved item inline in the Contradictions section "
        "with a `**RESOLVED YYYY-MM-DD:**` marker followed by the resolution rationale."
    )
    L.append(
        "4. The dashboard above classifies a Contradictions section as RESOLVED when ALL "
        "enumerated items are marked resolved OR a dedicated resolution section is dated "
        f"within the past {RESOLUTION_HORIZON_DAYS} days."
    )
    L.append("")

    return "\n".join(L)


def main() -> int:
    today = datetime.now(timezone.utc).date()
    print("=== select-contradictions ===")
    print(f"  vault: {VAULT}")
    print(f"  resolution horizon: {RESOLUTION_HORIZON_DAYS} days")

    items = discover_contradictions(today)
    by_class: Counter = Counter(c.classification for c in items)
    print(f"  pages with Contradictions section: {len(items)}")
    for cls in ("ACTIVE", "RESOLVED", "EMPTY"):
        print(f"    {cls}: {by_class.get(cls, 0)}")

    DASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_dashboard(items, today)
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
