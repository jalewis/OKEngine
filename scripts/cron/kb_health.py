#!/usr/bin/env python3
"""kb_health.py — aggregate vault-health metrics into one dashboard.

KB health is a first-class metric: the dashboard the operator skims to know the
substrate is sound (the 18-day curated-entity corruption in #85 survived because
nothing surfaced it as a number).

This is an AGGREGATOR, not a recomputer. The expensive scans already run daily:
  - lint_watcher  -> wiki/operational/queue-snapshots.md
      (broken-wikilinks, orphans, fm-parse-errors, schema-drift, publisher-drift, …)
  - page_quality_audit -> wiki/operational/page-quality-snapshots.md
  - detect_field_loss  -> wiki/operational/field-loss-snapshots.md
This script reads the LATEST row of each snapshot history and renders
wiki/dashboards/kb-health.md with red/green thresholds plus a
drain-vs-accumulation trend (now vs ~7 days ago). It also appends a one-row/day
history to wiki/operational/kb-health-snapshots.md so KB health itself trends.

Domain-specific health (e.g. prediction grading/calibration) is NOT computed
here — that conformance lives in the domain pack, which can publish its own
snapshot files and dashboards.

Script-only, no LLM. Always emits {"wakeAgent": false}.

Env:
  WIKI_PATH   vault root (default /opt/vault)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_lib  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
OP_DIR = VAULT / "wiki" / "operational"
DASH_PATH = VAULT / "wiki" / "dashboards" / "kb-health.md"
HISTORY_PATH = OP_DIR / "kb-health-snapshots.md"

QUEUE_SNAPSHOTS = OP_DIR / "queue-snapshots.md"

# Queues we trend for drain-vs-accumulation. Δ/day > 0 means the backlog is
# growing faster than the drains clear it.
TREND_KEYS = ["broken-wikilinks", "orphans", "schema-drift", "fm-parse-errors"]
TREND_WINDOW_DAYS = 7

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _parse_date(s) -> date | None:
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


def parse_queue_snapshots(text: str) -> list[tuple[date, dict[str, int]]]:
    """Each data row: `| YYYY-MM-DD | key=val, key=val, … |`."""
    rows: list[tuple[date, dict[str, int]]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cols) < 2:
            continue
        d = _parse_date(cols[0])
        if d is None:
            continue
        kv: dict[str, int] = {}
        for pair in cols[1].split(","):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            try:
                kv[k.strip()] = int(v.strip())
            except ValueError:
                continue
        if kv:
            rows.append((d, kv))
    rows.sort(key=lambda r: r[0])
    return rows


def _latest_field_loss() -> int | None:
    """Latest curated-field-loss count from detect_field_loss's snapshot."""
    snap = OP_DIR / "field-loss-snapshots.md"
    if not snap.exists():
        return None
    last = None
    for line in snap.read_text(errors="replace").splitlines():
        if line.startswith("| 2"):
            parts = [c.strip() for c in line.strip("|").split("|")]
            if len(parts) >= 2 and parts[1].isdigit():
                last = int(parts[1])
    return last


def _latest_page_quality() -> tuple[int, float] | None:
    """(deficient_count, entity_stub_pct) from page_quality_audit's snapshot."""
    snap = OP_DIR / "page-quality-snapshots.md"
    if not snap.exists():
        return None
    last = None
    for line in snap.read_text(errors="replace").splitlines():
        if line.startswith("| 2"):
            c = [x.strip() for x in line.strip("|").split("|")]
            if len(c) >= 4 and c[1].isdigit():
                last = c
    if not last:
        return None
    try:
        return int(last[1]), float(last[3])
    except (ValueError, IndexError):
        return None


def knowledge_namespaces() -> list[str]:
    """Knowledge namespaces counted toward total vault pages. Schema-driven
    (schema.yaml `partitioning.namespaces`, minus `exclude:` dirs); on-disk
    top-level wiki dirs (minus excluded + dot/underscore + dashboards/operational)
    as a fallback when the pack declares none. The engine ships no hardcoded list."""
    schema = schema_lib.governing_schema(VAULT)
    excluded = schema_lib.excluded_dirs(schema) | {"operational", "dashboards"}
    names = schema_lib.knowledge_namespaces(schema) - excluded
    if not names:
        wiki = VAULT / "wiki"
        if wiki.is_dir():
            names = {
                d.name for d in wiki.iterdir()
                if d.is_dir()
                and not d.name.startswith((".", "_"))
                and d.name not in excluded
            }
    return sorted(names)


def total_pages() -> int:
    n = 0
    for sub in knowledge_namespaces():
        d = VAULT / "wiki" / sub
        if d.is_dir():
            n += sum(1 for _ in d.rglob("*.md"))
    return n


def _status(ok: bool, warn: bool = False) -> str:
    return "🟢" if ok else ("🟡" if warn else "🔴")


def _trend_row(key: str, latest, prior) -> str:
    if latest is None:
        return f"| {key} | — | — | — | no data |"
    now_d, now_kv = latest
    now = now_kv.get(key)
    if now is None:
        return f"| {key} | — | — | — | not tracked |"
    if prior is None or prior[1].get(key) is None or prior[0] == now_d:
        return f"| {key} | {now} | — | — | (no prior row) |"
    prior_d, prior_kv = prior
    days = max(1, (now_d - prior_d).days)
    delta_per_day = (now - prior_kv[key]) / days
    if delta_per_day <= -1:
        verdict = f"🟢 draining ({delta_per_day:+.1f}/day)"
    elif delta_per_day <= 0.5:
        verdict = f"🟡 ~flat ({delta_per_day:+.1f}/day)"
    else:
        verdict = f"🔴 GROWING ({delta_per_day:+.1f}/day)"
    return f"| {key} | {now} | {prior_kv[key]} ({prior_d}) | {delta_per_day:+.1f} | {verdict} |"


def render(today: date, q_rows, pages) -> str:
    latest = q_rows[-1] if q_rows else None
    # prior row: closest to (latest_date - window), else earliest available
    prior = None
    if latest and len(q_rows) > 1:
        target = latest[0]
        target_days = TREND_WINDOW_DAYS
        best = None
        for r in q_rows[:-1]:
            gap = (latest[0] - r[0]).days
            if gap <= 0:
                continue
            if best is None or abs(gap - target_days) < abs((latest[0] - best[0]).days - target_days):
                best = r
        prior = best

    lkv = latest[1] if latest else {}
    fm_errors = lkv.get("fm-parse-errors", 0)
    fm_valid_pct = 100.0 * (pages - fm_errors) / pages if pages else 0.0

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = []
    L.append("---")
    L.append("type: dashboard")
    L.append("title: KB Health")
    L.append(f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    L.append("generator: scripts/cron/kb_health.py")
    L.append("---")
    L.append("")
    L.append("# KB Health")
    L.append("")
    L.append(f"_Synced {ts} — aggregated from `operational/queue-snapshots.md` "
             "and the page-quality / field-loss snapshot histories._")
    L.append("")
    src_date = latest[0].isoformat() if latest else "n/a"
    L.append(f"Snapshot source date: **{src_date}** · total vault pages: **{pages:,}**")
    ref_pages = lkv.get("reference-pages")
    if ref_pages:
        L.append("")
        L.append(f"Reference-catalog pages: **{ref_pages:,}** — deterministic imports "
                 "(CVE / ATT&CK / encyclopedia); link-target scaffolding, **excluded from the "
                 "orphan and page-quality debt metrics** (a catalog entry with no inbound links "
                 "yet is waiting to be cited, not a defect).")
    L.append("")

    # ── Top-line ────────────────────────────────────────────────────
    L.append("## Top-line")
    L.append("")
    L.append("| Metric | Value | Threshold | Status |")
    L.append("|---|---|---|---|")
    L.append(f"| Frontmatter-valid | {fm_valid_pct:.2f}% "
             f"({fm_errors} unparseable / {pages:,}) | ≥99.5% | "
             f"{_status(fm_valid_pct >= 99.5, fm_valid_pct >= 99.0)} |")
    yaml_invalid = lkv.get("yaml-invalid", 0)
    L.append(f"| YAML-invalid pages | {yaml_invalid} | 0 | "
             f"{_status(yaml_invalid == 0, yaml_invalid <= 5)} |")
    fl = _latest_field_loss()
    if fl is None:
        L.append("| Curated-field losses (24h) | _no data yet_ | 0 | — |")
    else:
        L.append(f"| Curated-field losses (24h) | {fl} | 0 | {_status(fl == 0)} |")
    pq = _latest_page_quality()
    if pq is not None:
        L.append(f"| Page quality (entity stub%) | {pq[0]:,} deficient · "
                 f"{pq[1]:.0f}% entity stubs | ≤15% | {_status(pq[1] <= 15, pq[1] <= 25)} |")
    L.append("")

    # ── Drain vs accumulation ───────────────────────────────────────
    L.append("## Drain vs accumulation")
    L.append("")
    L.append(f"Are the cleanup queues clearing faster than they fill? "
             f"Δ/day over ~{TREND_WINDOW_DAYS}d. Positive = the backlog is growing "
             f"despite the drains.")
    L.append("")
    L.append("| Queue | now | ~prior | Δ/day | verdict |")
    L.append("|---|---|---|---|---|")
    for key in TREND_KEYS:
        L.append(_trend_row(key, latest, prior))
    L.append("")

    # ── Other queue depths (informational) ──────────────────────────
    if latest:
        L.append("## Other queue depths (latest snapshot)")
        L.append("")
        for k in sorted(lkv):
            if k in TREND_KEYS:
                continue
            L.append(f"- `{k}`: {lkv[k]}")
        L.append("")

    L.append("---")
    L.append("")
    L.append("Thresholds are deterministic heuristics in `kb_health.py`. "
             "History: [[operational/kb-health-snapshots]].")
    return "\n".join(L) + "\n"


def append_history(today: date, q_rows, pages) -> None:
    lkv = q_rows[-1][1] if q_rows else {}
    fm_errors = lkv.get("fm-parse-errors", 0)
    fm_valid = round(100.0 * (pages - fm_errors) / pages, 2) if pages else 0.0
    row = (f"| {today.isoformat()} | {fm_valid} | {lkv.get('broken-wikilinks','')} "
           f"| {lkv.get('orphans','')} | {lkv.get('schema-drift','')} |")
    header = (
        "---\ntype: dashboard\ntitle: KB health snapshots\n---\n\n"
        "# KB health snapshots\n\n"
        "One row/day appended by `kb_health.py`.\n\n"
        "| date | fm-valid% | broken-links | orphans | schema-drift |\n"
        "|---|---|---|---|---|\n"
    )
    OP_DIR.mkdir(parents=True, exist_ok=True)
    if not HISTORY_PATH.exists():
        HISTORY_PATH.write_text(header)
    existing = HISTORY_PATH.read_text(errors="replace")
    # idempotent per day: drop any existing row for today, then append
    lines = [ln for ln in existing.splitlines()
             if not ln.startswith(f"| {today.isoformat()} |")]
    HISTORY_PATH.write_text("\n".join(lines).rstrip() + "\n" + row + "\n")


def main() -> int:
    today = datetime.now(timezone.utc).date()
    q_rows = (parse_queue_snapshots(QUEUE_SNAPSHOTS.read_text(errors="replace"))
              if QUEUE_SNAPSHOTS.exists() else [])
    pages = total_pages()

    DASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASH_PATH.write_text(render(today, q_rows, pages))
    append_history(today, q_rows, pages)

    print("=== kb-health ===")
    print(f"  vault: {VAULT}")
    print(f"  pages: {pages}")
    if q_rows:
        print(f"  queue snapshot: {q_rows[-1][0]}  {q_rows[-1][1]}")
    print(f"  wrote: {DASH_PATH.relative_to(VAULT)}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
