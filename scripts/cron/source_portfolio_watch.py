#!/usr/bin/env python3
"""source_portfolio_watch.py — pure-script observability for the source CORPUS COMPOSITION
(ported and generalized from the origin system's source-portfolio-watch).

Complements source-staleness / source_decay, which score per-source AGE DECAY. This is the
orthogonal view: is the corpus BALANCED, or drifting? It walks `wiki/sources/` and writes a
`wiki/dashboards/source-portfolio.md` snapshot of:

  - headline distribution (by `signal_class` if the pack defines that field, else by `source_kind`);
  - recent ingest mix (last 7d / 30d) — is the mix drifting healthier or worse over time?;
  - source_kind × signal_class crosstab (signal_class column only when present);
  - publisher concentration (top-N publishers by volume — catches over-reliance on one outlet);
  - reliability distribution (are the high-rated sources all one class?);
  - prediction-bearing coverage — how many sources any OPEN prediction cites in `basis:` (connects
    corpus depth to the live forecast surface).

GENERIC: every field is optional (`(unset)` fallback). `signal_class` is an origin-system convention
(a source's forecasting role) — packs that don't define it still get every other section, and the
signal_class columns simply collapse. No domain vocabulary is baked in; the origin-system watchlist
section was dropped (that's a competitive-analytics concern, not generic corpus observability).

Pure `no_agent` script — the math IS the deliverable; emits `wakeAgent=false` always. Idempotent
per day.

Env: WIKI_PATH (default /opt/vault) · PORTFOLIO_TOP_PUBLISHERS (20)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
SOURCES_DIR = VAULT / "wiki" / "sources"
PRED_DIR = VAULT / "wiki" / "predictions"


def _bases(ns: str) -> list:
    """Root wiki/<ns> + every walk-up sub-domain wiki/<sub>/<ns> (a dir carrying its own schema.yaml).
    A root-only scan under-reports the concentration/coverage this dashboard exists to surface on a
    co-installed (multipack) vault (okengine#178)."""
    out = [WIKI / ns]
    if WIKI.is_dir():
        for sub in sorted(WIKI.iterdir()):
            if sub.is_dir() and (sub / "schema.yaml").is_file():
                out.append(sub / ns)
    return out
DASH_DIR = VAULT / "wiki" / "dashboards"
TOP_PUBLISHERS = int(os.environ.get("PORTFOLIO_TOP_PUBLISHERS", "20"))

_FM_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+?)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_RESOLVED_RE = re.compile(r"confirm|refut|partial|expired-ungraded|tombstone", re.IGNORECASE)


def _parse_fm(text: str) -> dict | None:
    m = _FM_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
        return fm if isinstance(fm, dict) else None
    except yaml.YAMLError:
        return None


def _date(val) -> date | None:
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _open_prediction_basis_slugs() -> set[str]:
    """Slugs cited in `basis:` by any prediction that is NOT resolved."""
    out: set[str] = set()
    for base in _bases("predictions"):         # root + walk-up sub-domain predictions (multipack)
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            if p.name.startswith(("_", ".")):
                continue
            try:
                fm = _parse_fm(p.read_text(errors="replace"))
            except OSError:
                continue
            if not fm or fm.get("type") != "prediction":
                continue
            if _RESOLVED_RE.search(str(fm.get("status") or "open")):
                continue
            basis = fm.get("basis")
            for entry in (basis if isinstance(basis, list) else []):
                for wm in _WIKILINK_RE.finditer(str(entry)):
                    slug = wm.group(1).strip().split("/")[-1]
                    out.add(slug[:-3] if slug.endswith(".md") else slug)
    return out


def _collect() -> list[dict]:
    out = []
    for base in _bases("sources"):             # root + walk-up sub-domain sources (multipack)
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            if p.name.startswith(("_", ".")):
                continue
            try:
                fm = _parse_fm(p.read_text(errors="replace"))
            except OSError:
                continue
            if not fm:
                continue
            out.append({
                "stem": p.stem,
                "signal_class": fm.get("signal_class") or "(unset)",
                "source_kind": fm.get("source_kind") or "(unset)",
                "publisher": fm.get("publisher") or "(unset)",
                "reliability": str(fm.get("reliability") or "(unset)"),
                "ingested": _date(fm.get("ingested") or fm.get("created")),
            })
    return out


def _pct(n: int, total: int) -> str:
    return f"{100.0 * n / total:.1f}%" if total else "0.0%"


def render(sources: list[dict], today: date) -> str:
    total = len(sources)
    has_class = any(s["signal_class"] != "(unset)" for s in sources)
    classes = sorted({s["signal_class"] for s in sources}, key=lambda c: (c == "(unset)", c))
    pred_slugs = _open_prediction_basis_slugs()
    n_pred = sum(1 for s in sources if s["stem"] in pred_slugs)

    L = ["---", "type: dashboard", f'title: "Source portfolio watch — {today.isoformat()}"',
         f"updated: {today.isoformat()}", f"n_sources: {total}", "---", "",
         f"# Source portfolio watch — {today.isoformat()}", "",
         "Pure-script snapshot of corpus COMPOSITION (complements source-staleness, which scores "
         "per-source age decay). Spot a corpus over-concentrated on one publisher / source-kind, "
         "or drifting away from decision-bearing signal.", ""]

    # ── headline distribution: by signal_class if present, else by source_kind ──
    axis = "signal_class" if has_class else "source_kind"
    dist = Counter(s[axis] for s in sources)
    L += [f"## Headline distribution (by {axis})", "",
          f"Total sources: **{total:,}**", "", f"| {axis} | count | % |", "|---|---|---|"]
    for k, n in dist.most_common():
        L.append(f"| {k} | {n:,} | {_pct(n, total)} |")
    L.append("")

    # ── recent ingest mix (7d / 30d) ──
    L += ["## Recent ingest mix", "",
          f"Is the corpus drifting? Composition of the last 7d / 30d ingests, by {axis}.", ""]
    for label, days in (("last 7d", 7), ("last 30d", 30)):
        cut = today - timedelta(days=days)
        recent = [s for s in sources if s["ingested"] and s["ingested"] >= cut]
        by = Counter(s[axis] for s in recent)
        top = ", ".join(f"{k} {n}" for k, n in by.most_common(4)) if recent else "—"
        L.append(f"- **{label}**: {len(recent):,} ingested — {top}")
    L.append("")

    # ── source_kind × signal_class crosstab (class columns only when present) ──
    L += ["## Source kind" + (" × signal class" if has_class else " distribution"), ""]
    sk = defaultdict(Counter)
    for s in sources:
        sk[s["source_kind"]][s["signal_class"]] += 1
    if has_class:
        L += ["| source_kind | total | " + " | ".join(classes) + " |",
              "|---|---|" + "|".join("---" for _ in classes) + "|"]
        for k in sorted(sk, key=lambda k: -sum(sk[k].values())):
            row = " | ".join(f"{sk[k].get(c, 0):,}" for c in classes)
            L.append(f"| {k} | {sum(sk[k].values()):,} | {row} |")
    else:
        L += ["| source_kind | count | % |", "|---|---|---|"]
        for k in sorted(sk, key=lambda k: -sum(sk[k].values())):
            n = sum(sk[k].values())
            L.append(f"| {k} | {n:,} | {_pct(n, total)} |")
    L.append("")

    # ── publisher concentration ──
    pub = Counter(s["publisher"] for s in sources)
    L += [f"## Top {TOP_PUBLISHERS} publishers (concentration)", "",
          "One outlet dominating the corpus is a single-source-of-truth risk.", "",
          "| publisher | count | % of corpus |", "|---|---|---|"]
    for name, n in pub.most_common(TOP_PUBLISHERS):
        L.append(f"| {name} | {n:,} | {_pct(n, total)} |")
    L.append("")

    # ── reliability distribution ──
    rel = Counter(s["reliability"] for s in sources)
    L += ["## Reliability distribution", "", "| reliability | count | % |", "|---|---|---|"]
    for r in sorted(rel, key=lambda r: (r == "(unset)", r)):
        L.append(f"| {r} | {rel[r]:,} | {_pct(rel[r], total)} |")
    L.append("")

    # ── prediction-bearing coverage ──
    L += ["## Prediction-bearing coverage", "",
          f"Sources cited in `basis:` by an OPEN prediction: **{n_pred:,}** of {total:,} "
          f"({_pct(n_pred, total)}). These directly bear on live forecasts.", ""]
    return "\n".join(L) + "\n"


def main() -> int:
    sources = _collect()
    DASH_DIR.mkdir(parents=True, exist_ok=True)
    (DASH_DIR / "source-portfolio.md").write_text(render(sources, date.today()), encoding="utf-8")
    print(f"source-portfolio: {len(sources)} source(s) -> dashboards/source-portfolio.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
