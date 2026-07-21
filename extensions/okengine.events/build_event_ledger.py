#!/usr/bin/env python3
"""okengine.events — build the domain event ledger (okengine#155). Deterministic, no LLM.

Compiles the pack's dated event-typed pages into a scored, newest-first ledger dashboard. The
mechanism is sector-agnostic; the domain coupling is PACK config, read from the governing schema:

  event_types:         [<type>, ...]   page types that count as events (REQUIRED to do anything)
  event_date_field:    <field>          frontmatter field holding the event date (default: date)
  event_score_weights: {<type>: <n>}    per-type weight (default 1.0)

A pack that declares no `event_types` is a clean no-op (generic — not every vault models events).

Self-contained (stdlib + yaml only; runs from its own staged dir). Writes
`wiki/dashboards/event-ledger.md` + appends a one-row/day count to
`wiki/operational/event-ledger-snapshots.md`. Script-only (wakeAgent=false).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

VAULT = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
WIKI = VAULT / "wiki"
DASH = WIKI / "dashboards" / "event-ledger.md"
SNAP = WIKI / "operational" / "event-ledger-snapshots.md"
MAX_EVENTS = int(os.environ.get(
    "EVENT_LEDGER_MAX", os.environ.get("OKENGINE_EVENTS_MAX_EVENTS", "500")
))

_FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
_DATE_FULL = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_YM = re.compile(r"(\d{4}-\d{2})(?!-?\d)")     # year-month (e.g. campaign first_seen: 2025-10)
_DATE_Y = re.compile(r"\b(\d{4})\b")                 # bare year


def _norm_date(v) -> str | None:
    """Normalize a frontmatter date value to YYYY-MM-DD: a full date as-is, a year-month padded
    to -01, a bare year to -01-01. Partial dates are common (campaign first_seen: 2025-10)."""
    s = str(v)
    m = _DATE_FULL.search(s)
    if m:
        return m.group(1)
    m = _DATE_YM.search(s)
    if m:
        return m.group(1) + "-01"
    m = _DATE_Y.search(s)
    if m:
        return m.group(1) + "-01-01"
    return None


def _schema() -> dict:
    """Governing schema: prefer the composed artifact, else the raw pack schema. Either carries
    the pack's event_* inputs (they pass through composition unchanged)."""
    for p in (VAULT / ".okengine" / "composed-schema.yaml", VAULT / "schema.yaml",
              WIKI / "schema.yaml"):
        if p.is_file():
            try:
                d = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    return {}


def _fm(md: Path) -> dict:
    try:
        m = _FM.match(md.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return {}
    if not m:
        return {}
    try:
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _event_date(fm: dict, field: str) -> str | None:
    for key in (field, "date", "published", "occurred", "first_seen", "created", "updated"):
        v = fm.get(key)
        if v:
            d = _norm_date(v)
            if d:
                return d
    return None


def main() -> int:
    schema = _schema()
    types = schema.get("event_types")
    types = {str(t) for t in types} if isinstance(types, list) else set()
    if not types or not WIKI.is_dir():
        print("event-ledger: no event_types declared (or no vault) — nothing to compile")
        print(json.dumps({"wakeAgent": False}))
        return 0

    date_field = str(schema.get("event_date_field") or "date")
    weights = schema.get("event_score_weights") or {}
    weights = {str(k): float(v) for k, v in weights.items()} if isinstance(weights, dict) else {}

    events = []
    for md in WIKI.rglob("*.md"):
        if any(part.startswith((".", "_")) or ".bak." in part for part in md.parts):
            continue
        fm = _fm(md)
        t = str(fm.get("type", ""))
        if t not in types:
            continue
        rel = md.relative_to(WIKI).with_suffix("").as_posix()
        events.append({
            "date": _event_date(fm, date_field) or "",
            "type": t,
            "title": str(fm.get("title") or fm.get("name") or md.stem),
            "score": weights.get(t, 1.0),
            "ref": rel,
        })
    # newest first; undated sink to the bottom
    events.sort(key=lambda e: (e["date"] or "0000-00-00", e["title"]), reverse=True)
    shown = events[:MAX_EVENTS]

    L = ["---", "type: dashboard", "title: Event ledger", f"updated: {_now()}",
         "generator: extensions/okengine.events/build_event_ledger.py", "---", "",
         "# Event ledger", "",
         f"_Dated domain events compiled from {len(types)} event type(s): "
         f"{', '.join(sorted(types))}. Score = per-type weight. Deterministic; no LLM._", "",
         f"**{len(events)} events**" + (f" (showing newest {len(shown)})" if len(shown) < len(events) else ""),
         "", "| date | type | score | event |", "|---|---|--:|---|"]
    for e in shown:
        L.append(f"| {e['date'] or '—'} | {e['type']} | {e['score']:g} | [[{e['ref']}]] |")
    L.append("")
    DASH.parent.mkdir(parents=True, exist_ok=True)
    DASH.write_text("\n".join(L) + "\n", encoding="utf-8")

    # one-row/day snapshot so the ledger size trends
    try:
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        header = ("---\ntype: dashboard\ntitle: Event-ledger snapshots\n---\n\n"
                  "# Event-ledger snapshots\n\n| date | events |\n|---|---|\n")
        if not SNAP.exists():
            SNAP.write_text(header, encoding="utf-8")
        row = f"| {_today()} | {len(events)} |"
        lines = [ln for ln in SNAP.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith(f"| {_today()} |")]
        SNAP.write_text("\n".join(lines).rstrip() + "\n" + row + "\n", encoding="utf-8")
    except OSError:
        pass

    print(f"event-ledger: compiled {len(events)} event(s) across {len(types)} type(s) -> {DASH.name}")
    print(json.dumps({"wakeAgent": False}))
    return 0


def _today() -> str:
    return os.environ.get("OKENGINE_MCP_WRITE_DATE") or date.today().isoformat()


def _now() -> str:
    """ISO-8601 UTC timestamp for the dashboard `updated:` stamp."""
    return os.environ.get("OKENGINE_MCP_WRITE_NOW") or os.environ.get("OKENGINE_MCP_WRITE_DATE") \
        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
