from __future__ import annotations
# ruff: noqa: F821

import functools
import os
import re
import json
import glob
import hashlib
import hmac
import sys
import threading
import time
import datetime
from collections import Counter, defaultdict
import subprocess
import shutil
import tempfile
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import Any

import yaml
import markdown as md
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

_FM_SCAN_BYTES = 262_144


def _drill_text(value, *, limit: int = 3) -> str:
    """Compact, JSON-safe display text for a drill fact; never stringify dict payloads."""
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        parts = [_drill_text(v) for v in value[:limit]]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _drill_assessment_fact(row: dict, spec: dict) -> str:
    record = _assessment_for_row(row, spec)
    if not record:
        terminal = _assessment_terminal_for_row(row, spec)
        return _assessment_terminal_label(str((terminal or {}).get("state") or "review-not-run"))
    state = str(record.get("epistemic_status") or "assessed").replace("-", " ")
    raw = record.get(str(spec.get("value_field") or "assessed_value"))
    labels = spec.get("labels") if isinstance(spec.get("labels"), dict) else {}
    value = labels.get(raw, labels.get(str(raw), raw)) if raw not in (None, "", []) else state
    confidence = record.get("confidence")
    if isinstance(confidence, (int, float)):
        return f"{value} · {state} · {round(float(confidence) * 100):d}% confidence"
    band = _drill_text(record.get("confidence_band"))
    return f"{value} · {state}" + (f" · {band} confidence" if band else "")


def _drill_column_fact(row: dict, col: dict) -> str:
    """Plain-text counterpart to _ds_cell for contextual result cards."""
    if isinstance(col.get("assessment"), dict):
        return _drill_assessment_fact(row, col["assessment"])
    field = str(col.get("field") or "")
    raw = _source_publisher(row) if col.get("source_publisher") else row.get(field)
    labels = col.get("labels") or col.get("value_labels") or {}
    labels = labels if isinstance(labels, dict) else {}
    if isinstance(raw, list):
        value = ", ".join(
            _drill_text(labels.get(v, labels.get(str(v), v)))
            for v in raw[: int(col.get("max") or 3)]
        )
    else:
        value = _drill_text(labels.get(raw, labels.get(str(raw), raw)))
    if not value:
        return ""
    if col.get("pct"):
        try:
            pct = float(raw) * 100
            value = f"{pct:.1f}%" if 0 < pct < 10 else f"{pct:.0f}%"
        except (TypeError, ValueError):
            pass
    if col.get("date"):
        parsed = _as_date(raw)
        value = parsed.isoformat() if parsed else ""
    if col.get("defang"):
        value = _defang(value)
    return value


def _row_page(r: dict, box: dict | None = None) -> dict:
    """Context-rich dataset result shared by every cockpit list/drill renderer.

    Configured table columns are the primary contract: they explain why a record belongs on that
    panel. Aggregate drills without columns receive a small set of common, non-empty facts. The
    original path/title/type keys remain stable for older clients.
    """
    rel = r.get("_rel") or r.get("_name") or ""
    page = {
        "path": f"{r.get('_sub', '')}/{rel}".strip("/"),
        "title": _disp(r),
        "type": str(r.get("type") or ""),
    }
    for field in _DRILL_SUMMARY_FIELDS:
        summary = _drill_text(r.get(field))
        if summary and summary.casefold() != page["title"].casefold():
            page["summary"] = summary[:320] + ("…" if len(summary) > 320 else "")
            break

    facts, used = [], set()
    cols = [col for col in ((box or {}).get("columns") or []) if isinstance(col, dict)]
    for col in cols:
        field = str(col.get("field") or "")
        # A title/link column repeats the card heading rather than adding context.
        if col.get("link") and field in {"", "title", "name", "cve_id"}:
            continue
        value = _drill_column_fact(r, col)
        if not value or value.casefold() == page["title"].casefold():
            continue
        label = str(col.get("label") or field or "Detail")
        facts.append({"label": label, "value": value})
        if field:
            used.add(field)
    if not facts:
        for field, label, is_date in _DRILL_FALLBACK_FIELDS:
            if field in used or r.get(field) in (None, "", []):
                continue
            value = _drill_text(r.get(field))
            if is_date:
                parsed = _as_date(r.get(field))
                value = parsed.isoformat() if parsed else ""
            if value:
                facts.append({"label": label, "value": value})
            if len(facts) >= 5:
                break
    if facts:
        page["facts"] = facts[:8]
    return page


def api_drill(tab: str, box: int, value: str = Query(default=""), item: int = Query(default=-1)):
    """The pages behind one aggregate value — a bars/chips group_by bucket, or a bignums item
    (count / filtered-count / top-of-group). The dataset + filter are re-derived from the SAME
    tab config the widget rendered from; the client only names the box + the bucket, never a raw
    query (okengine#189). Returns browse-shaped pages so the UI reuses its list renderer."""
    cfg = cockpit_config()
    d = (cfg.get("tab_defs") or {}).get(tab)
    if not d or not (0 <= box < len(d.get("boxes") or [])):
        raise HTTPException(404, "no such box")
    b = d["boxes"][box]
    view = str(b.get("view") or "table")
    heading = str(b.get("title") or tab)
    if view == "bignums":
        items = b.get("items") or []
        if not (0 <= item < len(items)) or not isinstance(items[item], dict):
            raise HTTPException(404, "no such item")
        it = items[item]
        rows = _configured_rows(it) if it.get("dataset") else _configured_rows(b)
        if not it.get("dataset"):
            rows = _refine_rows(rows, it)
        heading = str(it.get("label") or heading)
        if it.get("stat") == "top" and it.get("group_by"):  # drill the WINNING bucket
            cnt = Counter(s for r in rows for s in _gb_values(r.get(it["group_by"])))
            top = cnt.most_common(1)[0][0] if cnt else None
            rows = [r for r in rows if top in _gb_values(r.get(it["group_by"]))] if top else []
            heading = f"{heading}: {top}" if top else heading
    elif view in ("bars", "chips") and isinstance(b.get("assessment"), dict):
        spec = b["assessment"]
        source_rows = [row for row in _configured_rows(b) if _current_assessment_subject(row)]
        matches = [
            (row, record)
            for row in source_rows
            for key, record in [_assessment_bucket(row, spec)]
            if key == value
        ]
        labels = {str(k): str(v) for k, v in (spec.get("labels") or b.get("labels") or {}).items()}
        special = {
            "__not_assessed__": "Not assessed",
            "__review_not_run__": "Review not run",
            "__assessed__": "Assessment reference stale",
            "__no_association_established__": "No association established",
            "__collection_required__": "Collection required",
            "__failed__": "Review failed",
            "__disputed__": "Disputed",
            "__inconclusive__": "Inconclusive",
            "__unknown__": "Unknown",
            "__metadata_unavailable__": "Assessment metadata unavailable",
        }
        heading = f"{heading}: {special.get(value, labels.get(value, value))}"
        pages = []
        primary_pages = []
        assessment_pages = []
        matches.sort(key=lambda pair: _disp(pair[0]).casefold())
        for row, record in matches[:_DRILL_CAP]:
            primary = _row_page(row, b)
            primary_pages.append(primary)
            pages.append(primary)
            if record:
                supporting = {
                    "path": record["path"],
                    "title": record["title"],
                    "type": "assessment",
                    "summary": _drill_text(record.get("claim")),
                }
                assessment_facts = []
                for field, label in (
                    ("status", "Status"),
                    ("epistemic_status", "Evidence state"),
                    ("confidence_band", "Confidence"),
                    ("as_of", "As of"),
                ):
                    fact = _drill_text(record.get(field))
                    if fact:
                        assessment_facts.append({"label": label, "value": fact})
                # A record reaches here only if its `status` is in the spec's status set, and
                # any non-empty status renders as a fact, so the list is never empty in practice.
                # The guard stays because `facts: []` would render as an empty fact strip if that
                # filter ever loosens.
                if assessment_facts:  # pragma: no branch
                    supporting["facts"] = assessment_facts
                assessment_pages.append(supporting)
                pages.append(supporting)
        primary_type = str(
            (b.get("dataset") or {}).get("type")
            or (primary_pages[0].get("type") if primary_pages else "page")
        )
        primary_title = _humanize(primary_type)
        primary_plural = (
            primary_title if primary_title.casefold().endswith("s") else primary_title + "s"
        )
        result = {
            "title": heading,
            "count": len(matches),
            "count_label": f"{len(matches)} {primary_title.casefold() if len(matches) == 1 else primary_plural.casefold()}",
            "truncated": len(matches) > _DRILL_CAP,
            "pages": pages,
        }
        if assessment_pages:
            result["sections"] = [
                {"title": primary_plural, "count": len(primary_pages), "pages": primary_pages},
                {
                    "title": "Supporting assessments",
                    "count": len(assessment_pages),
                    "pages": assessment_pages,
                },
            ]
        return result
    elif view in ("bars", "chips") and b.get("group_by"):
        gb = b["group_by"]
        lblmap = {str(k): str(v) for k, v in (b.get("labels") or {}).items()}
        if value == _UNMAPPED_KEY:  # the collapsed "unmapped (N)" bucket row
            drop = {"", "None", "Unknown", "nan"}
            rows = [
                r
                for r in _configured_rows(b)
                if any(s not in lblmap and s not in drop for s in _gb_values(r.get(gb)))
            ]
            heading = f"{heading}: unmapped values"
        else:
            aliases = {str(k): str(v) for k, v in (b.get("aliases") or {}).items()}
            rows = [
                r
                for r in _configured_rows(b)
                if value in [aliases.get(v, v) for v in _gb_values(r.get(gb))]
            ]
            heading = f"{heading}: {lblmap.get(value, value)}"
    elif view == "coverage":
        versus = b.get("versus") or {}
        group_by = str(versus.get("group_by") or "")
        if not group_by:
            raise HTTPException(400, "coverage box has no versus group")
        rows = [r for r in _ds_rows(versus) if value in _gb_values(r.get(group_by))]
        heading = f"{heading}: {value}"
    elif view in ("table", "cards"):
        # The FULL row set behind a truncated widget, in the same order the widget cut it (the
        # box's own `sort`) — so the first row past the fold is the row the table would have shown
        # next. A table box advertises its own denominator in the meta line ("showing 8 of 83");
        # until now that total was unreachable — 17 boxes on one live vault named a number the UI
        # gave no way to open. Unlike a group_by drill there is no bucket to filter on, so `value`
        # is ignored here rather than required.
        rows = _ds_sorted(_configured_rows(b), b.get("sort") or {})
        return {
            "title": heading,
            "count": len(rows),
            "truncated": len(rows) > _DRILL_CAP,
            "pages": [_row_page(r, b) for r in rows[:_DRILL_CAP]],
        }
    else:
        raise HTTPException(400, "box is not drillable")
    rows = _ds_sorted(rows, {"field": "title"})[:_DRILL_CAP]
    return {"title": heading, "count": len(rows), "pages": [_row_page(r, b) for r in rows]}


def api_dashboards():
    groups = cockpit_config()["dashboards"]
    if groups:

        def _dmeta(path):
            p = WIKI / (path + ".md")
            try:
                fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return {}
            return fm if isinstance(fm, dict) else {}

        out, seen = [], set()
        for g in groups:
            if not isinstance(g, dict):
                continue
            items = []
            for it in g.get("items") or []:
                if not (isinstance(it, dict) and it.get("path")):
                    continue
                path = str(it["path"]).strip().strip("/")
                fm = _dmeta(path)
                items.append(
                    {
                        "path": path,
                        "title": str(
                            it.get("title") or fm.get("title") or path.rsplit("/", 1)[-1]
                        ).strip(),
                        "desc": str(
                            it.get("desc") or fm.get("summary") or fm.get("description") or ""
                        ).strip(),
                    }
                )
                seen.add(path)
            out.append({"group": str(g.get("group") or "").strip(), "items": items})
        # nothing hides: any dashboard not placed in a configured group lands in "Other"
        base = WIKI / "dashboards"
        extra = []
        if base.is_dir():
            # RECURSIVE, matching the default branch below: extensions write nested dashboards
            # (dashboards/<ns>/*.md); a flat *.md glob left an un-curated nested dashboard out of the
            # "Other" catch-all entirely, so it was invisible in the grid (invariant-audit M7).
            for p in sorted(base.rglob("*.md")):
                if not _visible_page(
                    p, base
                ):  # segment-level: drops dashboards/_archive/… (batch-2 re-verify)
                    continue
                rel = p.relative_to(base).with_suffix("").as_posix()
                path = f"dashboards/{rel}"
                if path in seen:
                    continue
                fm = _dmeta(path)
                extra.append(
                    {
                        "path": path,
                        "title": str(fm.get("title") or p.stem).strip(),
                        "desc": str(fm.get("summary") or fm.get("description") or "").strip(),
                    }
                )
        if extra:
            out.append({"group": "Other", "items": extra})
        return {"groups": out}
    # default: auto-list every page under wiki/dashboards/
    base = WIKI / "dashboards"
    items = []
    if base.is_dir():
        # RECURSIVE: extensions write nested dashboards (dashboards/<ns>/*.md, e.g. competitive/);
        # a flat *.md glob left them invisible in the grid unless a pack curated them explicitly.
        for p in sorted(base.rglob("*.md")):
            if not _visible_page(
                p, base
            ):  # segment-level: drops dashboards/_archive/… (batch-2 re-verify)
                continue
            try:
                fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            rel = p.relative_to(base).as_posix()[:-3]  # keep the sub-namespace in the path
            items.append(
                {
                    "path": f"dashboards/{rel}",
                    "title": str(fm.get("title") or p.stem).strip(),
                    "desc": str(fm.get("summary") or fm.get("description") or "").strip(),
                }
            )
    return {"groups": [{"group": "Dashboards", "items": items}] if items else []}


def _ops_meta(path: str) -> dict:
    p = WIKI / (path + ".md")
    try:
        fm, _ = split_fm(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}
    return fm if isinstance(fm, dict) else {}


def _ops_item(path: str) -> dict:
    fm = _ops_meta(path)
    updated = str(fm.get("updated") or fm.get("generated_at") or fm.get("date") or "")[:10] or None
    if not updated:
        md = _DATED_SERIES_RE.match(path.rsplit("/", 1)[-1])
        updated = md.group(2) if md else None
    parsed = _as_date(updated)
    age = (datetime.datetime.now(datetime.timezone.utc).date() - parsed).days if parsed else None
    stale_days = max(1, int(os.environ.get("OKENGINE_OPS_STALE_DAYS", "2")))
    freshness = "unknown" if age is None or age < 0 else "stale" if age > stale_days else "current"
    return {
        "path": path,
        "title": str(fm.get("title") or path.rsplit("/", 1)[-1]).strip(),
        "desc": str(fm.get("summary") or fm.get("description") or "").strip(),
        "updated": updated,
        "age_days": age,
        "freshness": freshness,
    }


def _sort_ops_items(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda it: (
            _OPS_PRIORITY.get(str(it.get("freshness")), 1),
            str(it.get("title") or "").lower(),
        ),
    )


def _ops_groups() -> list[dict]:
    """The operational/health page groups surfaced in the Ops tab. Only pages that exist are
    included; empty groups are dropped. Present on any OKF vault the engine crons have run."""
    out, seen = [], set()
    application = load_application_declaration(VAULT)
    if application:
        out.append(
            {
                "group": "Applications",
                "items": [
                    {
                        "action": "application",
                        "title": _humanize(application["profile"]),
                        "desc": "Application contract, lifecycle bindings, queues, surfaces, and measures",
                        "updated": None,
                    }
                ],
            }
        )
    for label, paths in _OPS_GROUPS:
        items = []
        for path in paths:
            if path in seen or not (WIKI / (path + ".md")).is_file():
                continue
            seen.add(path)
            item = _ops_item(path)
            if path == "_review-queue":
                item.update(
                    {
                        "action": "reviews",
                        "title": "Human review",
                        "desc": "Complete, prioritized review worklist and evidence decisions",
                    }
                )
            items.append(item)
        if items:
            out.append({"group": label, "items": _sort_ops_items(items)})
    # nothing hides: anything else under operational/ (new artifacts, per-day snapshots). A daily
    # series (`<series>-YYYY-MM-DD.md`) is collapsed to its NEWEST page so the log doesn't drown in
    # a page-per-day; the rolled-up `-snapshots` variants are already pinned in the groups above.
    base = WIKI / "operational"
    latest: dict[
        str, tuple[str, str]
    ] = {}  # series-prefix -> (date, stem); "" key = non-dated (kept as-is)
    if base.is_dir():
        for p in sorted(base.glob("*.md")):  # glob-ok: operational/ is a flat (unsharded) namespace
            if p.name.startswith(("_", ".")) or p.name == "INDEX.md":
                continue
            path = f"operational/{p.stem}"
            if path in seen:
                continue
            seen.add(path)
            md = _DATED_SERIES_RE.match(p.stem)
            if md:
                series, date = md.group(1), md.group(2)
                # ``glob`` is sorted by the ISO-dated stem, so later entries for a series are newer.
                latest[series] = (date, p.stem)
            else:
                latest[p.stem] = ("", p.stem)  # non-dated: unique key, always kept
    extra = [
        _ops_item(f"operational/{stem}") for _, stem in sorted(latest.values(), key=lambda v: v[1])
    ]
    if extra:
        out.append({"group": "Operational log", "items": _sort_ops_items(extra)})
    # Exception-bearing groups lead; the curated group order remains the tie-breaker.
    return sorted(
        out,
        key=lambda g: min(
            (_OPS_PRIORITY.get(i.get("freshness"), 1) for i in g["items"]), default=3
        ),
    )


def _ops_available() -> bool:
    return bool(_ops_groups())


def _ops_summary() -> dict:
    """Small, evidence-linked exception summary derived from existing engine artifacts."""
    metrics = []
    overall = "ok"
    fleet = WIKI / "dashboards" / "fleet-health.md"
    if fleet.is_file():
        text = fleet.read_text(encoding="utf-8", errors="replace")
        counts = {
            k: int(v)
            for k, v in re.findall(
                r"(ok|critical-stale|stale|errored|off-model|never-run):\s*(\d+)", text
            )
        }
        for key, tone in (
            ("errored", "crit"),
            ("off-model", "crit"),
            ("critical-stale", "warn"),
            ("stale", "warn"),
            ("never-run", "warn"),
            ("ok", "ok"),
        ):
            if key in counts:
                display_tone = tone if counts[key] else "ok"
                metrics.append(
                    {
                        "label": key,
                        "value": counts[key],
                        "tone": display_tone,
                        "path": "dashboards/fleet-health",
                    }
                )
        if any(counts.get(k, 0) for k in ("errored", "off-model")):
            overall = "critical"
        elif any(counts.get(k, 0) for k in ("critical-stale", "stale", "never-run")):
            overall = "warning"
    else:
        overall = "unknown"
        metrics.append({"label": "fleet status", "value": "unknown", "tone": "mut", "path": None})

    validation = WIKI / "operational" / "deployment-validation.md"
    if validation.is_file():
        text = validation.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\*\*(PASS|FAIL)\*\*\s*[—-]\s*(\d+)\s+fail\s*·\s*(\d+)\s+warn", text)
        if m:
            fail_n, warn_n = int(m.group(2)), int(m.group(3))
            metrics.append(
                {
                    "label": "validation failures",
                    "value": fail_n,
                    "tone": "crit" if fail_n else "ok",
                    "path": "operational/deployment-validation",
                }
            )
            metrics.append(
                {
                    "label": "validation warnings",
                    "value": warn_n,
                    "tone": "warn" if warn_n else "ok",
                    "path": "operational/deployment-validation",
                }
            )
            if fail_n:
                overall = "critical"
            elif warn_n and overall == "ok":
                overall = "warning"
    else:
        metrics.append(
            {"label": "deployment validation", "value": "missing", "tone": "mut", "path": None}
        )
        if overall == "ok":
            overall = "unknown"

    review = WIKI / "_review-queue.md"
    if review.is_file():
        try:
            _fm, body = split_fm(review.read_text(encoding="utf-8", errors="replace"))
            backlog = sum(
                1 for line in body.splitlines() if re.match(r"^\s*-\s+(?:\[[ xX]\]\s+)?\S", line)
            )
            metrics.append(
                {
                    "label": "review backlog",
                    "value": backlog,
                    "tone": "warn" if backlog else "ok",
                    "path": "_review-queue",
                }
            )
            if backlog and overall == "ok":
                overall = "warning"
        except OSError:
            pass

    sources = _load_dir("sources") if (WIKI / "sources").is_dir() else []
    dated = []
    for row in sources:
        value = row.get("created") or row.get("ingested_at") or row.get("updated")
        date = _as_date(value)
        if date:
            dated.append((date, row))
    if dated:
        latest_date, latest_row = max(dated, key=lambda pair: pair[0])
        age = (datetime.datetime.now(datetime.timezone.utc).date() - latest_date).days
        tone = "ok" if 0 <= age <= 1 else "warn" if age >= 0 else "mut"
        metrics.append(
            {
                "label": "latest ingest",
                "value": latest_date.isoformat(),
                "tone": tone,
                "path": _row_page(latest_row)["path"],
            }
        )
        if tone == "warn" and overall == "ok":
            overall = "warning"
    else:
        metrics.append({"label": "latest ingest", "value": "unknown", "tone": "mut", "path": None})
        if overall == "ok":
            overall = "unknown"

    artifacts = [item for group in _ops_groups() for item in group["items"] if item.get("path")]
    stale_n = sum(1 for item in artifacts if item.get("freshness") == "stale")
    unknown_n = sum(1 for item in artifacts if item.get("freshness") == "unknown")
    if stale_n:
        metrics.append(
            {
                "label": "stale artifacts",
                "value": stale_n,
                "tone": "crit",
                "path": next(
                    item["path"] for item in artifacts if item.get("freshness") == "stale"
                ),
            }
        )
        overall = "critical"
    elif artifacts:
        metrics.append(
            {
                "label": "artifact freshness",
                "value": "current",
                "tone": "ok",
                "path": artifacts[0]["path"],
            }
        )
    if unknown_n:
        metrics.append(
            {
                "label": "freshness unknown",
                "value": unknown_n,
                "tone": "mut",
                "path": next(
                    item["path"] for item in artifacts if item.get("freshness") == "unknown"
                ),
            }
        )
    return {"state": overall, "metrics": metrics}


def api_ops():
    return {"summary": _ops_summary(), "groups": _ops_groups()}


def api_policy():
    """Structured policy health; the Ops dashboard is its human-readable projection."""

    def load(name: str, default):
        try:
            value = json.loads((VAULT / ".okengine" / name).read_text(encoding="utf-8"))
            return value if isinstance(value, type(default)) else default
        except (OSError, json.JSONDecodeError):
            return default

    effective = load("effective-policy.json", {})
    coverage = load("policy-coverage.json", {})
    findings = load("policy-findings.json", {})
    return {
        "digest": effective.get("digest"),
        "rules": effective.get("rules") or [],
        "capabilities": effective.get("capabilities") or {},
        "waivers": effective.get("waivers") or [],
        "coverage": coverage.get("rules") or [],
        "findings": findings.get("findings") or [],
        "generated_at": findings.get("generated_at") or coverage.get("generated_at"),
    }


def _content_dirs() -> list:
    """Top-level content dirs ACTUALLY present under wiki/ — layout-agnostic basename resolution.
    Replaces a hardcoded 10-namespace tuple that 404'd pack-owned namespaces (detections/actor/cve/…)
    and walk-up sub-domain roots (invariant-audit M-1758). Fallback only: the direct wiki-relative
    path is tried first, and rglob under each dir reaches nested (partitioned/walk-up) pages."""
    try:
        return [d for d in WIKI.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]
    except OSError:
        return []
