#!/usr/bin/env python3
"""refresh_kb_dashboards.py — precompute the KB-hygiene / source Dataview pages
into reader-friendly static markdown (okengine-reader Phase 2, Tier 2).

The okengine-reader can't run Obsidian's Dataview plugin, so the Dataview-driven
dashboards render as "open in Obsidian". This script computes the same tables in
Python and writes `wiki/dashboards/latest-<name>.md` (plain markdown tables that
render in the reader AND Obsidian). The original Dataview pages are left intact
for Obsidian; these `latest-*` copies are what the reader's Dashboards tab links.

The set of knowledge namespaces is driven by the pack's schema.yaml
(`partitioning.namespaces`); when the schema declares none, it falls back to
the namespace directories present on disk under `wiki/`. The engine ships no
domain taxonomy of its own.

Produces (generic, always):
  latest-pages-by-confidence      (per-namespace: by-type, low-confidence review, well-sourced)
  latest-stale-content            (knowledge pages not updated >30d, null-updated)
  latest-source-density           (knowledge pages by source count, zero-source orphans)
  latest-recent-ingest            (last 30 ingested, last 24h, per-day 14d) — when a `sources` namespace exists

Produces (conditional):
  latest-source-quality           (reliability×credibility matrix, unrated backlog) — only when
                                  the schema declares the source-rating fields on the source type

Script-only, no LLM. Always emits {"wakeAgent": false}.

Env:
  WIKI_PATH        vault root (default /opt/vault)
  SOURCE_NAMESPACE namespace holding source-rating pages (default "sources")
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH_DIR = WIKI / "dashboards"
_SCHEMA = schema_lib.governing_schema(VAULT)

# Namespace holding source-rating pages (frontmatter type "source" by
# convention). Pack-overridable; the source-quality / recent-ingest dashboards
# key off it.
SOURCE_NAMESPACE = os.environ.get("SOURCE_NAMESPACE", "sources")

# Source-rating frontmatter fields (engine source-quality convention). The
# source-quality dashboard is only emitted when the pack's schema actually
# declares these on its source type — otherwise it would assert a grading
# scheme the pack doesn't use.
_SOURCE_RATING_FIELDS = ("reliability", "credibility")


def _discover_namespaces() -> list[str]:
    """Knowledge namespaces to build dashboards over.

    Schema-declared (`partitioning.namespaces`) when present; otherwise the
    namespace directories found on disk under wiki/ (minus schema-excluded /
    reserved dirs). Never falls back to a built-in domain taxonomy.
    """
    declared = schema_lib.knowledge_namespaces(_SCHEMA)
    if declared:
        names = set(declared)
    elif WIKI.is_dir():
        excluded = schema_lib.excluded_dirs(_SCHEMA) | {"dashboards", "operational"}
        names = {
            d.name for d in WIKI.iterdir()
            if d.is_dir() and not d.name.startswith((".", "_")) and d.name not in excluded
        }
    else:
        names = set()
    return sorted(names)


def _schema_declares_source_rating() -> bool:
    """True only if the schema declares the source-rating fields on a type.
    Gates the (otherwise grading-scheme-specific) source-quality dashboard."""
    types = _SCHEMA.get("types")
    if not isinstance(types, dict):
        return False
    for spec in types.values():
        if not isinstance(spec, dict):
            continue
        declared_fields = set()
        for key in ("required", "optional", "fields"):
            v = spec.get(key)
            if isinstance(v, (list, tuple, set)):
                declared_fields |= {str(x) for x in v}
            elif isinstance(v, dict):
                declared_fields |= {str(x) for x in v.keys()}
        if all(f in declared_fields for f in _SOURCE_RATING_FIELDS):
            return True
    return False

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.S)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
CONF_ORDER = {"low": 0, "medium": 1, "medium-high": 2, "high": 3}


def _parse_date(s) -> "date | None":
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, str):
        m = _DATE_RE.search(s)
        if m:
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


# When a source was filed into the vault. Sources rarely carry an explicit `ingested`/`created`
# stamp, so fall back to `last_updated`/`updated` (the importer's write date ~= ingest) and
# finally `published`. Without this, the recent-ingest dashboard reads only `ingested`, finds it
# on no source, and renders an all-empty "none" board even as sources stream in (okengine#…).
_INGEST_DATE_FIELDS = ("ingested", "created", "last_updated", "updated", "published")


def _ingest_date(s: dict) -> "date | None":
    for f in _INGEST_DATE_FIELDS:
        d = _parse_date(s.get(f))
        if d:
            return d
    return None


def _frontmatter(path: Path) -> dict:
    try:
        txt = path.read_text(errors="replace")
    except OSError:
        return {}
    m = _FM_RE.match(txt)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return fm if isinstance(fm, dict) else {}


def load_dir(sub: str) -> list[dict]:
    """Frontmatter of every page under wiki/<sub> (recursive), tagged with _name/_sub."""
    out: list[dict] = []
    base = WIKI / sub
    if not base.is_dir():
        return out
    for p in base.rglob("*.md"):
        name = p.name
        if name.startswith(("_", ".")) or ".bak." in name:
            continue
        fm = _frontmatter(p)
        fm["_name"] = p.stem
        fm["_sub"] = sub
        out.append(fm)
    return out


def _n_sources(fm: dict) -> int:
    s = fm.get("sources")
    return len(s) if isinstance(s, list) else 0


def _wl(fm: dict) -> str:
    disp = str(fm.get("title") or fm.get("name") or fm["_name"]).strip()
    return f"[[{fm['_sub']}/{fm['_name']}|{disp}]]"


def _esc(v) -> str:
    return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def _header(title: str, ts: str, note: str) -> list[str]:
    return ["---", "type: dashboard", f"title: {title}",
            f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "generator: scripts/cron/refresh_kb_dashboards.py", "---", "",
            f"# {title}", "", f"_Synced {ts} — {note}_", ""]


def _write(name: str, lines: list[str]) -> Path:
    path = DASH_DIR / f"{name}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ── dashboards ───────────────────────────────────────────────────────────────
def dash_pages_by_confidence(pages, ts) -> Path:
    """Generic: confidence + sourcing review over all knowledge pages.
    Uses only engine-convention fields (type, confidence, sources)."""
    L = _header("Pages by confidence", ts,
                "low-confidence and single-source pages to review, across all "
                "knowledge namespaces.")
    by_type: dict[str, int] = {}
    for e in pages:
        by_type[str(e.get("type") or "—")] = by_type.get(str(e.get("type") or "—"), 0) + 1
    L.append("## By type\n")
    L.append(_table(["Type", "Count"],
                    [[t, n] for t, n in sorted(by_type.items(), key=lambda x: -x[1])]))
    review = sorted(
        [e for e in pages if str(e.get("confidence") or "").lower() in ("low", "medium")
         or _n_sources(e) <= 1],
        key=lambda e: (CONF_ORDER.get(str(e.get("confidence") or "").lower(), 4), _n_sources(e)))
    L.append(f"## Low-confidence / single-source review ({len(review)})\n")
    L.append(_table(["Page", "Type", "Conf", "Sources", "Updated"],
                    [[_wl(e), _esc(e.get("type")), _esc(e.get("confidence")),
                      _n_sources(e), _esc(str(e.get("updated") or "")[:10])] for e in review[:60]]))
    well = sorted([e for e in pages if _n_sources(e) >= 5], key=_n_sources, reverse=True)
    L.append(f"## Well-sourced (≥5 sources) ({len(well)})\n")
    L.append(_table(["Page", "Type", "Conf", "Sources"],
                    [[_wl(e), _esc(e.get("type")), _esc(e.get("confidence")), _n_sources(e)]
                     for e in well[:25]]))
    return _write("latest-pages-by-confidence", L)


def dash_stale_content(pages, today, ts) -> Path:
    """Generic: knowledge pages not updated in >30d, plus pages missing `updated`."""
    L = _header("Stale content", ts,
                "knowledge pages not updated in >30d, across all namespaces.")
    rows = []
    for it in pages:
        u = _parse_date(it.get("updated"))
        if u and (today - u).days > 30:
            rows.append((it, u, (today - u).days))
    rows.sort(key=lambda r: r[1])
    L.append(f"## Stale pages (>30d) ({len(rows)})\n")
    L.append(_table(["Page", "Namespace", "Last updated", "Days stale", "Sources", "Conf"],
                    [[_wl(it), _esc(it.get("_sub")), u.isoformat(), days,
                      _n_sources(it), _esc(it.get("confidence"))]
                     for it, u, days in rows[:50]]))
    nullu = [it for it in pages if not _parse_date(it.get("updated"))]
    L.append(f"## No `updated` field ({len(nullu)})\n")
    L.append(_table(["Page", "Type", "Sources"],
                    [[_wl(it), _esc(it.get("type")), _n_sources(it)] for it in nullu[:50]]))
    return _write("latest-stale-content", L)


def dash_source_density(pages, ts) -> Path:
    """Generic: knowledge pages ranked by source count; zero-source orphans."""
    L = _header("Source density", ts,
                "knowledge pages ranked by source count; zero-source orphans, "
                "across all namespaces.")
    top = sorted(pages, key=_n_sources, reverse=True)[:30]
    L.append("## Top pages by sources\n")
    L.append(_table(["Page", "Namespace", "Type", "Sources", "Conf"],
                    [[_wl(it), _esc(it.get("_sub")), _esc(it.get("type")),
                      _n_sources(it), _esc(it.get("confidence"))] for it in top]))
    zero = [it for it in pages if _n_sources(it) == 0]
    L.append(f"## Zero-source pages ({len(zero)})\n")
    L.append(_table(["Page", "Namespace", "Type"],
                    [[_wl(it), _esc(it.get("_sub")), _esc(it.get("type"))] for it in zero[:40]]))
    return _write("latest-source-density", L)


def dash_source_quality(sources, ts) -> Path:
    srcs = [s for s in sources if str(s.get("type") or "") == "source"]
    L = _header("Source quality distribution", ts,
                f"reliability×credibility over {len(srcs):,} sources. From `wiki/sources`.")
    matrix: dict[str, int] = {}
    rated = 0
    for s in srcs:
        rel, cred = s.get("reliability"), s.get("credibility")
        if rel and cred is not None:
            matrix[f"{rel} / {cred}"] = matrix.get(f"{rel} / {cred}", 0) + 1
            rated += 1
    L.append(f"## Reliability / credibility matrix ({rated:,} rated)\n")
    L.append(_table(["Rel / Cred", "Count"],
                    [[k, v] for k, v in sorted(matrix.items())]))
    unrated = [s for s in srcs if not s.get("reliability")]
    L.append(f"## Unrated backlog (no reliability) ({len(unrated):,})\n")
    unrated.sort(key=lambda s: str(_parse_date(s.get("ingested")) or ""), reverse=True)
    L.append(_table(["Source", "Publisher", "Kind", "Ingested"],
                    [[_wl(s), _esc(s.get("publisher")), _esc(s.get("source_kind")),
                      _esc(str(s.get("ingested") or "")[:10])] for s in unrated[:30]]))
    flagged = [s for s in srcs if isinstance(s.get("bias_flags"), list) and s.get("bias_flags")]
    L.append(f"## Bias-flagged ({len(flagged):,})\n")
    L.append(_table(["Source", "Publisher", "Rel", "Cred", "Flags"],
                    [[_wl(s), _esc(s.get("publisher")), _esc(s.get("reliability")),
                      _esc(s.get("credibility")), _esc(", ".join(map(str, s.get("bias_flags", []))))]
                     for s in flagged[:30]]))
    return _write("latest-source-quality", L)


def dash_recent_ingest(sources, today, ts) -> Path:
    srcs = [s for s in sources if str(s.get("type") or "") == "source"]
    L = _header("Recent ingest", ts, "newest sources filed. From `wiki/sources`.")
    dated = [(s, _ingest_date(s)) for s in srcs]
    dated = [(s, d) for s, d in dated if d]
    dated.sort(key=lambda x: x[1], reverse=True)
    L.append("## Last 30 ingested\n")
    L.append(_table(["Source", "Publisher", "Kind", "Published", "Ingested"],
                    [[_wl(s), _esc(s.get("publisher")), _esc(s.get("source_kind")),
                      _esc(str(s.get("published") or "")[:10]), d.isoformat()]
                     for s, d in dated[:30]]))
    last24 = [s for s, d in dated if (today - d).days <= 1]
    L.append(f"## Last 24h ({len(last24)})\n")
    L.append(_table(["Source", "Publisher", "Kind"],
                    [[_wl(s), _esc(s.get("publisher")), _esc(s.get("source_kind"))] for s in last24[:50]]))
    per_day: dict[str, int] = {}
    for s, d in dated:
        if (today - d).days <= 14:
            per_day[d.isoformat()] = per_day.get(d.isoformat(), 0) + 1
    L.append("## Per-day (last 14d)\n")
    L.append(_table(["Ingested", "Sources"],
                    [[k, per_day[k]] for k in sorted(per_day, reverse=True)]))
    return _write("latest-recent-ingest", L)


# NOTE: a domain-specific "trend acceleration" dashboard (keyed on a `trend`
# page type and thesis-tracking fields: trend_status, last_thesis_update,
# thesis_confidence, with literal "reversed"/"dormant" statuses) was REMOVED
# from the engine — those concepts are irreducibly domain-specific and belong
# in the pack, not the domain-agnostic engine. A pack that uses trend/thesis
# tracking should ship its own dashboard generator.


def main() -> int:
    today = datetime.now(timezone.utc).date()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    DASH_DIR.mkdir(parents=True, exist_ok=True)

    namespaces = _discover_namespaces()
    # Knowledge pages = every namespace except the source-rating namespace,
    # which is handled by the source-specific dashboards below.
    knowledge_ns = [ns for ns in namespaces if ns != SOURCE_NAMESPACE]
    pages: list[dict] = []
    for ns in knowledge_ns:
        pages.extend(load_dir(ns))
    sources = load_dir(SOURCE_NAMESPACE) if SOURCE_NAMESPACE in namespaces else []

    written = [
        dash_pages_by_confidence(pages, ts),
        dash_stale_content(pages, today, ts),
        dash_source_density(pages, ts),
    ]
    # Source-quality dashboard: only when the schema declares the source-rating
    # fields (reliability/credibility). Otherwise it would assert a grading
    # scheme the pack doesn't use.
    if sources and _schema_declares_source_rating():
        written.append(dash_source_quality(sources, ts))
    if sources:
        written.append(dash_recent_ingest(sources, today, ts))

    print("=== refresh-kb-dashboards ===")
    print(f"  vault: {VAULT}")
    print(f"  namespaces: {namespaces}  source_ns: {SOURCE_NAMESPACE}")
    print(f"  loaded: knowledge_pages={len(pages)} sources={len(sources)}")
    for p in written:
        print(f"  wrote: {p.relative_to(VAULT)}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
