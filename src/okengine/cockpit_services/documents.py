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


def _latest_doc(box: dict):
    """Return (frontmatter, body, page path, stem) for the latest matching document."""
    d = str(box.get("dir") or "").strip("/")
    pat = str(box.get("glob") or "*.md")
    if not pat.endswith(".md"):
        pat += ".md"
    base = WIKI / d
    # rglob so a PARTITIONED doc dir matches; read the winner by its path RELATIVE TO base — a sub-dir
    # hit passed as a bare basename to safe_read(base, name) 404s the ENTIRE tab (M-1513). Sort by the
    # filename DATE (then name), NOT the full path: a full-path sort ranks a flat/letter-leading page
    # above a YYYY/-sharded newer one (batch-2 re-verify). _visible_page drops reserved sub-dirs.
    cands = (
        sorted(
            (p for p in base.rglob(pat) if _visible_page(p, base)),
            key=lambda p: (_file_date(p.name) or "", p.name),
            reverse=True,
        )
        if base.is_dir()
        else []
    )
    if not cands:
        return None
    fm, body = split_fm(safe_read(base, str(cands[0].relative_to(base))))
    page = str(cands[0].relative_to(WIKI).with_suffix(""))
    return fm, body, page, cands[0].stem


def _v_doc(box: dict):
    """Render the latest matching document inline (truncated at _DOC_INLINE_CAP — a panel is a
    view, not a document reader). Returns (html, meta)."""
    latest = _latest_doc(box)
    if not latest:
        return "", ""
    _fm, body, page, stem = latest
    if len(body) > _DOC_INLINE_CAP:
        cut = body.rfind("\n", 0, _DOC_INLINE_CAP)
        clipped = body[: cut if cut > 0 else _DOC_INLINE_CAP]
        note = (
            f'<p class="dnote">Document truncated for inline view '
            f"({len(body) // 1024} KB total) — "
            f'<a class="wl" data-page="{_esc(page)}">open the full page</a>.</p>'
        )
        return f'<div class="ddoc">{render_md(clipped)}{note}</div>', stem
    return f'<div class="ddoc">{render_md(body)}</div>', stem


def _v_operation_control(box: dict) -> str:
    """Generic plan/confirm/start surface for one declarative operation."""
    name = str(box.get("operation") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,79}", name):
        return '<p class="dnote">Invalid operation configuration.</p>'
    arguments = box.get("arguments") or []
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        return '<p class="dnote">Invalid operation arguments.</p>'
    description = str(
        box.get("description")
        or "Plan the operation to confirm its frozen scope before starting it."
    )
    disabled = "" if _OPERATION_ENABLED else " disabled"
    availability = (
        ""
        if _OPERATION_ENABLED
        else (
            '<p class="op-unavailable">Operation controls are not enabled for this deployment.</p>'
        )
    )
    return (
        f'<div class="operation-control" data-operation="{_esc(name)}" '
        f'data-arguments="{_esc(json.dumps(arguments))}">'
        f"<p>{_esc(description)}</p>{availability}"
        f'<div class="operation-actions"><button data-operation-plan{disabled}>Plan scope</button>'
        f"<button data-operation-run disabled>Start operation</button></div>"
        '<div class="operation-result" aria-live="polite">Plan required before execution.</div>'
        "</div>"
    )


def _markdown_section(body: str, heading: str) -> str:
    """Extract a Markdown heading's body through the next same-or-higher heading."""
    wanted = str(heading or "").strip().lower()
    if not wanted:
        return ""
    lines = body.splitlines()
    start = None
    level = 0
    for i, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and match.group(2).strip().lower() == wanted:
            start, level = i + 1, len(match.group(1))
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start, len(lines)):
        if i > start and re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", lines[i]):
            end = i
            break
        match = re.match(r"^(#{1,6})\s+", lines[i])
        if match and len(match.group(1)) <= level:
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def _markdown_sections(body: str, max_sections: int = 2) -> str:
    """Select the first bounded peer sections when no named summary section is configured."""
    lines = body.splitlines()
    headings = []
    for i, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((i, len(match.group(1))))
    if not headings:
        return ""
    level = headings[0][1]
    if level == 1 and any(candidate == 2 for _line, candidate in headings[1:]):
        level = 2
    peers = [
        (position, line)
        for position, (line, candidate) in enumerate(headings)
        if candidate == level
    ]
    take = peers[: max(1, max_sections)]
    next_position = take[-1][0] + 1
    end = headings[next_position][0] if next_position < len(headings) else len(lines)
    return "\n".join(lines[take[0][1] : end]).strip()


def _v_doc_summary(box: dict):
    """Render one named section, or a bounded excerpt, plus a link to the complete document."""
    latest = _latest_doc(box)
    if not latest:
        return "", ""
    fm, body, page, stem = latest
    configured = box.get("section")
    headings = (configured if isinstance(configured, list) else [configured]) if configured else []
    excerpt = (
        next(
            filter(
                None,
                (
                    _markdown_section(body, heading)
                    for heading in headings
                    if str(heading or "").strip()
                ),
            ),
            "",
        )
        if headings
        else _markdown_sections(body, int(box.get("max_sections") or 2))
    )
    fallback = False
    if not excerpt:
        fallback = True
        limit = max(200, min(2400, int(box.get("max_chars") or 900)))
        plain = re.sub(r"^---.*?---\s*", "", body, flags=re.S).strip()
        excerpt = plain[:limit].rsplit(" ", 1)[0] if len(plain) > limit else plain
        if len(plain) > len(excerpt):
            excerpt += " …"
    title = str(fm.get("title") or stem)
    note = (
        '<p class="dsummary-note">Summary section unavailable; showing an excerpt.</p>'
        if fallback
        else ""
    )
    action = (
        f'<p class="dsummary-action"><a class="wl" data-page="{_esc(page)}">'
        f"Read full brief: {_esc(title)} →</a></p>"
    )
    return f'<div class="ddoc dsummary">{note}{render_md(excerpt)}{action}</div>', stem


def _application_ref(value) -> str:
    """Render a declared operation/artifact reference, linking canonical vault pages."""
    ref = str(value or "").strip()
    if ref.startswith("dashboards/"):
        return f'<a class="wl" data-page="{_esc(ref)}">{_esc(ref)}</a>'
    return f"<code>{_esc(ref or '—')}</code>"


def _v_application(application: dict) -> str:
    """Generic application-profile summary; no CHE/domain names are hardcoded here."""
    profile = str(application.get("profile") or "application")
    version = str(application.get("profile_version") or "—")
    propositions = application.get("propositions") or []
    rows = []
    for binding in propositions:
        operations = (
            binding.get("operations") if isinstance(binding.get("operations"), dict) else {}
        )
        op_text = (
            " · ".join(
                f"{_esc(kind)}: {_application_ref(operation)}"
                for kind, operation in operations.items()
            )
            or "—"
        )
        rows.append(
            f"<tr><td><strong>{_esc(binding.get('type') or '—')}</strong></td>"
            f"<td><code>{_esc(binding.get('namespace') or '—')}</code></td><td>{op_text}</td></tr>"
        )
    proposition_table = (
        "<table><thead><tr><th>Proposition</th><th>Namespace</th><th>Lifecycle operations</th></tr>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows
        else '<p class="dnote">No proposition bindings.</p>'
    )

    def refs(title: str, values: dict) -> str:
        items = "".join(
            f"<tr><td>{_esc(name)}</td><td>{_application_ref(value)}</td></tr>"
            for name, value in values.items()
        )
        return f"<h3>{_esc(title)}</h3><table><tbody>{items}</tbody></table>" if items else ""

    return (
        '<div class="app-contract">'
        f'<div class="bignums"><div class="bn-item"><div class="bn-v t-ok">active</div>'
        f'<div class="bn-l">{_esc(_humanize(profile))} · v{_esc(version)}</div></div>'
        f'<div class="bn-item"><div class="bn-v">{len(propositions)}</div>'
        '<div class="bn-l">bound proposition classes</div></div></div>'
        f"<h3>Lifecycle bindings</h3>{proposition_table}"
        f"{refs('Analyst surfaces', application.get('surfaces') or {})}"
        f"{refs('Queues', application.get('queues') or {})}"
        f"{refs('Success measures', application.get('success_measures') or {})}</div>"
    )


def api_application():
    """Operational inspection surface for the deployed application contract."""
    application = cockpit_config().get("application")
    if not application:
        raise HTTPException(404, "no application declaration")
    return {
        "title": _humanize(application["profile"]),
        "profile": application["profile"],
        "profile_version": application.get("profile_version") or "",
        "html": _v_application(application),
    }


def _dataset_meta(box: dict, view: str, rows: list[dict]) -> str:
    """Describe what the widget actually shows, not the implementation unit that stores it.

    Packs can override the wording with `meta_template`; the engine supplies stable counts. Explicit
    `meta` remains authoritative for domain-specific windows and denominators.
    """
    eligible = _ds_sorted(rows, box.get("sort") or {})
    total = len(eligible)
    limit = _box_limit(box, view)
    groups = len(_ds_pairs(box, eligible)) if view in ("bars", "chips") else 0
    shown = groups if groups else min(total, limit) if view in ("table", "cards") else total
    values = _MetaValues(
        total=f"{total:,}", shown=f"{shown:,}", groups=f"{groups:,}", limit=f"{limit:,}"
    )
    template = box.get("meta_template")
    if template:
        try:
            return str(template).format_map(values)
        except (ValueError, TypeError):
            return str(template)
    if view == "table":
        return f"showing {shown:,} of {total:,} records"
    if view in ("bars", "chips"):
        return f"top {groups:,} groups from {total:,} records"
    if view == "cards":
        return f"showing {shown:,} of {total:,} trends"
    if view == "coverage":
        return f"coverage from {total:,} records"
    return f"{total:,} records"


def api_tab(key: str):
    cfg = cockpit_config()
    canonical_key = (cfg.get("tab_aliases") or {}).get(key, key)
    d = (cfg.get("tab_defs") or {}).get(canonical_key)
    if not d:
        raise HTTPException(404, "no such tab")
    views = {
        "table": _v_table,
        "bars": _v_bars,
        "chips": _v_chips,
        "bignums": _v_bignums,
        "cards": _v_cards,
        "coverage": _v_coverage,
    }
    drillable = {"bars", "chips", "bignums", "coverage"}
    boxes = []
    for bi, b in enumerate(d["boxes"]):
        view = str(b.get("view") or "table")
        meta = str(b.get("meta") or "")
        unmapped: list = []
        meta_drill: dict | None = None
        if view == "application":
            html = _v_application(cfg.get("application") or {})
        elif view == "application-help":
            summary = _esc(str(b.get("summary") or "Open workspace guide"))
            html = (
                f'<details class="app-help"><summary>{summary}</summary>'
                f"{_v_application(cfg.get('application') or {})}</details>"
            )
        elif view == "review-queue":
            html = _v_review_queue(b)
        elif view == "tid-trace":
            html = _v_tid_trace(b)
        elif view == "tid-actor-posture":
            html = _v_tid_actor_posture(b)
        elif view == "tid-facet-matrix":
            html = _v_tid_facet_matrix(b)
        elif view == "tid-detection-dossier":
            html = _v_tid_detection_dossier(b)
        elif view == "tid-validation-queue":
            html = _v_tid_validation_queue(b)
        elif view == "tid-gap-workbench":
            html = _v_tid_gap_workbench(b)
        elif view in ("doc", "doc-summary"):
            renderer = _v_doc_summary if view == "doc-summary" else _v_doc
            html, stem = renderer(b)
            meta = meta or stem
        elif view == "operation-control":
            html = _v_operation_control(b)
            meta = meta or ("available" if _OPERATION_ENABLED else "not configured")
        else:
            rows = _configured_rows(b)
            fn = views.get(view)
            if not fn:
                html = f'<p class="dnote">Unsupported configured view: {_esc(view)}</p>'
                meta = "configuration error"
            elif view in drillable:  # rows/values open a filtered list (okengine#189)
                html = fn(b, rows, (canonical_key, bi))
            else:
                html = fn(b, rows)
            if not meta and rows:
                meta = _dataset_meta(b, view, rows)
            if view in ("table", "cards") and len(rows) > _box_limit(b, view):
                # only when rows were actually withheld — a meta line that opens a list identical
                # to the table above it is a dead affordance, not a feature.
                meta_drill = {"tab": canonical_key, "box": bi}
            if view in ("bars", "chips") and not b.get("bucket_unmapped"):
                # surface partial-labels-map drift (okengine#188). Skipped when bucket_unmapped is
                # on — there the single collapsed "unmapped (N)" bar IS the surfacing, and its label
                # is not a real drift value to list on the degraded card (okengine#259).
                unmapped = [l for l, _v, um, _k in _ds_pairs(b, rows) if um]
        if not html and b.get("empty"):  # honest-empty: pipeline state is information
            html = f'<p class="dnote">{_esc(str(b["empty"]))}</p>'
            meta = meta or "awaiting first data"
        if html:
            box = {
                "title": str(b.get("title") or ""),
                "meta": meta,
                "span": int(b.get("span") or 6),
                "html": html,
            }
            if meta_drill:
                box["meta_drill"] = meta_drill
            # ``section`` belongs to doc-summary: it selects the markdown section rendered
            # inside the card.  It must not double as a grid grouping label; doing so inserts a
            # full-width heading before (for example) the Weekly Brief and forces two span-6
            # brief cards onto separate rows. Existing non-document pack contributions used
            # ``section`` for grouping, so retain that input/API contract while exposing the
            # unambiguous client field ``layout_section``.
            layout_section = b.get("layout_section")
            if not layout_section and view != "doc-summary":
                layout_section = b.get("section")
            if str(layout_section or "").strip():
                box["layout_section"] = str(layout_section).strip()
                if b.get("section") and view != "doc-summary":
                    box["section"] = str(b["section"]).strip()
            if unmapped:  # only present when the card is degraded
                box["unmapped"] = unmapped
            # Provenance affordance (okengine#259 Rec 11): a panel measuring the CORPUS (reporting
            # volume / collection coverage) rather than the THREAT must be visibly distinct, so a
            # "ransomware ▼" coverage dip isn't misread as the threat declining. The pack tags such a
            # panel `provenance:` (a short label + optional note); the UI renders a badge + tooltip.
            prov = b.get("provenance")
            if isinstance(prov, str) and prov.strip():
                prov = {"label": prov.strip()}
            if isinstance(prov, dict) and str(prov.get("label") or "").strip():
                box["provenance"] = {
                    "label": str(prov["label"]).strip(),
                    "note": str(prov.get("note") or "").strip(),
                }
            boxes.append(box)
    # Echo the route identity so API consumers can correlate a declarative response without
    # reconstructing it from the request URL (and so composed guest tabs have the same shape as
    # built-in tab payloads).
    return {"key": key, "canonical_key": canonical_key, "label": d["label"], "boxes": boxes}
