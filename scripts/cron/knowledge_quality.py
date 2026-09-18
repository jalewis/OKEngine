"""Knowledge-quality rollups from durable lane receipts and adjudication snapshots.

The rollup never turns missing evidence into zero.  Every metric is an object with a
``state`` of ``measured`` or ``unknown`` so dashboards and release evidence cannot
accidentally present an instrument failure as good product quality.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DISPOSITIONS = (
    "accepted", "merged", "updated", "rejected-out-of-scope",
    "insufficient-evidence", "duplicate", "deferred-for-review", "failed",
)
LEGACY_DISPOSITIONS = {
    "skipped": "rejected-out-of-scope",
    "rejected": "insufficient-evidence",
    "deferred": "deferred-for-review",
}
METRICS = (
    "evidence_to_knowledge_latency_hours", "disposition_completeness",
    "disposition_correctness", "unsupported_claim_rate", "canonical_duplicate_rate",
    "contradiction_age_hours", "review_precision", "review_age_hours",
    "stale_knowledge_rate", "cost_per_accepted_update", "retrieval_usefulness",
)


def unknown(reason: str) -> dict:
    return {"state": "unknown", "value": None, "reason": reason}


def measured(value: float, *, numerator: int | None = None,
             denominator: int | None = None, unit: str | None = None) -> dict:
    result = {"state": "measured", "value": value}
    if numerator is not None:
        result.update(numerator=numerator, denominator=denominator)
    if unit:
        result["unit"] = unit
    return result


def canonical_disposition(value: object) -> str | None:
    if value in DISPOSITIONS:
        return str(value)
    return LEGACY_DISPOSITIONS.get(str(value))


def _receipts(root: Path) -> list[dict]:
    records = []
    if not root.is_dir():
        return records
    for path in root.glob("*/*.json"):  # glob-ok: flat receipt registry per lane
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def rollup(receipt_root: Path, adjudication_path: Path | None = None) -> dict:
    receipts = _receipts(receipt_root)
    counts = {name: 0 for name in DISPOSITIONS}
    selected = disposed = 0
    for receipt in receipts:
        receipt_counts = receipt.get("counts") if isinstance(receipt.get("counts"), dict) else {}
        selected += int(receipt_counts.get("selected") or 0)
        for raw, count in receipt_counts.items():
            canonical = canonical_disposition(raw)
            if raw in LEGACY_DISPOSITIONS and canonical in receipt_counts:
                continue  # compatibility alias emitted beside its canonical counter
            if canonical:
                counts[canonical] += int(count or 0)
                disposed += int(count or 0)

    metrics = {name: unknown("no adjudicated telemetry snapshot") for name in METRICS}
    if selected:
        metrics["disposition_completeness"] = measured(
            disposed / selected, numerator=disposed, denominator=selected)
        metrics["canonical_duplicate_rate"] = measured(
            counts["duplicate"] / selected, numerator=counts["duplicate"], denominator=selected)
    else:
        metrics["disposition_completeness"] = unknown("no selected receipt items")
        metrics["canonical_duplicate_rate"] = unknown("no selected receipt items")

    if adjudication_path and adjudication_path.is_file():
        try:
            snapshot = json.loads(adjudication_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snapshot = None
        supplied = snapshot.get("metrics") if isinstance(snapshot, dict) else None
        if isinstance(supplied, dict):
            for name in METRICS:
                value = supplied.get(name)
                if isinstance(value, dict) and value.get("state") in {"measured", "unknown"}:
                    metrics[name] = value

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "receipts": len(receipts), "selected": selected, "disposed": disposed,
        "dispositions": counts, "metrics": metrics,
    }


def markdown(report: dict) -> list[str]:
    lines = ["## Knowledge quality", "",
             f"Receipts: {report['receipts']} · selected: {report['selected']} · "
             f"disposed: {report['disposed']}", "",
             "| Measure | State | Value |", "|---|---|---:|"]
    for name, metric in report["metrics"].items():
        value = metric.get("value")
        rendered = "unknown" if value is None else f"{value:.4g}"
        lines.append(f"| {name.replace('_', ' ')} | {metric['state']} | {rendered} |")
    lines += ["", "_Unknown is not zero; it means the required receipt or adjudication evidence "
              "was unavailable._", ""]
    return lines
