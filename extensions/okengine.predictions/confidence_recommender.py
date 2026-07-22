#!/usr/bin/env python3
"""Deterministic, recommendation-only confidence updates (#212).

Pending structured evidence records (``confidence_before == confidence_after``) are joined
to the event-scoring substrate by source path.  The rule is the constrained origin-system
rule: weighted inputs, a neutral offset, per-event/cycle clamps, and final confidence bounds.
It never edits a prediction.  Recommendations remain present until the regrade agent records
its disposition by changing ``confidence_after`` or adding an explicit no-change disposition
marker to the evidence note.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

SIGNAL_WEIGHT = float(os.environ.get(
    "PREDICTION_RECOMMENDER_SIGNAL_WEIGHT",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_SIGNAL_WEIGHT", "0.40"),
))
QUALITY_WEIGHT = float(os.environ.get(
    "PREDICTION_RECOMMENDER_SOURCE_QUALITY_WEIGHT",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_SOURCE_QUALITY_WEIGHT", "0.30"),
))
CORROBORATION_WEIGHT = float(os.environ.get(
    "PREDICTION_RECOMMENDER_CORROBORATION_WEIGHT",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_CORROBORATION_WEIGHT", "0.05"),
))
CONTRADICTION_WEIGHT = float(os.environ.get(
    "PREDICTION_RECOMMENDER_CONTRADICTION_WEIGHT",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_CONTRADICTION_WEIGHT", "0.30"),
))
NEUTRAL_OFFSET = float(os.environ.get(
    "PREDICTION_RECOMMENDER_NEUTRAL_OFFSET",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_NEUTRAL_OFFSET", "0.40"),
))
PER_EVENT_CAP = float(os.environ.get(
    "PREDICTION_RECOMMENDER_PER_EVENT_CAP",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_PER_EVENT_CAP", "0.15"),
))
PER_CYCLE_CAP = float(os.environ.get(
    "PREDICTION_RECOMMENDER_PER_CYCLE_CAP",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_PER_CYCLE_CAP", "0.20"),
))
CONFIDENCE_FLOOR = float(os.environ.get("PREDICTION_RECOMMENDER_CONFIDENCE_FLOOR", "0.05"))
CONFIDENCE_CEILING = float(os.environ.get("PREDICTION_RECOMMENDER_CONFIDENCE_CEILING", "0.95"))

_DIRECTION_PENALTY = {"reinforces": 0.0, "neutral": 0.5, "partial": 0.75,
                      "contradicts": 1.0}


def _norm(value) -> str:
    text = str(value or "").strip()
    if text.startswith("[[") and text.endswith("]]" ):
        text = text[2:-2]
    text = text.split("|", 1)[0].split("#", 1)[0].strip().lstrip("/")
    if text.startswith("wiki/"):
        text = text[5:]
    return text[:-3] if text.endswith(".md") else text


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_dir(vault: Path) -> Path:
    root = Path(os.environ.get("HERMES_DATA", str(vault / ".state")))
    return root / "state" / "okengine.predictions"


def _event_scores(vault: Path) -> dict[str, list[dict]]:
    path = Path(os.environ.get(
        "PREDICTION_RECOMMENDER_EVENT_SCORES",
        str(Path(os.environ.get("HERMES_DATA", str(vault / ".state"))) /
            "state" / "okengine.events" / "event-scores.jsonl"),
    ))
    event_rows: dict[str, list[dict]] = defaultdict(list)
    source_rows: dict[str, list[dict]] = defaultdict(list)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        source = _norm(row.get("source")) if isinstance(row, dict) else ""
        if source:
            target = source_rows if row.get("score_scope") == "source" else event_rows
            target[source].append(row)
    # Prefer the one intrinsic source vector when present. Event-derived rows remain a compatibility
    # fallback for older event-scoring artifacts, but must not multiply one evidence record into
    # several confidence moves merely because several events cite the same source.
    return {source: source_rows.get(source) or rows for source, rows in event_rows.items()} | {
        source: rows for source, rows in source_rows.items() if source not in event_rows
    }


def event_delta(event: dict, direction: str) -> tuple[float, dict] | None:
    scores = event.get("scores") if isinstance(event.get("scores"), dict) else {}
    signal = _number(scores.get("signal_strength"))
    quality = _number(scores.get("source_reliability_score"))
    corroboration = _number(scores.get("corroboration_count"))
    penalty = _DIRECTION_PENALTY.get(direction)
    if penalty is None:
        # An unrecognized `direction` (outside the sanctioned evidence[].direction vocabulary) was
        # SILENTLY dropped — the event contributed nothing to the recommendation and the vocabulary
        # drift was invisible (okengine#326 [23]). Surface it on the lane's stderr; the event stays
        # excluded (an unknown direction has no defined penalty), but the drift is now visible.
        sys.stderr.write(
            f"confidence_recommender: unrecognized evidence direction {direction!r} "
            f"(known: {sorted(_DIRECTION_PENALTY)}) — event excluded; declare it in the schema's "
            f"evidence[].direction enum and the penalty map if intended\n")
        return None
    if signal is None or quality is None or corroboration is None:
        return None
    drivers = {"source_quality": quality, "signal_strength": signal,
               "corroboration": int(corroboration), "contradiction_penalty": penalty}
    raw = (SIGNAL_WEIGHT * signal + QUALITY_WEIGHT * quality +
           CORROBORATION_WEIGHT * min(corroboration, 5) -
           CONTRADICTION_WEIGHT * penalty - NEUTRAL_OFFSET)
    return max(-PER_EVENT_CAP, min(PER_EVENT_CAP, raw)), drivers


def recommendations(vault: Path) -> list[dict]:
    scores_by_source = _event_scores(vault)
    out = []
    for path, fm in P.predictions(vault):
        if not P.is_open(fm):
            continue
        current = _number(fm.get("confidence"))
        evidence = fm.get("evidence") if isinstance(fm.get("evidence"), list) else []
        if current is None:
            continue
        events = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                continue
            before, after = _number(item.get("confidence_before")), _number(item.get("confidence_after"))
            note = str(item.get("note") or "").lower()
            disposed = ("[recommender-accepted]" in note or
                        "[recommender-deviation:" in note)
            # Equal before/after is the explicit pending-disposition marker.
            if before is None or after is None or abs(before - after) > 1e-9 or disposed:
                continue
            source = _norm(item.get("source"))
            for event in scores_by_source.get(source, []):
                calculated = event_delta(event, str(item.get("direction") or "").lower())
                if calculated:
                    delta, drivers = calculated
                    events.append({"evidence_index": index, "source": source,
                                   "event_id": event.get("event_id"),
                                   "per_event_delta": round(delta, 4),
                                   "update_driver": drivers})
        if not events:
            continue
        total = max(-PER_CYCLE_CAP, min(PER_CYCLE_CAP,
                    sum(event["per_event_delta"] for event in events)))
        suggested = max(CONFIDENCE_FLOOR, min(CONFIDENCE_CEILING, current + total))
        rel = path.relative_to(vault / "wiki").with_suffix("").as_posix()
        out.append({
            "recommendation_id": hashlib.sha256(
                (rel + "|" + "|".join(str(e["event_id"]) for e in events)).encode()).hexdigest()[:16],
            "proposition": rel, "confidence_before": round(current, 3),
            "confidence_after_suggested": round(suggested, 3),
            "delta_suggested": round(suggested - current, 3),
            "rule_inputs": {"signal_weight": SIGNAL_WEIGHT, "source_quality_weight": QUALITY_WEIGHT,
                            "corroboration_weight": CORROBORATION_WEIGHT,
                            "contradiction_weight": CONTRADICTION_WEIGHT,
                            "neutral_offset": NEUTRAL_OFFSET, "per_event_cap": PER_EVENT_CAP,
                            "per_cycle_cap": PER_CYCLE_CAP,
                            "confidence_bounds": [CONFIDENCE_FLOOR, CONFIDENCE_CEILING]},
            "events": events,
        })
    return sorted(out, key=lambda row: (-abs(row["delta_suggested"]), row["proposition"]))


def write_outputs(vault: Path, rows: list[dict]) -> tuple[Path, Path]:
    state = _state_dir(vault)
    state.mkdir(parents=True, exist_ok=True)
    jsonl = state / "confidence-recommendations.jsonl"
    tmp = jsonl.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                           for row in rows), encoding="utf-8")
    os.replace(tmp, jsonl)
    dashboard = vault / "wiki" / "dashboards" / "confidence-recommendations.md"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: dashboard", "title: Confidence recommendations",
             "generator: extensions/okengine.predictions/confidence_recommender.py", "---", "",
             "# Confidence recommendations", "",
             "Recommendation-only deterministic deltas; the regrade agent retains the pen.", "",
             "| proposition | confidence | suggested | delta | scored events |", "|---|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| [[{row['proposition']}]] | {row['confidence_before']:.3f} | "
                     f"{row['confidence_after_suggested']:.3f} | {row['delta_suggested']:+.3f} | "
                     f"{len(row['events'])} |")
    if not rows:
        lines += ["", "No pending evidence records had complete scored inputs."]
    dashboard.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl, dashboard


def main() -> int:
    vault = P.vault()
    rows = recommendations(vault)
    jsonl, _ = write_outputs(vault, rows)
    print(f"confidence-recommender: {len(rows)} recommendation(s) -> {jsonl}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
