#!/usr/bin/env python3
"""Dependency-aware wake-gate + digest for prediction regrading (#36/#235).

When ``.reevaluation-edges.json`` is present, only source pages changed since the
last scan are joined to the open predictions that cite them.  Edge-less predictions
remain reachable through a deliberately slower legacy-freshness fallback.  An older
deployment without the edge artifact retains the legacy behaviour.  Pure script / no
LLM; state is a derived, atomically-replaced watermark sidecar.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# schema_lib lives one level UP in the staged layout (extension lanes stage into
# /opt/data/scripts/<ext-id>/, shared libs into /opt/data/scripts/) and in scripts/cron in
# the repo layout — append both so direction_enum derives instead of silently falling back.
sys.path.append(os.path.join(_HERE, ".."))
sys.path.append(os.path.join(_HERE, "..", "..", "scripts", "cron"))
import pred_lib as P   # noqa: E402

MAX_PRED = int(os.environ.get("PREDICTION_REGRADE_MAX_PRED", "12"))
MAX_SRC = int(os.environ.get("PREDICTION_REGRADE_MAX_SRC", "25"))
RECENT_DAYS = int(os.environ.get("PREDICTION_REGRADE_RECENT_DAYS", "3"))
FALLBACK_HOURS = int(os.environ.get("PREDICTION_REGRADE_EDGELESS_HOURS", "24"))
SKEPTIC_AFTER_RAISES = int(os.environ.get(
    "PREDICTION_RECOMMENDER_SKEPTIC_AFTER_RAISES",
    os.environ.get("OKENGINE_PREDICTIONS_RECOMMENDER_SKEPTIC_AFTER_RAISES", "2"),
))

# The evidence-record direction vocabulary is DERIVED from the vault's composed schema — the
# single source is the extension's schema fragment (schema/predictions.schema.yaml), which the
# write path ENFORCES (okengine#211/#217). This literal is only the fallback for a vault whose
# composed artifact predates the fragment; the cross-surface contract test (okengine#218) pins
# it to the fragment so the two can never drift. Display order is canonical, not alphabetical.
_DIRECTION_FALLBACK = ("reinforces", "contradicts", "partial", "neutral")


def direction_enum(vault) -> list[str]:
    """The sanctioned `evidence[].direction` values for this vault, from the composed schema
    (falls back to the canonical four). Ordered: canonical values first, any extras sorted."""
    try:
        import schema_lib
        rules = schema_lib.item_rules(schema_lib.merged_schema(vault))
        allowed = (rules.get("evidence") or {}).get("direction", {}).get("enum")
        if allowed:
            return [v for v in _DIRECTION_FALLBACK if v in allowed] + \
                   sorted(set(allowed) - set(_DIRECTION_FALLBACK))
    except Exception:
        pass
    return list(_DIRECTION_FALLBACK)


def _source_key(v: Path, p: Path) -> str:
    rel = p.relative_to(v / "wiki").as_posix()
    return rel[:-3] if rel.endswith(".md") else rel


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _starvation(pair: tuple[Path, dict]) -> tuple:
    """Least-served/oldest-first ordering shared by legacy and dependency batches (#216)."""
    ev = pair[1].get("evidence")
    ev = ev if isinstance(ev, list) else []
    last = max((str(e.get("date") or "") for e in ev if isinstance(e, dict)), default="")
    return (len(ev), last, pair[0].as_posix())


def confidence_recommendations(v: Path) -> dict[str, dict]:
    root = Path(os.environ.get("HERMES_DATA", str(v / ".state")))
    path = root / "state" / "okengine.predictions" / "confidence-recommendations.jsonl"
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and isinstance(row.get("proposition"), str):
            out[row["proposition"]] = row
    return out


def skeptic_fallback_allows_raise(fm: dict) -> bool:
    """Whether an unscored fallback may raise after a run of confidence increases."""
    evidence = fm.get("evidence") if isinstance(fm.get("evidence"), list) else []
    raises = 0
    for item in reversed(evidence):
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction") or "").strip().lower()
        note = str(item.get("note") or "").lower()
        if direction in {"neutral", "contradicts"} or (
                "falsification" in note and ("none found" in note or "searched" in note)):
            return True
        try:
            before, after = float(item.get("confidence_before")), float(item.get("confidence_after"))
        except (TypeError, ValueError):
            break
        if after > before:
            raises += 1
            if raises >= SKEPTIC_AFTER_RAISES:
                return False
        else:
            break
    return True


def dependency_batch(v: Path, open_preds: list[tuple[Path, dict]]) -> tuple[list, bool, str]:
    """Return ``([(prediction, fm, [(date, source, title), ...])], edge_mode, reason)``.

    The watermark is advanced for every completed scan, including an empty scan.  Initial
    scans use the configured recent window, avoiding a one-time replay of the whole corpus.
    """
    wiki = v / "wiki"
    edge_path = wiki / ".reevaluation-edges.json"
    if not edge_path.is_file():
        return [], False, "edge artifact absent"

    artifact = _load_json(edge_path)
    edges = artifact.get("edges")
    if not isinstance(edges, dict):
        return [], False, "edge artifact invalid"

    state_path = wiki / ".prediction-regrade-watermark.json"
    state = _load_json(state_path)
    scan_ns = time.time_ns()
    default_ns = scan_ns - RECENT_DAYS * 86_400 * 1_000_000_000
    try:
        watermark_ns = int(state.get("watermark_ns", default_ns))
    except (TypeError, ValueError):
        watermark_ns = default_ns

    changed: set[str] = set()
    sources: dict[str, tuple[str, Path, str]] = {}
    for p in P.iter_pages(v, "sources"):
        try:
            mtime_ns = p.stat().st_mtime_ns
        except OSError:
            continue
        fm = P.read_fm(p)
        d = P.fm_date(fm, "published", "last_updated", "updated", "created", "date")
        key = _source_key(v, p)
        sources[key] = (d or "unknown-date", p, str(fm.get("title") or p.stem))
        if watermark_ns < mtime_ns <= scan_ns:
            changed.add(key)

    by_page = {p.relative_to(wiki).as_posix()[:-3]: (p, fm) for p, fm in open_preds}
    # Overflow is durable. The watermark may advance over all scanned source mtimes because
    # unemitted proposition/source pairs survive in this sidecar for the next capped run.
    pending_raw = state.get("pending")
    matched: dict[str, set[str]] = {
        page: {source for source in source_keys if source in sources}
        for page, source_keys in (pending_raw.items() if isinstance(pending_raw, dict) else [])
        if page in by_page and isinstance(source_keys, list)
    }
    indexed_pages: set[str] = set()
    for source_key, rows in edges.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("page"), str):
                continue
            page = row["page"]
            indexed_pages.add(page)
            if source_key in changed and page in by_page:
                matched.setdefault(page, set()).add(source_key)

    last_fallback_ns = int(state.get("last_fallback_ns", 0) or 0)
    fallback_due = scan_ns - last_fallback_ns >= FALLBACK_HOURS * 3_600 * 1_000_000_000
    fallback_used = False
    if fallback_due:
        edge_less = [page for page in by_page if page not in indexed_pages]
        if edge_less:
            cutoff = P.days_ago_iso(RECENT_DAYS)
            recent = []
            for source in P.iter_pages(v, "sources"):
                fm = P.read_fm(source)
                d = P.fm_date(fm, "published", "created", "date", "updated")
                if d and d >= cutoff:
                    recent.append((d, source, str(fm.get("title") or source.stem)))
            recent.sort(key=lambda row: row[0], reverse=True)
            recent = recent[:MAX_SRC]
        else:
            recent = []
        if edge_less and recent:
            for page in edge_less:
                matched.setdefault(page, set()).update(
                    _source_key(v, source) for _, source, _ in recent
                )
            fallback_used = True
            last_fallback_ns = scan_ns

    ordered_pages = sorted(matched, key=lambda page: _starvation(by_page[page]))
    emitted_pages = ordered_pages[:MAX_PRED]
    pending = {
        page: sorted(matched[page])
        for page in ordered_pages[MAX_PRED:]
        if matched[page]
    }
    selected_sources: dict[str, list[str]] = {}
    for page in emitted_pages:
        source_keys = sorted(matched[page])
        selected_sources[page] = source_keys[:MAX_SRC]
        if source_keys[MAX_SRC:]:
            # The proposition was emitted, but not all of its changed sources fit. Carry the
            # remainder so a source cap cannot silently consume evidence either.
            pending[page] = source_keys[MAX_SRC:]
    _write_state(state_path, {
        "watermark_ns": scan_ns,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "last_fallback_ns": last_fallback_ns,
        "pending": pending,
    })

    batch = []
    for page in emitted_pages:
        p, fm = by_page[page]
        batch.append((p, fm, [sources[source] for source in selected_sources[page]
                              if source in sources]))
    reason = (f"{len(changed)} changed source(s); {len(pending)} deferred proposition(s); "
              f"fallback={'used' if fallback_used else 'not-used'}")
    return batch, True, reason


def main() -> int:
    v = P.vault()
    cutoff = P.days_ago_iso(RECENT_DAYS)

    open_preds = [(p, fm) for p, fm in P.predictions(v) if P.is_open(fm)]
    # STARVED-FIRST ordering (okengine#216): the old path-sorted [:MAX_PRED] slice fed the same
    # head-of-list every run — measured on cyber-market: evidence-less rises monotonically by
    # sort position (Q1 32 -> Q4 71; 119 pre-June predictions never regraded). Order by fewest
    # evidence entries, then oldest last-entry date, so coverage rotates instead of starving.
    open_preds.sort(key=_starvation)
    recs = confidence_recommendations(v)

    recent_sources = []
    for p in P.iter_pages(v, "sources"):
        fm = P.read_fm(p)
        d = P.fm_date(fm, "published", "last_updated", "updated")
        if d and d >= cutoff:
            recent_sources.append((d, p, str(fm.get("title") or p.stem)))
    recent_sources.sort(key=lambda t: t[0], reverse=True)

    batch, edge_mode, edge_reason = dependency_batch(v, open_preds)
    by_rel = {p.relative_to(v / "wiki").with_suffix("").as_posix(): (p, fm)
              for p, fm in open_preds}
    batched = {p.relative_to(v / "wiki").with_suffix("").as_posix() for p, _, _ in batch}
    for rel in sorted(recs):
        if rel in by_rel and rel not in batched and len(batch) < MAX_PRED:
            p, fm = by_rel[rel]
            batch.append((p, fm, []))

    print("=== prediction-regrade wake-gate ===")
    print(f"  vault: {v}")
    print(f"  open predictions: {len(open_preds)}  ·  sources since {cutoff}: {len(recent_sources)}")
    if edge_mode:
        print(f"  dependency mode: {edge_reason}")

    if edge_mode and not batch:
        print("  → SKIP: no cited source changed since the watermark and no edge-less fallback is due")
        print(json.dumps({"wakeAgent": False}))
        return 0
    if not edge_mode and (not open_preds or (not recent_sources and not recs)):
        print("  → SKIP: need both open predictions and recent sources")
        print(json.dumps({"wakeAgent": False}))
        return 0

    if edge_mode:
        preds = [(p, fm) for p, fm, _ in batch]
        srcs = []
        print(f"  batch: {len(preds)} dependency-matched prediction(s)\n")
    else:
        recommended = [by_rel[rel] for rel in sorted(recs) if rel in by_rel]
        seen = {p for p, _ in recommended}
        preds = (recommended + [(p, fm) for p, fm in open_preds if p not in seen])[:MAX_PRED]
        srcs = recent_sources[:MAX_SRC]
        print(f"  legacy batch: {len(preds)} open prediction(s) vs {len(srcs)} recent source(s)\n")
    print("=== open predictions ===")
    print(
        "For each source below that bears on a claim, update that prediction:\n"
        "  1. append a one-line prose entry to the body `## Evidence log` (append_to_section), and\n"
        "  2. append a STRUCTURED record to the frontmatter `evidence:` list and set top-level\n"
        "     `confidence:` to the new value — send the COMPLETE evidence list via update_entity.\n"
        "Each `evidence:` entry MUST carry these fields (the cockpit reads them to build the\n"
        "trajectory sparkline, the reinforces/contradicts tally, and the confidence-move column;\n"
        "the write path REJECTS any other direction value — resubmit the complete list clean):\n"
        "  - date: <YYYY-MM-DD>\n"
        f"    direction: {' | '.join(direction_enum(v))}\n"
        "    confidence_before: <this prediction's confidence shown below, BEFORE this update>\n"
        "    confidence_after: <your new confidence — also becomes the new top-level confidence:>\n"
        "    source: <the source page path below that drove the update>\n"
        "    note: <one line — what changed>\n"
        "For NEW evidence with no deterministic recommendation, classify it now but leave\n"
        "confidence unchanged (confidence_before == confidence_after); this explicit pending\n"
        "marker lets the recommender score it before the next regrade. When a recommendation\n"
        "is shown, dispose it by applying the suggested confidence_after. If that value is\n"
        "unchanged, add [recommender-accepted] to the note. A same-value deviation instead\n"
        "uses [recommender-deviation: concrete reason]. If scored inputs are unavailable,\n"
        "confidence MUST NOT rise unless the evidence list contains a later neutral/contradicts\n"
        "skeptic pass or an explicit searched-none-found falsification note.\n"
        "No-op is correct when no new source bears. Score direction against the prediction's\n"
        "RESOLUTION CRITERIA, not the topic's importance: topic-relevant-but-outcome-silent =\n"
        "neutral; serious-but-contained = contradicts/partial, never reinforces (#213).\n")
    for i, (p, fm) in enumerate(preds, 1):
        rel = p.relative_to(v).as_posix()
        title = str(fm.get("title") or fm.get("name") or p.stem)
        print(f"## P{i}. {title}  (confidence {fm.get('confidence')})")
        print(f"  page: `{rel}`  ·  subject: {fm.get('subject')}\n")
        rec = recs.get(p.relative_to(v / "wiki").with_suffix("").as_posix())
        if rec:
            print("  deterministic recommendation:")
            print(f"    confidence: {rec.get('confidence_before')} -> "
                  f"{rec.get('confidence_after_suggested')} "
                  f"(delta {float(rec.get('delta_suggested') or 0):+.3f})")
            for event in rec.get("events") or []:
                print(f"    - evidence[{event.get('evidence_index')}], "
                      f"event={event.get('event_id')}, drivers={event.get('update_driver')}")
            print("    Apply it; mark an accepted no-change with [recommender-accepted], or state "
                  "the deviation reason as [recommender-deviation: concrete reason].\n")
        elif not skeptic_fallback_allows_raise(fm):
            print("  fallback confidence action: HOLD — consecutive raises require a neutral/"
                  "contradicts skeptic pass or documented falsification search.\n")
        if edge_mode:
            print("  changed cited source(s):")
            for d, source, source_title in batch[i - 1][2]:
                print(f"    - {d}  `{source.relative_to(v).as_posix()}`  — {source_title}")
            print()
    if not edge_mode:
        print("=== recent sources ===")
        for d, p, title in srcs:
            rel = p.relative_to(v).as_posix()
            print(f"  - {d}  `{rel}`  — {title}")

    print(json.dumps({"wakeAgent": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
