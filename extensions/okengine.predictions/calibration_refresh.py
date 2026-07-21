#!/usr/bin/env python3
"""Forecast calibration + portfolio-watch dashboard for okengine.predictions (#222/#212).

Deterministic (no_agent): one authority for Brier/calibration and operational portfolio health.
The origin system split these across calibration, backtest, and portfolio-watch lanes; OKEngine
keeps them together so their denominators cannot drift:

- Brier score + empirical calibration bands, overall and by horizon / basis signal class;
- open-by-horizon, stale and near-due unresolved predictions;
- recent resolutions and lifetime high-confidence misses;
- evidence-direction bias versus realized outcomes (including an explicit unknown-value count);
- prediction subject coverage plus optional competitive-watchlist gaps;
- idempotent daily JSONL snapshots for Brier and bias trends.

Writes ``wiki/dashboards/calibration.md`` and
``$HERMES_DATA/state/okengine.predictions/calibration-history.jsonl``.

Env: WIKI_PATH · HERMES_DATA · PREDICTION_CONFIDENCE_SCALE · WATCHLIST_PATH
     PREDICTION_NEAR_DUE_PCT (0.8) · PREDICTION_NEAR_DUE_STALE_DAYS (14)
     PREDICTION_STALE_DAYS (60) · PREDICTION_RECENT_RESOLUTION_DAYS (30)
     PREDICTION_HIGH_CONF_MISS (0.7)
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pred_lib as P  # noqa: E402

OUTCOME = {"confirmed": 1.0, "refuted": 0.0, "partial": 0.5}
_DEFAULT_SCALE = {"very-low": 0.1, "low": 0.25, "medium-low": 0.375, "medium": 0.5,
                  "medium-high": 0.625, "high": 0.75, "very-high": 0.9}
_REF = re.compile(r"\[\[([^\]|#]+)")
_POSITIVE = {"reinforces", "reinforce", "supports", "support", "confirms", "confirm", "up"}
_NEGATIVE = {"contradicts", "contradict", "refutes", "refute", "weakens", "weaken", "down"}
_NEUTRAL = {"partial", "mixed", "neutral", "regrade", "note", "context"}

NEAR_DUE_PCT = float(os.environ.get("PREDICTION_NEAR_DUE_PCT", "0.8"))
NEAR_DUE_STALE_DAYS = int(os.environ.get("PREDICTION_NEAR_DUE_STALE_DAYS", "14"))
STALE_DAYS = int(os.environ.get("PREDICTION_STALE_DAYS", "60"))
RECENT_DAYS = int(os.environ.get("PREDICTION_RECENT_RESOLUTION_DAYS", "30"))
HIGH_CONF_MISS = float(os.environ.get("PREDICTION_HIGH_CONF_MISS", "0.7"))
HISTORY_DAYS = int(os.environ.get("PREDICTION_HISTORY_DAYS", "30"))


def _scale() -> dict:
    override = os.environ.get("PREDICTION_CONFIDENCE_SCALE")
    if override:
        try:
            d = json.loads(override)
            if isinstance(d, dict):
                return {str(k).strip().lower(): float(v) for k, v in d.items()}
        except Exception:
            pass
    return _DEFAULT_SCALE


def confidence_prob(v, scale: dict):
    """Stated confidence -> probability in [0,1]. Number (0-1 or 0-100), else label."""
    if v is None:
        return None
    s = str(v).strip().lower()
    try:
        f = float(s.rstrip("%"))
        return max(0.0, min(1.0, f / 100.0 if f > 1.0 else f))
    except ValueError:
        return scale.get(s)


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if v:
        try:
            return date.fromisoformat(str(v)[:10])
        except ValueError:
            pass
    return None


def _refs(v) -> list[str]:
    vals = v if isinstance(v, list) else [v] if v else []
    out = []
    for raw in vals:
        found = _REF.findall(str(raw))
        for ref in found or [str(raw)]:
            ref = ref.strip().strip("/")
            if ref.endswith(".md"):
                ref = ref[:-3]
            if ref:
                out.append(ref)
    return out


def _latest_evidence(fm: dict) -> date | None:
    dates = []
    for item in fm.get("evidence") if isinstance(fm.get("evidence"), list) else []:
        if isinstance(item, dict):
            d = _date(item.get("date") or item.get("on") or item.get("when"))
            if d:
                dates.append(d)
    return max(dates) if dates else None


def collect(v: Path, scale: dict) -> list[dict]:
    rows = []
    for p, fm in P.predictions(v):
        status = str(fm.get("status") or "").strip().lower()
        subjects = sorted(P.subject_slugs(fm))
        evidence = fm.get("evidence") if isinstance(fm.get("evidence"), list) else []
        rows.append({
            "path": p,
            "rel": p.relative_to(v / "wiki").with_suffix("").as_posix(),
            "fm": fm,
            "status": status,
            "outcome": OUTCOME.get(status),
            "confidence": confidence_prob(fm.get("confidence"), scale),
            "confidence_label": fm.get("confidence"),
            "made_on": _date(fm.get("made_on") or fm.get("created")),
            "resolves_by": _date(fm.get("resolves_by") or fm.get("target_date")),
            "updated": _date(fm.get("resolved_at") or fm.get("updated") or fm.get("last_updated")),
            "latest_evidence": _latest_evidence(fm),
            "evidence": evidence,
            "horizon": str(fm.get("horizon") or fm.get("signal_class") or "(unset)").strip().lower(),
            "subjects": subjects,
            "basis": _refs(fm.get("basis") or fm.get("sources") or fm.get("source")),
        })
    return rows


def calibration(rows: list[dict]) -> dict:
    graded = [r for r in rows if r["outcome"] is not None and r["confidence"] is not None]
    if not graded:
        return {"n": 0, "brier": None, "base_rate": None, "bands": []}
    brier = sum((r["confidence"] - r["outcome"]) ** 2 for r in graded) / len(graded)
    base = sum(r["outcome"] for r in graded) / len(graded)
    buckets: dict[int, list[dict]] = defaultdict(list)
    for r in graded:
        buckets[min(9, int(r["confidence"] * 10))].append(r)
    bands = []
    for band in sorted(buckets):
        rs = buckets[band]
        predicted = sum(r["confidence"] for r in rs) / len(rs)
        realized = sum(r["outcome"] for r in rs) / len(rs)
        bands.append({"band": band, "n": len(rs), "predicted": predicted, "realized": realized})
    return {"n": len(graded), "brier": brier, "base_rate": base, "bands": bands}


def calibration_groups(rows: list[dict], key: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["outcome"] is not None and row["confidence"] is not None:
            grouped[str(row.get(key) or "(unset)")].append(row)
    out = []
    for label, rs in sorted(grouped.items()):
        c = calibration(rs)
        out.append({"label": label, "n": c["n"], "brier": c["brier"],
                    "base_rate": c["base_rate"]})
    return out


def _source_classes(v: Path) -> dict[str, str]:
    out = {}
    for p in P.iter_pages(v, "sources"):
        fm = P.read_fm(p)
        sc = fm.get("signal_class")
        if isinstance(sc, str) and sc.strip():
            out[p.stem.lower()] = sc.strip()
    return out


def calibration_by_basis_class(rows: list[dict], source_classes: dict[str, str]) -> list[dict]:
    enriched = []
    for row in rows:
        counts = Counter(source_classes.get(ref.split("/")[-1].lower()) for ref in row["basis"])
        counts.pop(None, None)
        copy = dict(row)
        copy["basis_class"] = counts.most_common(1)[0][0] if counts else "(no classified basis)"
        enriched.append(copy)
    return calibration_groups(enriched, "basis_class")


def direction_bias(rows: list[dict]) -> dict:
    raw = Counter()
    positive = negative = neutral = unknown = 0
    for row in rows:
        for item in row["evidence"]:
            if not isinstance(item, dict):
                continue
            value = str(item.get("direction") or item.get("tag") or "").strip().lower()
            if not value:
                continue
            raw[value] += 1
            if value in _POSITIVE:
                positive += 1
            elif value in _NEGATIVE:
                negative += 1
            elif value in _NEUTRAL:
                neutral += 1
            else:
                unknown += 1
    hits = sum(1 for r in rows if r["status"] == "confirmed")
    misses = sum(1 for r in rows if r["status"] == "refuted")
    return {"positive": positive, "negative": negative, "neutral": neutral, "unknown": unknown,
            "raw": raw, "hits": hits, "misses": misses,
            "evidence_ratio": positive / negative if negative else None,
            "outcome_ratio": hits / misses if misses else None}


def _activity(row: dict) -> date | None:
    return max((d for d in (row["latest_evidence"], row["updated"], row["made_on"]) if d),
               default=None)


def near_due(rows: list[dict], today: date) -> list[dict]:
    out = []
    cutoff = today - timedelta(days=NEAR_DUE_STALE_DAYS)
    for row in rows:
        if row["status"] not in P.OPEN_VALUES or not row["made_on"] or not row["resolves_by"]:
            continue
        window = (row["resolves_by"] - row["made_on"]).days
        if window <= 0:
            continue
        pct = (today - row["made_on"]).days / window
        if pct < NEAR_DUE_PCT or (_activity(row) and _activity(row) >= cutoff):
            continue
        out.append({**row, "pct": pct, "days_left": (row["resolves_by"] - today).days})
    return sorted(out, key=lambda r: (-r["pct"], r["rel"]))


def open_by_horizon(rows: list[dict], today: date) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["status"] in P.OPEN_VALUES:
            grouped[row["horizon"]].append(row)
    stale_cutoff = today - timedelta(days=STALE_DAYS)
    out = []
    for horizon, rs in sorted(grouped.items()):
        confs = [r["confidence"] for r in rs if r["confidence"] is not None]
        stale = sum(1 for r in rs if _activity(r) and _activity(r) < stale_cutoff)
        due_30 = sum(1 for r in rs if r["resolves_by"] and r["resolves_by"] <= today + timedelta(days=30))
        out.append({"horizon": horizon, "n": len(rs),
                    "avg": sum(confs) / len(confs) if confs else None,
                    "due_30": due_30, "stale": stale})
    return out


def recent_resolutions(rows: list[dict], today: date) -> list[dict]:
    cutoff = today - timedelta(days=RECENT_DAYS)
    return sorted([r for r in rows if r["outcome"] is not None and r["updated"] and
                   r["updated"] >= cutoff], key=lambda r: (r["updated"], r["rel"]), reverse=True)


def high_confidence_misses(rows: list[dict]) -> list[dict]:
    return sorted([r for r in rows if r["status"] == "refuted" and
                   r["confidence"] is not None and r["confidence"] >= HIGH_CONF_MISS],
                  key=lambda r: (-r["confidence"], r["rel"]))


def _watchlist(v: Path) -> set[str]:
    configured = os.environ.get("WATCHLIST_PATH", "").strip()
    path = Path(configured) if configured else v / "config" / "competitive-watchlist.yaml"
    if not path.is_file():
        return set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    except Exception:
        return set()
    out = set()
    for segment in (data.get("segments") or {}).values() if isinstance(data, dict) else []:
        if not isinstance(segment, dict):
            continue
        for slug in segment.get("competitors") or []:
            out.add(str(slug).strip().strip("/").split("/")[-1].lower())
    return {s for s in out if s}


def subject_coverage(rows: list[dict], watchlist: set[str]) -> dict:
    counts = Counter(subject for row in rows for subject in row["subjects"])
    return {"top": counts.most_common(10), "watchlist_total": len(watchlist),
            "watchlist_missing": sorted(watchlist - set(counts))}


def _history_path(vault: Path) -> Path:
    # Deployments always provide HERMES_DATA.  Keeping a vault-local fallback makes direct runs and
    # extension fixture tests work without assuming that the caller can create /opt/data.
    return Path(os.environ.get("HERMES_DATA", str(vault / ".state"))) / "state" / \
        "okengine.predictions" / "calibration-history.jsonl"


def update_history(path: Path, snapshot: dict) -> list[dict]:
    """Idempotent one-row-per-day JSONL history, atomically replaced."""
    by_date = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("date"):
                    by_date[str(row["date"])] = row
            except json.JSONDecodeError:
                continue
    by_date[snapshot["date"]] = snapshot
    rows = [by_date[d] for d in sorted(by_date)][-HISTORY_DAYS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)
    return rows


def _ratio(value) -> str:
    return f"{value:.2f}:1" if value is not None else "n/a"


def render(rows: list[dict], today: date, history: list[dict], source_classes: dict[str, str],
           watchlist: set[str]) -> str:
    cal = calibration(rows)
    bias = direction_bias(rows)
    near = near_due(rows, today)
    recent = recent_resolutions(rows, today)
    misses = high_confidence_misses(rows)
    coverage = subject_coverage(rows, watchlist)
    statuses = Counter(r["status"] or "(missing)" for r in rows)
    horizons = open_by_horizon(rows, today)
    by_horizon = calibration_groups(rows, "horizon")
    by_basis = calibration_by_basis_class(rows, source_classes)
    # A measurement for a fixed effective date must render byte-for-byte identically on rerun.
    now = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    lines = ["---", "type: dashboard", 'title: "Forecast calibration and portfolio watch"',
             f"updated: {now}", "---", "", f"# Forecast calibration and portfolio watch — {today}", "",
             "_Deterministic no-agent measurement. Lower Brier is better; unknown evidence values "
             "are surfaced, never silently bucketed._", "", "## Portfolio summary", "",
             f"- total predictions: **{len(rows)}**",
             "- statuses: " + ", ".join(f"{k}={n}" for k, n in statuses.most_common()), ""]
    lines += ["### Open predictions by horizon", "",
              "| horizon | open | avg confidence | resolving ≤30d | stale ≥60d |",
              "|---|---:|---:|---:|---:|"]
    for h in horizons:
        avg = f"{h['avg']:.3f}" if h["avg"] is not None else "—"
        lines.append(f"| {h['horizon']} | {h['n']} | {avg} | {h['due_30']} | {h['stale']} |")

    lines += ["", "## Calibration", ""]
    if not cal["n"]:
        lines += ["No resolved, scored predictions yet.", ""]
    else:
        caveat = " — **small N**" if cal["n"] < 10 else ""
        lines += [f"- resolved & scored: **{cal['n']}**{caveat}",
                  f"- Brier: **{cal['brier']:.4f}**",
                  f"- realized hit fraction: **{cal['base_rate']:.3f}**", "",
                  "### Calibration by confidence band", "",
                  "| predicted probability | N | realized hit rate | gap |",
                  "|---:|---:|---:|---:|"]
        for band in cal["bands"]:
            lines.append(f"| {band['predicted']:.3f} | {band['n']} | {band['realized']:.3f} | "
                         f"{band['realized'] - band['predicted']:+.3f} |")
    for title, groups in (("Calibration by horizon", by_horizon),
                          ("Calibration by dominant basis signal class", by_basis)):
        lines += ["", f"### {title}", "", "| class | N | Brier | hit fraction |",
                  "|---|---:|---:|---:|"]
        for group in groups:
            lines.append(f"| {group['label']} | {group['n']} | {group['brier']:.4f} | "
                         f"{group['base_rate']:.3f} |")

    valid_history = [r for r in history if r.get("brier") is not None]
    lines += ["", "### Brier trend", ""]
    if len(valid_history) < 2:
        lines.append("_Needs at least two daily snapshots._")
    else:
        lines += ["| date | resolved | Brier |", "|---|---:|---:|"]
        for snap in valid_history[-14:]:
            lines.append(f"| {snap['date']} | {snap['resolved']} | {snap['brier']:.4f} |")
        delta = valid_history[-1]["brier"] - valid_history[0]["brier"]
        lines += ["", f"Window delta: **{delta:+.4f}** "
                  f"({'worse' if delta > .001 else 'better' if delta < -.001 else 'flat'})."]

    lines += ["", "## Evidence-direction bias (#212)", "",
              f"- evidence: **{bias['positive']} positive / {bias['negative']} negative** "
              f"({_ratio(bias['evidence_ratio'])})",
              f"- realized outcomes: **{bias['hits']} confirmed / {bias['misses']} refuted** "
              f"({_ratio(bias['outcome_ratio'])})",
              f"- neutral/partial: **{bias['neutral']}**; unknown/unmapped: **{bias['unknown']}**", ""]
    if bias["unknown"]:
        unknowns = [(k, n) for k, n in bias["raw"].most_common()
                    if k not in _POSITIVE | _NEGATIVE | _NEUTRAL]
        lines.append("Unknown values: " + ", ".join(f"`{k}`×{n}" for k, n in unknowns) + ".")

    lines += ["", f"## Near-due unresolved ({len(near)})", "",
              f"_Open, ≥{NEAR_DUE_PCT:.0%} through the window, with no activity in "
              f"{NEAR_DUE_STALE_DAYS} days._", ""]
    lines += ([f"- [[{r['rel']}]] — {r['pct']:.0%} through, {r['days_left']}d remaining, "
               f"confidence={r['confidence_label']}" for r in near] or ["None."])

    lines += ["", f"## Recent resolutions ({len(recent)} in {RECENT_DAYS}d)", ""]
    lines += ([f"- [[{r['rel']}]] — **{r['status']}**, confidence={r['confidence_label']}, "
               f"updated={r['updated']}" for r in recent] or ["None."])

    lines += ["", f"## High-confidence misses ({len(misses)})", "",
              f"_Refuted at confidence ≥{HIGH_CONF_MISS:.2f}; lifetime calibration red flags._", ""]
    lines += ([f"- [[{r['rel']}]] — confidence={r['confidence']:.3f}" for r in misses] or ["None."])

    lines += ["", "## Subject coverage", "", "| subject | predictions |", "|---|---:|"]
    lines += [f"| [[entities/{slug}]] | {n} |" for slug, n in coverage["top"]]
    if coverage["watchlist_total"]:
        missing = coverage["watchlist_missing"]
        lines += ["", f"Watchlist gaps: **{len(missing)} / {coverage['watchlist_total']}** tracked "
                  "entities have no prediction.", ""]
        lines += [f"- [[entities/{slug}]]" for slug in missing[:30]] or ["None."]
    else:
        lines += ["", "_No optional competitive watchlist configured; gap coverage is not applicable._"]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    v = P.vault()
    today = date.fromisoformat(P.today_iso())
    rows = collect(v, _scale())
    cal = calibration(rows)
    bias = direction_bias(rows)
    snapshot = {"date": today.isoformat(), "predictions": len(rows),
                "open": sum(1 for r in rows if r["status"] in P.OPEN_VALUES),
                "resolved": cal["n"], "brier": cal["brier"],
                "evidence_positive": bias["positive"], "evidence_negative": bias["negative"],
                "confirmed": bias["hits"], "refuted": bias["misses"]}
    history = update_history(_history_path(v), snapshot)
    dash = v / "wiki" / "dashboards" / "calibration.md"
    dash.parent.mkdir(parents=True, exist_ok=True)
    dash.write_text(render(rows, today, history, _source_classes(v), _watchlist(v)), encoding="utf-8")
    print(f"calibration-refresh: {len(rows)} prediction(s), {cal['n']} resolved+scored -> "
          "wiki/dashboards/calibration.md")
    print(json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
