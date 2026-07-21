#!/usr/bin/env python3
"""Pack-configured eight-vector event scoring and typed extraction (#220).

Mechanism lives here; vocabulary and weights live in pack ``schema.yaml``.  Derived outputs:

* ``$HERMES_DATA/state/okengine.events/event-scores.jsonl``
* ``$HERMES_DATA/state/okengine.events/typed-events/<page-type>.jsonl``
* ``wiki/dashboards/event-scoring.md``

The sidecar contains event rows plus source-intrinsic rows keyed by ``source``. This lets
downstream evidence consumers score any cited source, not only sources already attached to a
pack-declared event. The lane is deterministic/no-agent and creates no canonical pages.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_schemas import SUPPORTED_EXTRACTORS, extract_typed_fields  # noqa: E402

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_REF = re.compile(r"\[\[([^\]|#]+)")
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")
_DEFAULT_RELIABILITY = {"A": 1.0, "B": 0.8, "C": 0.6, "D": 0.4, "E": 0.2, "F": 0.3}


def _today() -> date:
    return date.fromisoformat(os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat())


def _schema() -> dict:
    for path in (VAULT / ".okengine" / "composed-schema.yaml", VAULT / "schema.yaml",
                 WIKI / "schema.yaml"):
        if path.is_file():
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
    return {}


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
        matches = _REF.findall(str(raw))
        for ref in matches or [str(raw)]:
            ref = ref.strip().strip("/")
            out.append(ref[:-3] if ref.endswith(".md") else ref)
    return [ref for ref in out if ref]


def _slug(value) -> str:
    refs = _refs(value)
    return refs[0].split("/")[-1].lower() if refs else ""


def _num_map(value, default=None) -> dict[str, float]:
    out = dict(default or {})
    if isinstance(value, dict):
        for key, raw in value.items():
            try:
                out[str(key)] = float(raw)
            except (TypeError, ValueError):
                pass
    return out


def config(schema: dict) -> dict:
    raw = schema.get("event_scoring") if isinstance(schema.get("event_scoring"), dict) else {}
    typed = raw.get("typed_extractors") if isinstance(raw.get("typed_extractors"), dict) else {}
    return {
        "event_types": {str(v) for v in schema.get("event_types", [])},
        "date_field": str(schema.get("event_date_field") or "date"),
        "type_weights": _num_map(schema.get("event_score_weights")),
        "reliability_weights": _num_map(raw.get("reliability_weights"), _DEFAULT_RELIABILITY),
        "source_kind_weights": _num_map(raw.get("source_kind_weights")),
        "tier_weights": _num_map(raw.get("watchlist_tier_weights")),
        "evidence_phrases": [str(v).lower() for v in raw.get("evidence_phrases", [])
                             if str(v).strip()],
        "typed_extractors": {str(k): str(v) for k, v in typed.items()},
        "entity_field": str(raw.get("entity_field") or "entity"),
        "source_field": str(raw.get("source_field") or "source"),
        "reliability_field": str(raw.get("reliability_field") or "reliability"),
        "source_kind_field": str(raw.get("source_kind_field") or "source_kind"),
        "publisher_field": str(raw.get("publisher_field") or "publisher"),
        "tier_field": str(raw.get("watchlist_tier_field") or "competitor_tier"),
        "lookback_days": int(raw.get("lookback_days") or 90),
        "half_life_days": float(raw.get("recency_half_life_days") or 30),
    }


def _source(event_fm: dict, cfg: dict) -> tuple[dict, str]:
    refs = _refs(event_fm.get(cfg["source_field"]) or event_fm.get("sources"))
    if not refs:
        return {}, ""
    path = WIKI / f"{refs[0]}.md"
    return _page(path) if path.is_file() else ({}, "")


def _entity_tier(entity: str, event_fm: dict, cfg: dict) -> str:
    direct = event_fm.get(cfg["tier_field"])
    if direct:
        return str(direct)
    if entity:
        matches = sorted((WIKI / "entities").rglob(f"{entity}.md")) \
            if (WIKI / "entities").is_dir() else []
        if matches:
            entity_fm, _ = _page(matches[0])
            return str(entity_fm.get(cfg["tier_field"]) or "")
    return ""


def collect(cfg: dict) -> list[dict]:
    rows = []
    if not WIKI.is_dir():
        return rows
    for path in WIKI.rglob("*.md"):
        if any(part.startswith((".", "_")) or ".bak." in part for part in path.parts):
            continue
        fm, body = _page(path)
        event_type = str(fm.get("type") or "")
        if event_type not in cfg["event_types"]:
            continue
        source_fm, source_body = _source(fm, cfg)
        entity = _slug(fm.get(cfg["entity_field"]) or fm.get("subject"))
        event_date = None
        for field in (cfg["date_field"], "date", "occurred", "published", "created", "updated"):
            event_date = _date(fm.get(field))
            if event_date:
                break
        reliability = fm.get(cfg["reliability_field"], source_fm.get(cfg["reliability_field"]))
        source_kind = fm.get(cfg["source_kind_field"],
                             source_fm.get(cfg["source_kind_field"], "(unset)"))
        publisher = fm.get(cfg["publisher_field"], source_fm.get(cfg["publisher_field"]))
        title = str(fm.get("title") or fm.get("name") or path.stem)
        rows.append({
            "event_id": path.relative_to(WIKI).with_suffix("").as_posix(),
            "event_type": event_type, "entity": entity,
            "date": event_date.isoformat() if event_date else None,
            "title": title, "body": f"{body[:2000]} {source_body[:2000]}",
            "source": (_refs(fm.get(cfg["source_field"]) or fm.get("sources")) or [None])[0],
            "reliability": str(reliability or "C").upper(),
            "source_kind": str(source_kind or "(unset)"),
            "publisher": str(publisher or "(unset)"),
            "tier": _entity_tier(entity, fm, cfg),
            "on_watchlist": bool(fm.get("on_watchlist")) or bool(_entity_tier(entity, fm, cfg)),
        })
    return sorted(rows, key=lambda row: row["event_id"])


def collect_sources(cfg: dict) -> list[dict]:
    """Collect every canonical source page for source-intrinsic scoring.

    Event-only coverage starved prediction evidence: predictions cite ordinary source pages,
    while the old sidecar contained only sources referenced by event-typed pages. Keep the same
    score-vector contract, but emit one source-scoped row for every source page.
    """
    source_dir = WIKI / "sources"
    if not source_dir.is_dir():
        return []
    rows = []
    for path in source_dir.rglob("*.md"):
        if path.name.startswith(("_", "INDEX")) or ".bak." in path.name:
            continue
        fm, body = _page(path)
        if str(fm.get("type") or "") != "source":
            continue
        source_id = path.relative_to(WIKI).with_suffix("").as_posix()
        observed = None
        for field in ("published", "ingested", "date", "created", "updated"):
            observed = _date(fm.get(field))
            if observed:
                break
        corroboration = fm.get("independent_corroboration_count", 0)
        try:
            corroboration = max(0, int(corroboration))
        except (TypeError, ValueError):
            corroboration = 0
        rows.append({
            "event_id": source_id,
            "event_type": "source-evidence",
            "entity": "",
            "date": observed.isoformat() if observed else None,
            "title": str(fm.get("title") or path.stem),
            "body": body[:4000],
            "source": source_id,
            "reliability": str(fm.get(cfg["reliability_field"]) or "C").upper(),
            "source_kind": str(fm.get(cfg["source_kind_field"]) or "(unset)"),
            "publisher": str(fm.get(cfg["publisher_field"]) or "(unset)"),
            "corroboration_count": corroboration,
        })
    return sorted(rows, key=lambda row: row["source"])


def score_event(event: dict, today: date, cooccurrence: int, cfg: dict) -> dict:
    reliability = cfg["reliability_weights"].get(event["reliability"], 0.5)
    kind = cfg["source_kind_weights"].get(event["source_kind"], 0.5)
    haystack = f"{event['title']} {event['body']}".lower()
    boost = 1.1 if any(phrase in haystack for phrase in cfg["evidence_phrases"]) else 1.0
    credibility = min(1.0, reliability * 0.6 + kind * 0.4 * boost)
    corroboration = max(0, cooccurrence - 1)
    corroboration_signal = min(1.0, 0.5 + 0.1 * corroboration)
    signal = reliability * 0.45 + kind * 0.30 + corroboration_signal * 0.25
    novelty = 1.0 / max(1, cooccurrence)
    materiality = signal * cfg["type_weights"].get(event["event_type"], 1.0) * \
        (0.7 + 0.3 * novelty)
    tier_relevance = cfg["tier_weights"].get(event["tier"], 0.5) \
        if event["on_watchlist"] else 0.0
    event_date = _date(event["date"])
    age = max(0, (today - event_date).days) if event_date else None
    decay = math.pow(0.5, age / cfg["half_life_days"]) if age is not None else 0.0
    scores = {
        "source_reliability_score": round(reliability, 3),
        "claim_credibility_score": round(credibility, 3),
        "signal_strength": round(min(1.0, signal), 3),
        "materiality": round(min(1.0, materiality), 3),
        "novelty": round(novelty, 3),
        "watchlist_relevance": round(tier_relevance, 3),
        "recency_decay": round(decay, 3),
        "corroboration_count": corroboration,
    }
    return {key: event.get(key) for key in
            ("event_id", "event_type", "entity", "date", "source", "publisher")} | \
        {"scores": scores}


def score_source(source: dict, today: date, cfg: dict) -> dict:
    """Produce the event-score-compatible vector used by evidence consumers."""
    reliability = cfg["reliability_weights"].get(source["reliability"], 0.5)
    kind = cfg["source_kind_weights"].get(source["source_kind"], 0.5)
    boost = 1.1 if any(phrase in f"{source['title']} {source['body']}".lower()
                       for phrase in cfg["evidence_phrases"]) else 1.0
    credibility = min(1.0, reliability * 0.6 + kind * 0.4 * boost)
    corroboration = source["corroboration_count"]
    corroboration_signal = min(1.0, 0.5 + 0.1 * corroboration)
    signal = reliability * 0.45 + kind * 0.30 + corroboration_signal * 0.25
    observed = _date(source["date"])
    age = max(0, (today - observed).days) if observed else None
    decay = math.pow(0.5, age / cfg["half_life_days"]) if age is not None else 0.0
    scores = {
        "source_reliability_score": round(reliability, 3),
        "claim_credibility_score": round(credibility, 3),
        "signal_strength": round(min(1.0, signal), 3),
        "materiality": round(min(1.0, signal), 3),
        "novelty": 1.0,
        "watchlist_relevance": 0.0,
        "recency_decay": round(decay, 3),
        "corroboration_count": corroboration,
    }
    return {
        "event_id": source["event_id"],
        "event_type": source["event_type"],
        "entity": "",
        "date": source["date"],
        "source": source["source"],
        "publisher": source["publisher"],
        "score_scope": "source",
        "scores": scores,
        "composite_score": round(scores["materiality"] * scores["recency_decay"], 3),
    }


def score(rows: list[dict], today: date, cfg: dict) -> tuple[list[dict], dict[str, list[dict]]]:
    cutoff = today - timedelta(days=cfg["lookback_days"])
    counts = Counter((row["entity"], row["event_type"]) for row in rows
                     if (not _date(row["date"]) or _date(row["date"]) >= cutoff))
    scored, typed = [], defaultdict(list)
    for event in rows:
        count = max(1, counts[(event["entity"], event["event_type"])])
        row = score_event(event, today, count, cfg)
        extractor = cfg["typed_extractors"].get(event["event_type"])
        if extractor in SUPPORTED_EXTRACTORS:
            fields = extract_typed_fields(extractor, event["title"], event["body"],
                                          [event["entity"]] if event["entity"] else [])
            row["typed_fields"] = fields
            typed[event["event_type"]].append({
                "event_id": event["event_id"], "entity": event["entity"],
                "date": event["date"], "source": event["source"],
                "extractor": extractor, "typed_fields": fields,
            })
        row["composite_score"] = round(
            row["scores"]["materiality"] * row["scores"]["recency_decay"] *
            (1 + row["scores"]["watchlist_relevance"]), 3)
        scored.append(row)
    return scored, typed


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                           for row in rows), encoding="utf-8")
    tmp.replace(path)


def _state_dir() -> Path:
    root = Path(os.environ.get("HERMES_DATA", str(VAULT / ".state")))
    return root / "state" / "okengine.events"


def render(scored: list[dict], today: date, cfg: dict, typed: dict[str, list[dict]],
           source_scored: list[dict]) -> str:
    ranked = sorted(scored, key=lambda row: (-row["composite_score"], row["event_id"]))[:30]
    lines = ["---", "type: dashboard", "title: Event scoring", f"updated: {today.isoformat()}",
             "generator: extensions/okengine.events/event_scoring.py", "---", "",
             "# Event scoring", "", f"**{len(scored)} events scored.** Deterministic eight-vector "
             "substrate; vocabulary and weights are pack-owned.", "",
             "| date | event | type | entity | signal | materiality | novelty | credibility | "
             "recency | relevance | corroboration | composite |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in ranked:
        s = row["scores"]
        entity = f"[[entities/{row['entity']}]]" if row["entity"] else "—"
        lines.append(f"| {row['date'] or '—'} | [[{row['event_id']}]] | {row['event_type']} | "
                     f"{entity} | {s['signal_strength']:.3f} | {s['materiality']:.3f} | "
                     f"{s['novelty']:.3f} | {s['claim_credibility_score']:.3f} | "
                     f"{s['recency_decay']:.3f} | {s['watchlist_relevance']:.3f} | "
                     f"{s['corroboration_count']} | {row['composite_score']:.3f} |")
    lines += ["", "## Typed extraction", ""]
    lines += ([f"- `{event_type}`: **{len(rows)}** ({cfg['typed_extractors'][event_type]})"
               for event_type, rows in sorted(typed.items())] or ["No typed extractors produced rows."])
    lines += ["", "## Source evidence coverage", "",
              f"**{len(source_scored)} canonical sources scored.** These source-intrinsic rows let "
              "prediction evidence and other consumers join any cited source, including sources "
              "not attached to an event-typed page."]
    lines += ["", "_Sidecar: `state/okengine.events/event-scores.jsonl`; typed partitions: "
              "`state/okengine.events/typed-events/*.jsonl`._", ""]
    return "\n".join(lines)


def main() -> int:
    cfg = config(_schema())
    if not WIKI.is_dir():
        print("event-scoring: no vault — nothing to score")
        print(json.dumps({"wakeAgent": False}))
        return 0
    today = _today()
    scored, typed = score(collect(cfg), today, cfg)
    source_scored = [score_source(row, today, cfg) for row in collect_sources(cfg)]
    state = _state_dir()
    _write_jsonl(state / "event-scores.jsonl", scored + source_scored)
    # Write every configured typed partition, including empty ones, so stale rows cannot survive.
    for event_type, extractor in sorted(cfg["typed_extractors"].items()):
        if extractor in SUPPORTED_EXTRACTORS:
            name = _SAFE_NAME.sub("-", event_type).strip("-") or "unknown"
            _write_jsonl(state / "typed-events" / f"{name}.jsonl", typed.get(event_type, []))
    dashboard = WIKI / "dashboards" / "event-scoring.md"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(render(scored, today, cfg, typed, source_scored), encoding="utf-8")
    print(f"event-scoring: scored {len(scored)} event(s) + {len(source_scored)} source(s) "
          f"-> {dashboard.name}")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
