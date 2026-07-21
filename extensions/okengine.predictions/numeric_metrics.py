"""Deterministic numeric substrate beneath the base-rate/output-outcome agent lanes (#223)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

import yaml

import pred_lib as P

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_REF = re.compile(r"\[\[([^\]|#]+)")
OUTCOMES = {"confirmed": 1.0, "refuted": 0.0, "partial": 0.5}


def _today() -> date:
    return date.fromisoformat(P.today_iso())


def _data(vault: Path) -> Path:
    root = Path(os.environ.get("HERMES_DATA", str(vault / ".state")))
    return root / "state" / "okengine.predictions"


def _events_path(vault: Path) -> Path:
    root = Path(os.environ.get("HERMES_DATA", str(vault / ".state")))
    return root / "state" / "okengine.events" / "event-scores.jsonl"


def _jsonl(path: Path) -> list[dict]:
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    out.append(row)
            except json.JSONDecodeError:
                continue
    return out


def _event_rows(vault: Path) -> list[dict]:
    """Event analytics must ignore the source-intrinsic compatibility rows in the shared sidecar."""
    return [row for row in _jsonl(_events_path(vault)) if row.get("score_scope") != "source"]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                           for row in rows), encoding="utf-8")
    tmp.replace(path)


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _refs(value) -> list[str]:
    values = value if isinstance(value, list) else [value] if value else []
    out = []
    for raw in values:
        found = _REF.findall(str(raw))
        for ref in found or [str(raw)]:
            ref = ref.strip().strip("/")
            if ref.endswith(".md"):
                ref = ref[:-3]
            if ref:
                out.append(ref.lower())
    return out


def _slug(value) -> str:
    refs = _refs(value)
    return refs[0].split("/")[-1] if refs else ""


def _predictions(vault: Path) -> list[dict]:
    rows = []
    for path, fm in P.predictions(vault):
        rows.append({
            "ref": path.relative_to(vault / "wiki").with_suffix("").as_posix().lower(),
            "fm": fm, "status": str(fm.get("status") or "").lower(),
            "outcome": OUTCOMES.get(str(fm.get("status") or "").lower()),
            "horizon": str(fm.get("horizon") or "(unset)"),
            "subject": _slug(fm.get("subject")),
            "basis": _refs(fm.get("basis") or fm.get("sources") or fm.get("source")),
            "made_on": _date(fm.get("made_on") or fm.get("created")),
        })
    return rows


def _source_metadata(vault: Path) -> dict[str, dict]:
    out = {}
    for path in P.iter_pages(vault, "sources"):
        fm = P.read_fm(path)
        rel = path.relative_to(vault / "wiki").with_suffix("").as_posix().lower()
        meta = {"signal_class": str(fm.get("signal_class") or "(unset)"),
                "publisher": str(fm.get("publisher") or "(unset)")}
        out[rel] = meta
        out[path.stem.lower()] = meta
    return out


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    pos = q * (len(values) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(values) - 1)
    return values[lo] * (hi - pos) + values[hi] * (pos - lo)


def _id(kind: str, label: str) -> str:
    return hashlib.sha256(f"{kind}|{label}".encode()).hexdigest()[:16]


def _rate(kind: str, label: str, n: int, value, unit: str, today: date,
          threshold: int, **extras) -> dict:
    return {"rate_id": _id(kind, label), "rate_kind": kind, "class_label": label,
            "n_observations": n, "value": value, "value_unit": unit,
            "small_n": n < threshold, "computed_at": today.isoformat(), **extras}


def _event_scores(event: dict) -> dict:
    return event.get("scores") if isinstance(event.get("scores"), dict) else {}


def base_rate_rows(events: list[dict], predictions: list[dict], sources: dict[str, dict],
                   today: date, lookback: int = 90, outcome_min: int = 5,
                   descriptive_min: int = 10) -> list[dict]:
    """Compute families A–E.  A–C/E degrade to empty without #220; D remains available."""
    cutoff, months = today - timedelta(days=lookback), lookback / 30
    recent = [event for event in events if _date(event.get("date")) and
              _date(event.get("date")) >= cutoff]
    rows = []

    # A — event frequency and materiality distribution by pack event type.
    by_type = defaultdict(list)
    for event in recent:
        by_type[str(event.get("event_type") or "(unknown)")].append(event)
    for event_type, grouped in sorted(by_type.items()):
        mats = [float(_event_scores(event).get("materiality")) for event in grouped
                if _event_scores(event).get("materiality") is not None]
        rows.append(_rate("event-frequency", event_type, len(grouped),
                          round(len(grouped) / months, 3), "events_per_month", today,
                          descriptive_min, materiality_p50=round(median(mats), 3) if mats else None,
                          materiality_p90=round(_quantile(mats, .9), 3) if mats else None,
                          lookback_days=lookback))

    # B — per-entity frequency, bounded to the 30 most active.
    by_entity = defaultdict(list)
    for event in recent:
        by_entity[str(event.get("entity") or "(unknown)")].append(event)
    ranked_entities = sorted(by_entity.items(), key=lambda item: (-len(item[1]), item[0]))[:30]
    for entity, grouped in ranked_entities:
        kinds = Counter(str(event.get("event_type") or "(unknown)") for event in grouped)
        mats = [float(_event_scores(event).get("materiality") or 0) for event in grouped]
        relevance = [float(_event_scores(event).get("watchlist_relevance") or 0)
                     for event in grouped]
        rows.append(_rate("entity-frequency", f"entities/{entity}", len(grouped),
                          round(len(grouped) / months, 3), "events_per_month", today,
                          descriptive_min, dominant_event_type=kinds.most_common(1)[0][0],
                          avg_materiality=round(sum(mats) / len(mats), 3),
                          on_watchlist=any(value > 0 for value in relevance),
                          lookback_days=lookback))

    # C — event coverage by an explicitly cited event/source OR any prediction on its entity.
    pred_refs = {ref for pred in predictions for ref in pred["basis"]}
    predicted_entities = {pred["subject"] for pred in predictions if pred["subject"]}
    for event_type, grouped in sorted(by_type.items()):
        covered = sum(1 for event in grouped if
                      str(event.get("event_id") or "").lower() in pred_refs or
                      str(event.get("source") or "").lower() in pred_refs or
                      event.get("entity") in predicted_entities)
        rows.append(_rate("event-coverage", event_type, len(grouped),
                          round(covered / len(grouped), 3), "fraction_with_prediction", today,
                          descriptive_min, n_with_prediction=covered, lookback_days=lookback))
    if recent:
        coverage_rows = [row for row in rows if row["rate_kind"] == "event-coverage"]
        covered = sum(row["n_with_prediction"] for row in coverage_rows)
        rows.append(_rate("event-coverage", "(all-event-types)", len(recent),
                          round(covered / len(recent), 3), "fraction_with_prediction", today,
                          descriptive_min, n_with_prediction=covered, lookback_days=lookback))

    # D — resolved outcomes along three axes and their joint comparison class.
    event_type_for_ref = {}
    for event in events:
        for ref in (event.get("event_id"), event.get("source")):
            if ref:
                event_type_for_ref[str(ref).lower()] = str(event.get("event_type") or "(unknown)")
                event_type_for_ref[str(ref).split("/")[-1].lower()] = \
                    str(event.get("event_type") or "(unknown)")
    groups = defaultdict(list)
    for pred in predictions:
        if pred["outcome"] is None:
            continue
        signal_classes = Counter()
        event_types = Counter()
        for ref in pred["basis"]:
            meta = sources.get(ref) or sources.get(ref.split("/")[-1])
            signal_classes[(meta or {}).get("signal_class", "(unset)")] += 1
            event_types[event_type_for_ref.get(ref,
                        event_type_for_ref.get(ref.split("/")[-1], "(no-event-basis)"))] += 1
        signal_class = signal_classes.most_common(1)[0][0] if signal_classes else "(no-basis)"
        event_type = event_types.most_common(1)[0][0] if event_types else "(no-event-basis)"
        labels = (f"horizon={pred['horizon']}", f"basis-signal-class={signal_class}",
                  f"basis-event-type={event_type}",
                  f"horizon={pred['horizon']}|basis-signal-class={signal_class}|"
                  f"basis-event-type={event_type}")
        for label in labels:
            groups[label].append(pred["outcome"])
    resolved = [pred["outcome"] for pred in predictions if pred["outcome"] is not None]
    if resolved:
        groups["(all-resolved)"] = resolved
    for label, outcomes in sorted(groups.items()):
        rows.append(_rate("outcome-rate", label, len(outcomes),
                          round(sum(outcomes) / len(outcomes), 3), "hit_rate", today,
                          outcome_min, n_hits=sum(outcomes)))

    # E — publisher volume/strength and how often it stands alone as prediction basis.
    by_publisher = defaultdict(list)
    publisher_for_ref = {}
    for event in recent:
        publisher = str(event.get("publisher") or "(unset)")
        by_publisher[publisher].append(event)
        for ref in (event.get("event_id"), event.get("source")):
            if ref:
                publisher_for_ref[str(ref).lower()] = publisher
                publisher_for_ref[str(ref).split("/")[-1].lower()] = publisher
    sole = Counter()
    basis_uses = Counter()
    for pred in predictions:
        pubs = [publisher_for_ref.get(ref, publisher_for_ref.get(ref.split("/")[-1]))
                for ref in pred["basis"]]
        pubs = [pub for pub in pubs if pub]
        for pub in set(pubs):
            basis_uses[pub] += 1
        if len(pred["basis"]) == 1 and pubs:
            sole[pubs[0]] += 1
    for publisher, grouped in sorted(by_publisher.items()):
        strengths = [float(_event_scores(event).get("signal_strength") or 0)
                     for event in grouped]
        uses = basis_uses[publisher]
        rows.append(_rate("publisher-mix", publisher, len(grouped),
                          round(sum(strengths) / len(strengths), 3), "avg_signal_strength",
                          today, descriptive_min, n_prediction_basis_uses=uses,
                          n_sole_basis=sole[publisher],
                          sole_basis_fraction=round(sole[publisher] / uses, 3) if uses else 0.0,
                          lookback_days=lookback))
    return rows


def render_base_rates(rows: list[dict], today: date, has_events: bool) -> str:
    by_kind = defaultdict(list)
    for row in rows:
        by_kind[row["rate_kind"]].append(row)
    lines = ["---", "type: dashboard", "title: Numeric base-rate metrics",
             f"updated: {today.isoformat()}", "---", "", "# Numeric base-rate metrics", "",
             "_Deterministic denominators beneath the narrative base-rates agent lane._", ""]
    if not has_events:
        lines += ["> Event-score sidecar unavailable: families A–C and E are empty; family D is "
                  "still computed from predictions.", ""]
    titles = {"event-frequency": "A. Event frequency", "entity-frequency": "B. Entity frequency",
              "event-coverage": "C. Event coverage", "outcome-rate": "D. Outcome rates",
              "publisher-mix": "E. Publisher mix"}
    for kind, title in titles.items():
        lines += [f"## {title}", "", "| class | N | value | unit | small-N |", "|---|---:|---:|---|---|"]
        for row in by_kind[kind]:
            value = f"{row['value']:.3f}" if isinstance(row["value"], float) else str(row["value"])
            lines.append(f"| {row['class_label']} | {row['n_observations']} | {value} | "
                         f"{row['value_unit']} | {'⚠' if row['small_n'] else ''} |")
        if not by_kind[kind]:
            lines.append("| _(no observations)_ | 0 | — | — | |")
        lines.append("")
    return "\n".join(lines)


def compute_base_rates(vault: Path | None = None) -> tuple[list[dict], Path, Path]:
    vault, today = vault or P.vault(), _today()
    events = _event_rows(vault)
    predictions = _predictions(vault)
    rows = base_rate_rows(events, predictions, _source_metadata(vault), today,
                          int(os.environ.get("BASE_RATES_LOOKBACK_DAYS", "90")),
                          int(os.environ.get("BASE_RATES_SMALL_N_OUTCOME", "5")),
                          int(os.environ.get("BASE_RATES_SMALL_N_DESCRIPTIVE", "10")))
    state = _data(vault) / "base-rates.jsonl"
    dashboard = vault / "wiki" / "dashboards" / "base-rate-metrics.md"
    _write_jsonl(state, rows)
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(render_base_rates(rows, today, bool(events)), encoding="utf-8")
    return rows, state, dashboard


def _page(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    match = _FM.match(text)
    if not match:
        return {}, text
    try:
        fm = yaml.safe_load(match.group(1))
    except Exception:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), text[match.end():]


def _metric(label: str, n: int, value: float, today: date, threshold: int = 5, **extras) -> dict:
    return {"metric_id": _id("output-outcome", label), "metric_kind": "output-outcome",
            "metric_label": label, "n_observations": n, "value": round(value, 3),
            "value_unit": "fraction", "small_n": n < threshold,
            "computed_at": today.isoformat(), **extras}


def output_outcome_rows(vault: Path, events: list[dict], predictions: list[dict], today: date,
                        lookback: int = 90, materiality_threshold: float = .6) -> list[dict]:
    outputs = []
    for path in P.iter_pages(vault, "briefings"):
        fm, body = _page(path)
        output_date = None
        for field in ("date", "created", "published", "updated"):
            output_date = _date(fm.get(field))
            if output_date:
                break
        if not output_date:
            output_date = _date(path.stem)
        if output_date:
            outputs.append({"ref": path.relative_to(vault / "wiki").with_suffix("").as_posix(),
                            "date": output_date, "refs": set(_refs(body)),
                            "entities": {ref.split("/")[-1] for ref in _refs(body)
                                         if ref.startswith("entities/")}})
    source_pairs = {(output["ref"], ref, output["date"]) for output in outputs
                    for ref in output["refs"] if ref.startswith("sources/")}
    yielded_sources = 0
    for _, source, output_date in source_pairs:
        if any(source in pred["basis"] and pred["made_on"] and pred["made_on"] >= output_date
               for pred in predictions):
            yielded_sources += 1
    entity_pairs = {(output["ref"], entity, output["date"]) for output in outputs
                    for entity in output["entities"]}
    yielded_entities = 0
    for _, entity, output_date in entity_pairs:
        if any(event.get("entity") == entity and _date(event.get("date")) and
               _date(event.get("date")) >= output_date and
               float(_event_scores(event).get("materiality") or 0) >= materiality_threshold
               for event in events):
            yielded_entities += 1
    cutoff = today - timedelta(days=lookback)
    material_events = [event for event in events if _date(event.get("date")) and
                       _date(event.get("date")) >= cutoff and
                       float(_event_scores(event).get("materiality") or 0) >= materiality_threshold]
    mentioned = {ref for output in outputs for ref in output["refs"]}
    missed = sum(1 for event in material_events if
                 str(event.get("source") or "").lower() not in mentioned and
                 str(event.get("event_id") or "").lower() not in mentioned)
    return [
        _metric("briefing_source_to_prediction_basis_yield", len(source_pairs),
                yielded_sources / len(source_pairs) if source_pairs else 0, today,
                n_yielded=yielded_sources, n_outputs=len(outputs)),
        _metric("briefing_entity_subsequent_material_event_yield", len(entity_pairs),
                yielded_entities / len(entity_pairs) if entity_pairs else 0, today,
                n_yielded=yielded_entities, n_outputs=len(outputs),
                materiality_threshold=materiality_threshold),
        _metric("high_materiality_event_briefing_coverage_miss_rate", len(material_events),
                missed / len(material_events) if material_events else 0, today,
                n_missed=missed, n_covered=len(material_events) - missed,
                materiality_threshold=materiality_threshold, lookback_days=lookback),
    ]


def render_output_outcomes(rows: list[dict], today: date, has_events: bool) -> str:
    lines = ["---", "type: dashboard", "title: Numeric output-outcome metrics",
             f"updated: {today.isoformat()}", "---", "", "# Numeric output-outcome metrics", "",
             "_Deterministic joins beneath the narrative output-outcome agent lane._", ""]
    if not has_events:
        lines += ["> Event-score sidecar unavailable: event-dependent yields have zero "
                  "observations; source-to-prediction yield still computes.", ""]
    lines += ["| metric | N | value | small-N |", "|---|---:|---:|---|"]
    for row in rows:
        lines.append(f"| {row['metric_label']} | {row['n_observations']} | "
                     f"{row['value']:.1%} | {'⚠' if row['small_n'] else ''} |")
    lines.append("")
    return "\n".join(lines)


def compute_output_outcomes(vault: Path | None = None) -> tuple[list[dict], Path, Path]:
    vault, today = vault or P.vault(), _today()
    events, predictions = _event_rows(vault), _predictions(vault)
    rows = output_outcome_rows(vault, events, predictions, today,
                               int(os.environ.get("OUTCOME_EVAL_LOOKBACK_DAYS", "90")),
                               float(os.environ.get("OUTCOME_EVAL_HIGH_MAT", ".6")))
    state = _data(vault) / "output-outcome-scores.jsonl"
    dashboard = vault / "wiki" / "dashboards" / "output-outcome-metrics.md"
    _write_jsonl(state, rows)
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(render_output_outcomes(rows, today, bool(events)), encoding="utf-8")
    return rows, state, dashboard
