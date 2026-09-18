#!/usr/bin/env python3
"""Render collection freshness and source coverage into the Cockpit Ops surface."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collection_ledger  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
LEDGER = Path(os.environ.get("COLLECTION_LEDGER_DIR", "/opt/data/collection"))
RETENTION_DAYS = int(os.environ.get("COLLECTION_LEDGER_RETENTION_DAYS", "90"))
STALE_HOURS = float(os.environ.get("COLLECTION_STALE_HOURS", "26"))
# okengine#748 — raw files per distinct article. 1.0 means every raw item is its own article.
# A ratio above this is the capture layer minting revisions for documents that did not change;
# the incident that motivated the metric ran at 12.7 (82,769 files, 6,531 articles) with 639
# copies of one blog post, and nothing reported it because nothing measured it.
CHURN_RATIO_WARN = float(os.environ.get("COLLECTION_CHURN_RATIO_WARN", "1.5"))
_REVISION_RE = re.compile(r"-revision-[0-9a-f]{8}(?:-[0-9a-f]{8})?(?=\.[^.]+$)")


def _show(value, suffix=""):
    return "unknown" if value is None else f"{value}{suffix}"


def _latency(ms):
    if ms is None:
        return "unknown"
    hours = ms / 3_600_000
    return f"{hours:.1f}h" if hours < 72 else f"{hours / 24:.1f}d"


def raw_churn(vault: Path) -> dict:
    """Raw files per distinct article, and the worst offender.

    The standing detector for okengine#748. Counting FILES was how the duplication stayed
    invisible: every surface reported a healthy, growing corpus while 92% of it was re-captures
    of documents that had not changed.

    Returns `ratio: None` when there is no raw tree — unknown, never a healthy-looking zero.
    """
    raw = Path(vault) / "raw"
    if not raw.is_dir():
        return {"files": 0, "articles": 0, "ratio": None, "worst": 0, "worst_article": ""}
    groups: dict[str, int] = {}
    files = 0
    for path in raw.rglob("*.md"):
        if any(part.startswith(".") for part in path.parts):
            continue
        files += 1
        key = _REVISION_RE.sub("", path.name)
        groups[key] = groups.get(key, 0) + 1
    if not groups:
        return {"files": files, "articles": 0, "ratio": None, "worst": 0, "worst_article": ""}
    worst_article, worst = max(groups.items(), key=lambda kv: (kv[1], kv[0]))
    return {"files": files, "articles": len(groups), "ratio": files / len(groups),
            "worst": worst, "worst_article": worst_article}


def render(*, vault: Path = VAULT, ledger: Path = LEDGER, now=None) -> Path:
    now_dt = now or datetime.now(timezone.utc)
    sources = collection_ledger.load_sources(ledger)
    attempts = collection_ledger.load_attempts(ledger, now=now_dt, retention_days=RETENTION_DAYS)
    rows = collection_ledger.project_current(
        sources, attempts, now=now_dt, stale_after_hours=STALE_HOURS)
    collection_ledger.prune(ledger, now=now_dt, retention_days=RETENTION_DAYS)
    statuses = {key: sum(row["status"] == key for row in rows)
                for key in ("healthy", "partial", "failing", "stale", "unknown")}
    kinds = {key: sum(collection_ledger.origin_class_of(row) == key for row in rows)
             for key in ("primary", "secondary", "unknown")}
    independent = {
        "yes": sum(row.get("independent_origin") is True for row in rows),
        "no": sum(row.get("independent_origin") is False for row in rows),
        "unknown": sum(row.get("independent_origin") is None for row in rows),
    }
    recent = [row for row in attempts if row.get("finished_at")]
    totals = {field: sum(int(row.get(field, 0)) for row in recent)
              for field in collection_ledger.COUNT_FIELDS}
    yield_summary = (
        f"fetched {totals['fetched']} · accepted {totals['accepted']} · "
        f"rejected {totals['rejected']} · deduped {totals['deduped']} · "
        f"dead letters {totals['dead_letter']}" if recent else
        "unknown — no collection attempts recorded"
    )
    churn = raw_churn(vault)
    generated = now_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = [
        "---", "type: dashboard", "id: dashboard:collection-health",
        'title: "Collection health and source coverage"',
        'summary: "Configured-source freshness, yield, failures, provenance mix, and ingest latency."',
        f"updated: {generated}", "---", "", "# Collection health and source coverage", "",
        f"Generated {generated}. Telemetry retention: {RETENTION_DAYS} days.", "",
        "## Current state", "",
        f"- configured sources: {len(rows)}",
        f"- healthy: {statuses['healthy']} · partial: {statuses['partial']} · failing: {statuses['failing']} · stale: {statuses['stale']} · unknown: {statuses['unknown']}",
        f"- recent yield: {yield_summary}",
        "", "## Provenance coverage", "",
        f"- source class: primary {kinds['primary']} · secondary {kinds['secondary']} · unknown {kinds['unknown']}",
        f"- independent origin: yes {independent['yes']} · no {independent['no']} · unknown {independent['unknown']}",
        "", "## Capture churn", "",
        (f"- raw files: {churn['files']} across {churn['articles']} distinct article(s)"
         if churn["articles"] else "- raw files: no raw tree — ratio unknown"),
        (f"- files per article: {churn['ratio']:.2f}"
         + (f"  **ABOVE {CHURN_RATIO_WARN:.2f} — the capture layer is minting revisions for "
            f"unchanged documents (okengine#748); worst: {churn['worst']} copies of "
            f"`{churn['worst_article']}`**" if churn["ratio"] > CHURN_RATIO_WARN else "  (ok)")
         if churn["ratio"] is not None else "- files per article: unknown"),
        "", "Missing or stale telemetry is shown as `unknown`/`stale`; it is never interpreted as zero or healthy.", "",
        "## Configured sources", "",
        "| Status | Source | Connector | Last attempt | Last success | Failures | Fetched → accepted | Dead letters | Pub→ingest | Class | Independent |",
        "|---|---|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        independent_label = ("yes" if row.get("independent_origin") is True else
                             "no" if row.get("independent_origin") is False else "unknown")
        counts = ("unknown" if row.get("fetched") is None else
                  f"{row['fetched']} → {row.get('accepted', 0)}")
        lines.append(
            f"| {row['status']} | {row.get('label') or row['source_id']} | {row['connector_id']} | "
            f"{_show(row.get('last_attempt'))} | {_show(row.get('last_success'))} | "
            f"{_show(row.get('consecutive_failures'))} | {counts} | {_show(row.get('dead_letter'))} | "
            f"{_latency(row.get('publication_to_ingest_ms'))} | {collection_ledger.origin_class_of(row)} | {independent_label} |"
        )
    if not rows:
        lines.append("| unknown | No configured sources registered | — | unknown | unknown | unknown | unknown | unknown | unknown | unknown | unknown |")
    lines += ["", "## Telemetry contract", "",
              "The ledger stores stable source/connector identifiers, timestamps, counters, error categories, latency, and opaque checkpoint hashes. Credentials, request bodies, response bodies, and private query text are prohibited.", ""]
    out = Path(vault) / "wiki" / "operational" / "collection-health.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, out)
    return out


def main() -> int:
    out = render()
    print(f"collection-health: wrote {out}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
