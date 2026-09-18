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


def _defang(v: str) -> str:
    return v.replace("http://", "hxxp://").replace("https://", "hxxps://").replace(".", "[.]")


def _row_path(row: dict) -> str:
    return f"{row.get('_sub', '')}/{row.get('_rel') or row.get('_name', '')}".strip("/")


def _assessment_for_row(row: dict, spec: dict) -> dict | None:
    """Newest current ledger judgment for a dataset row and assessment family."""
    subject = _subject_key(_row_path(row))
    kind = str(spec.get("kind") or "").strip()
    statuses = {str(value) for value in (spec.get("statuses") or ["active", "disputed"])}
    return next(
        (
            record
            for record in _assessment_subject_index().get(subject, [])
            if (not kind or record.get("assessment_kind") == kind)
            and record.get("status") in statuses
        ),
        None,
    )


def _terminal_by_subject_key(subjects: dict[str, dict]) -> dict[str, dict]:
    """Re-key the projection by (namespace, slug) so a reshard cannot orphan a lookup.

    The projection stores the subject path AS IT WAS when the lane ran. A reshard between that run
    and the page view moves the page, the raw-path lookup misses, and every actor silently reads
    "Review not run" -- the same orphaning `_subject_key` already fixes for assessment records, left
    on the sibling lookup.

    A slug appearing twice in one namespace is a partition duplicate that
    `deployment_checks.check_partition_dups()` owns as a FAIL, so it must not be resolved by picking
    a winner here: if the two entries disagree, the ambiguous key is DROPPED and the row falls
    through to "Review not run". Guessing would publish one of two contradictory verdicts as fact.
    """
    index: dict[str, dict] = {}
    for raw, record in subjects.items():
        if not isinstance(record, dict):
            continue
        key = _subject_key(raw)
        if key in index and index[key] != record:
            index[key] = {}  # ambiguous -- refuse rather than choose
            continue
        index.setdefault(key, record)
    return {key: record for key, record in index.items() if record}


def _assessment_terminal_for_row(row: dict, spec: dict) -> dict | None:
    """Read the bounded-search projection; never scan the corpus on a UI request."""
    global _assessment_terminal_cache
    if str(spec.get("kind") or "") != "actor-country-linkage":
        return None
    cached_at, cached, by_key = _assessment_terminal_cache
    if time.monotonic() - cached_at >= 15:
        path = VAULT / ".okengine" / "actor-country-review-coverage.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached = payload.get("subjects") if isinstance(payload.get("subjects"), dict) else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            cached = {}
        by_key = _terminal_by_subject_key(cached)
        _assessment_terminal_cache = (time.monotonic(), cached, by_key)
    path_key = _row_path(row)
    # Exact path first, so a matching projection behaves exactly as before.
    return cached.get(path_key) or by_key.get(_subject_key(path_key))


def _assessment_terminal_label(state: str) -> str:
    return {
        "no-association-established": "No association established",
        "collection-required": "Collection required",
        "review-not-run": "Review not run",
        "assessed": "Assessment reference stale",
        "failed": "Review failed",
    }.get(state, "Review not run")


def _assessment_bucket(row: dict, spec: dict) -> tuple[str, dict | None]:
    """Epistemically safe aggregate bucket; never falls back to a canonical entity field."""
    record = _assessment_for_row(row, spec)
    if not record:
        terminal = _assessment_terminal_for_row(row, spec)
        state = str((terminal or {}).get("state") or "review-not-run")
        return f"__{state.replace('-', '_')}__", None
    state = str(record.get("epistemic_status") or "assessed").lower()
    if state in {"disputed", "inconclusive", "unknown"}:
        return f"__{state}__", record
    raw = record.get(str(spec.get("value_field") or "assessed_value"))
    if raw in (None, "", []):
        return "__metadata_unavailable__", record
    return str(raw), record


def _current_assessment_subject(row: dict) -> bool:
    """Only canonical, current subjects belong in assessment coverage aggregates."""
    return str(row.get("status") or "").casefold() != "tombstoned" and not bool(
        row.get("redirect_to")
    )


def _assessed_value_cell(row: dict, spec: dict) -> str:
    """Render a ledger judgment without laundering it into an unqualified fact."""
    record = _assessment_for_row(row, spec)
    if not record:
        terminal = _assessment_terminal_for_row(row, spec)
        state = str((terminal or {}).get("state") or "review-not-run")
        label = _assessment_terminal_label(state)
        reason = str(
            (terminal or {}).get("reason")
            or "No covering actor review result is available for this actor."
        )
        return (
            f'<a class="wl assessment-empty assessment-terminal assessment-{_esc(state)}" '
            f'data-page="dashboards/actor-review-status" '
            f'aria-label="{_esc(label)}; {_esc(reason)}" title="{_esc(reason)}">'
            f"{_esc(label)}</a>"
        )
    value_field = str(spec.get("value_field") or "assessed_value")
    raw_value = record.get(value_field)
    state = str(record.get("epistemic_status") or "assessed").lower()
    labels = spec.get("labels") if isinstance(spec.get("labels"), dict) else {}
    if state == "inconclusive":
        value = "Inconclusive"
    elif state == "unknown":
        value = "Unknown"
    elif raw_value in (None, "", []):
        value = "assessment metadata unavailable"
        state = "metadata-unavailable"
    else:
        value = str(labels.get(raw_value, labels.get(str(raw_value), raw_value)))
    state_label = {
        "reported": "Reported",
        "assessed": "Assessed",
        "confirmed": "Confirmed",
        "disputed": "Disputed",
        "inconclusive": "Inconclusive",
        "unknown": "Unknown",
        "metadata-unavailable": "Assessment metadata unavailable",
    }.get(state, "Assessed")
    confidence = record.get("confidence")
    confidence_text = (
        f"{round(float(confidence) * 100):d}%" if isinstance(confidence, (int, float)) else ""
    )
    band = str(record.get("confidence_band") or "").replace("-", " ").strip()
    review = "human review pending" if record.get("needs_review") else "reviewed"
    aria_bits = [f"{state_label} analytical judgment"]
    if band:
        aria_bits.append(f"{band} confidence")
    elif confidence_text:
        aria_bits.append(f"{confidence_text} confidence")
    aria_bits.append(review)
    aria = "; ".join(aria_bits)
    warn = (
        ' <span class="assessment-review-pending" title="human review pending" '
        'aria-hidden="true">⚠</span>'
        if record.get("needs_review")
        else ""
    )
    conf = (
        f' <span class="assessment-confidence">{_esc(confidence_text)}</span>'
        if confidence_text
        else ""
    )
    marker = "◆" if state == "confirmed" else "◇"
    return (
        f'<a class="wl assessed-value assessment-{_esc(state)}" '
        f'data-page="{_esc(record["path"])}" aria-label="{_esc(aria)}" '
        f'title="{_esc(aria)}"><strong>{_esc(value)}</strong> '
        f'<span class="assessment-marker" aria-hidden="true">{marker}</span>{conf}{warn}</a>'
    )


def _ref_link_cell(value, col: dict) -> str:
    """Render a page REFERENCE as a link to that page, titled by its own title.

    `link: true` links the ROW's page and ignores `field`, which is right for a title column and
    wrong for every relationship column: pointing `attributed_to` at `link: true` rendered the
    campaign's own name three times across three columns. A board that shows a relationship needs
    to link the OTHER end of it.

    Falls back to a readable form of the slug when the target has no resolvable title, so a
    dangling ref still reads as what it points at rather than vanishing to an em-dash — a missing
    target is information, not absence.
    """
    refs = value if isinstance(value, list) else [value]
    out = []
    for ref in refs[: int(col.get("max") or 2)]:
        rel = str(ref or "").strip().removesuffix(".md")
        if not rel:
            continue
        title = _ref_title(rel) or rel.rsplit("/", 1)[-1].replace("-", " ")
        out.append(f'<a class="wl" data-page="{_esc(rel)}">{_esc(title)}</a>')
    return ", ".join(out)


def _ds_cell(r: dict, col: dict) -> str:
    if col.get("ref_link"):
        raw = r.get(str(col.get("field") or ""))
        rendered = _ref_link_cell(raw, col)
        return rendered or str(col.get("empty") or "—")
    if col.get("link"):
        if col.get("source_title"):
            rel = r.get("_rel") or r["_name"]
            return f'<a class="wl" data-page="{_esc(r["_sub"])}/{_esc(rel)}">{_esc(_source_title(r))}</a>'
        return _page_link(r)
    if isinstance(col.get("assessment"), dict):
        return _assessed_value_cell(r, col["assessment"])
    raw = (
        _source_publisher(r) if col.get("source_publisher") else r.get(str(col.get("field") or ""))
    )
    v = raw
    labels_raw = col.get("labels") or col.get("value_labels") or {}
    labels = labels_raw if isinstance(labels_raw, dict) else {}
    if isinstance(v, list):
        v = ", ".join(
            str(labels.get(x, labels.get(str(x), x))) for x in v[: int(col.get("max") or 3)]
        )
    elif v not in (None, ""):
        v = labels.get(v, labels.get(str(v), v))
    v = str(col.get("empty") or "—") if v in (None, "", []) else str(v)
    if col.get("pct") and raw not in (None, "", []):
        # A 0..1 probability/ratio (e.g. an EPSS score, 0.00783) is unreadable raw — render it as a
        # percentage. One decimal below 10% so small-but-nonzero scores don't collapse to "0%"/"1%"
        # (0.00783 -> "0.8%"), whole numbers above (0.94 -> "94%"). okengine#259.
        try:
            p = float(raw) * 100
            v = f"{p:.1f}%" if 0 < p < 10 else f"{p:.0f}%"
        except (TypeError, ValueError):
            pass  # non-numeric -> leave the labelled/raw value as-is
    if col.get("date"):
        parsed = _as_date(raw)
        if raw not in (None, "") and parsed is None:
            return '<span class="t-warn nw">⚠ invalid date</span>'
        v = parsed.isoformat() if parsed else "—"
    if col.get("defang"):  # IOC hygiene: never render a live URL/domain
        return f"<code>{_esc(_defang(v))}</code>"
    tone = col.get("tone")
    # tone_by: pick the column tone FROM the cell value (a severity enum colours critical=crit,
    # high=warn, …) instead of one static colour for the whole column. Falls back to `tone`. #259.
    tb = col.get("tone_by")
    if isinstance(tb, dict) and raw not in (None, "", []):
        tone = tb.get(str(raw), tb.get(str(raw).lower(), tone))
    classes = [f"t-{tone}"] if tone in _TONES else []
    invalid_numeric = False
    if col.get("numeric") and v != "—":
        try:
            float(v)
        except (TypeError, ValueError):
            invalid_numeric = True
            classes.append("invalid-value")
            v = f"⚠ {v}"
    # A structured single-token value — a date, a number, or an enum like "moderate-high" — must
    # never break mid-token when the column squeezes (dates broke at their hyphens, confidence
    # enums at theirs). `_html_table`'s .num heuristic can't see it here (the cell arrives as
    # ready HTML), so tag it nowrap directly. Multi-word prose (a thesis/summary column) has
    # internal whitespace and keeps its normal word-wrap.
    if col.get("date") or (v.strip() and " " not in v.strip()):
        classes.append("nw")
    cls = f' class="{" ".join(classes)}"' if classes else ""
    title = (
        ' title="invalid numeric value; legacy data needs normalization"' if invalid_numeric else ""
    )
    return f"<span{cls}{title}>{_esc(v)}</span>"


def _drill_attrs(drill, *, value=None, item=None, page=None):
    """(class_suffix, attrs) that make an aggregate row/value navigable (okengine#189). A group_by
    bucket (value) or bignums item opens its filtered page LIST via /api/drill; a value_field bar
    (page) — already one page — opens that page directly. ('', '') when not navigable."""
    if page:  # value_field bar -> open the page itself
        return " drill", f' role="button" tabindex="0" data-drill data-dpage="{_esc(page)}"'
    if not drill or (value is None and item is None):
        return "", ""
    tab, bi = drill
    sel = f' data-dval="{_esc(str(value))}"' if value is not None else f' data-ditem="{item}"'
    return (
        " drill",
        f' role="button" tabindex="0" data-drill data-dtab="{_esc(tab)}" data-dbox="{bi}"{sel}',
    )


def _gb_values(v) -> list[str]:
    """The group_by buckets a row contributes: each element of a LIST field (so a page targeting
    ['government','finance'] counts toward both), or the single scalar. Empties dropped."""
    return (
        [str(x) for x in v if str(x).strip()]
        if isinstance(v, list)
        else ([str(v)] if v not in (None, "") else [])
    )


def _box_limit(box: dict, view: str) -> int:
    """How many rows/groups a widget shows — ONE definition, shared by every renderer, the meta
    line and the drill-through. Copies of this literal are exactly how "showing 8 of 83" drifts
    from the count the table actually cut, and how a drill re-cuts at a depth it never claimed.
    `limit: 0` is absence, not "show nothing" — a pack zeroing a box would otherwise render an
    empty widget that still advertises a total."""
    return int(box.get("limit") or _VIEW_DEFAULT_LIMIT.get(view, 8))


def _ds_pairs(box: dict, rows: list[dict]) -> list:
    """(label, value, unmapped, key) tuples for bars/chips — a group_by count or explicit fields.
    `unmapped` is True only when a `labels:` map is configured but this grouped value is absent
    from it (okengine#188). `key` is the RAW group value (drives a group_by drilldown filter), or
    None for value_field pairs (which are already one page each)."""
    if isinstance(box.get("assessment"), dict):
        spec = box["assessment"]
        labels = {
            str(k): str(v) for k, v in (spec.get("labels") or box.get("labels") or {}).items()
        }
        counts: Counter = Counter()
        confidences: dict[str, list[float]] = {}
        pending: Counter = Counter()
        for row in rows:
            if not _current_assessment_subject(row):
                continue
            key, record = _assessment_bucket(row, spec)
            counts[key] += 1
            if record and isinstance(record.get("confidence"), (int, float)):
                confidences.setdefault(key, []).append(float(record["confidence"]))
            if record and record.get("needs_review"):
                pending[key] += 1
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
        assessed = []
        remainder = []
        for key, count in counts.items():
            if key in special:
                remainder.append((special[key], count, False, key))
                continue
            conf = confidences.get(key) or []
            avg = f" {round(sum(conf) / len(conf) * 100):d}% avg" if conf else ""
            warn = f" ⚠{pending[key]}" if pending[key] else ""
            assessed.append(
                (
                    f"{labels.get(key, key)} ◇{avg}{warn}",
                    count,
                    bool(labels) and key not in labels,
                    key,
                )
            )
        limit = _box_limit(box, "bars")
        assessed.sort(key=lambda item: (-item[1], item[0]))
        remainder.sort(key=lambda item: (-item[1], item[0]))
        return assessed[:limit] + remainder
    if box.get("group_by"):
        labels = {str(k): str(v) for k, v in (box.get("labels") or {}).items()}
        aliases = {str(k): str(v) for k, v in (box.get("aliases") or {}).items()}
        drop = {"", "None", "Unknown", "nan"}
        cnt: Counter = Counter()
        for r in rows:  # list fields explode: each element is its own bucket
            for raw in _gb_values(r.get(box["group_by"])):
                s = aliases.get(raw, raw)
                if s not in drop:
                    cnt[s] += 1
        limit = _box_limit(box, "bars")
        # okengine#259: with a `labels:` vocabulary configured, `bucket_unmapped` COLLAPSES every
        # value outside it into ONE "unmapped (N)" row appended after the top-N sanctioned
        # categories — so the raw NAICS codes, the "China ⚠" near-duplicate, and free-text leaking
        # into an enum stop each occupying a real-category slot. The row drills (via _UNMAPPED_KEY)
        # to the offender pages. Without bucket_unmapped, the demote path below just RANKS mapped
        # above unmapped (still shown individually) — the pre-existing, less aggressive behavior.
        if labels and box.get("bucket_unmapped"):
            mapped = sorted(
                ((k, v) for k, v in cnt.items() if k in labels), key=lambda kv: (-kv[1], kv[0])
            )[:limit]
            pairs = [(labels.get(k, k), v, False, k) for k, v in mapped]
            um = [(k, v) for k, v in cnt.items() if k not in labels]
            if um:
                pairs.append((f"unmapped ({len(um)})", sum(v for _, v in um), True, _UNMAPPED_KEY))
            return pairs
        if labels:
            ranked = sorted(cnt.items(), key=lambda kv: (kv[0] not in labels, -kv[1], kv[0]))[
                :limit
            ]
        else:
            ranked = cnt.most_common(limit)
        return [(labels.get(k, k), v, bool(labels) and k not in labels, k) for k, v in ranked]
    vf = str(box.get("value_field") or "")
    lf = str(box.get("label_field") or "title")
    rs = [r for r in rows if r.get(vf) not in (None, "")]
    rs = _ds_sorted(rs, {"field": vf, "desc": True})[: _box_limit(box, "bars")]
    # key = the bar's own page path — a value_field bar is one page, so it opens directly
    return [
        (
            str(r.get(lf) or r.get("name") or r.get("_name") or "?"),
            int(float(r.get(vf) or 0)),
            False,
            f"{r.get('_sub', '')}/{r.get('_rel') or r.get('_name', '')}".strip("/"),
        )
        for r in rs
    ]


def _v_table(box: dict, rows: list[dict]) -> str:
    cols = [c for c in (box.get("columns") or []) if isinstance(c, dict)]
    rows = _ds_sorted(rows, box.get("sort") or {})[: _box_limit(box, "table")]
    if not rows or not cols:
        return ""
    table = _html_table(
        [str(c.get("label") or c.get("field") or "") for c in cols],
        [[_ds_cell(r, c) for c in cols] for r in rows],
    )
    if any(isinstance(col.get("assessment"), dict) for col in cols):
        table += (
            '<p class="assessment-legend"><span aria-hidden="true">◇</span> Assessed judgment — '
            "supported by evaluated evidence, but not presented as established fact. Unassessed cells "
            "distinguish bounded no-finding, collection required, and review not run. Select a value "
            "to inspect its evidence, alternatives, confidence, and review state. "
            '<a class="wl" data-page="assessments/_about">How assessments work</a>.</p>'
        )
    return table
