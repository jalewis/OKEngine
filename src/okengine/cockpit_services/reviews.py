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


def _review_candidate(path: str) -> Path:
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "bad path")
    cand = WIKI / (path.removesuffix(".md") + ".md")
    if not cand.is_file():
        raise HTTPException(404, "page not found")
    try:
        cand.resolve().relative_to(WIKI.resolve())
    except ValueError:
        raise HTTPException(403, "blocked")
    return cand


def api_review(path: str = Query(...)):
    return _review_detail(_review_candidate(path))


def _invalidate_review_snapshot() -> None:
    """Force the NEXT read to rebuild synchronously (an operator review action must be reflected
    immediately, so serve-stale is wrong right after a state change)."""
    global _review_snapshot_cache
    _review_snapshot_cache = (float("-inf"), [], [])


def _review_queue_snapshot() -> tuple[list[dict], list[dict]]:
    """The complete review worklist, STALE-WHILE-REVALIDATE (same contract as _load_dir).

    The rebuild walks the WHOLE vault (head-read + frontmatter parse of every page — tens of
    seconds at 10k+ pages). It used to run ON the request path behind a 30s cache, so any tab
    carrying a review-queue box (Detections, CHE) hung on a bare 'Loading…' for ~24s once every
    30 seconds (2026-07-19 UI sweep). Now a request only ever blocks when this process has NEVER
    built a snapshot; after that, expiry serves the last snapshot and kicks ONE background
    rebuild."""
    global _review_snapshot_cache, _review_snapshot_refreshing
    cached_at, cached_rows, cached_records = _review_snapshot_cache
    if time.monotonic() - cached_at < _REVIEW_SNAPSHOT_TTL:
        return cached_rows, cached_records
    if cached_at != float("-inf"):
        # stale but present: serve it now, refresh off the request path (single-flight)
        kick = False
        with _review_snapshot_lock:
            if not _review_snapshot_refreshing:
                _review_snapshot_refreshing = True
                kick = True
        if kick:

            def _work():
                global _review_snapshot_cache, _review_snapshot_refreshing
                try:
                    _review_snapshot_cache = (time.monotonic(), *_build_review_snapshot())
                finally:
                    with _review_snapshot_lock:
                        _review_snapshot_refreshing = False

            threading.Thread(target=_work, daemon=True, name="review-snapshot-refresh").start()
        return cached_rows, cached_records
    with _review_snapshot_lock:  # first build of this process — nothing to serve
        cached_at, cached_rows, cached_records = _review_snapshot_cache
        if time.monotonic() - cached_at < _REVIEW_SNAPSHOT_TTL:
            return cached_rows, cached_records
        rows, all_records = _build_review_snapshot()
        _review_snapshot_cache = (time.monotonic(), rows, all_records)
        return rows, all_records


def _build_review_snapshot() -> tuple[list[dict], list[dict]]:
    """The uncached full-vault worklist build (see _review_queue_snapshot for the caching contract)."""
    records = {}
    all_records = sorted(_review_records(), key=lambda r: str(r.get("requested_at") or ""))
    for rec in all_records:
        if rec.get("subject"):
            records[str(rec["subject"])] = rec
    rows = []
    for p in WIKI.rglob("*.md"):
        try:
            rel = p.relative_to(WIKI)
        except ValueError:
            continue
        if any(part.startswith(("_", ".")) for part in rel.parts) or "operational" in rel.parts:
            continue
        try:
            fm, body = split_fm(_read_head(p, 65536))
        except OSError:
            continue
        if not isinstance(fm, dict) or not _requires_review(fm, body):
            continue
        subject = rel.as_posix()[:-3]
        rec = records.get(subject) or {}
        reasons = rec.get("reasons") or _review_reasons(fm, body)
        evidence = _evidence_sources(fm)
        resolved, total = sum(1 for e in evidence if e.get("page")), len(evidence)
        resolution = "none" if not total else "complete" if resolved == total else "partial"
        updated = str(fm.get("last_updated") or fm.get("updated") or fm.get("created") or "")
        parsed = _as_date(updated)
        rows.append(
            {
                "subject": subject,
                "title": str(fm.get("title") or fm.get("name") or p.stem),
                "type": str(fm.get("type") or ""),
                "state": str(rec.get("state") or "open"),
                "reasons": reasons,
                "assigned_to": rec.get("assigned_to"),
                "updated": updated,
                "age_days": max(0, (TODAY() - parsed).days) if parsed else None,
                "evidence_total": total,
                "evidence_resolved": resolved,
                "source_resolution": resolution,
                "machine_eligible": bool(rec.get("machine_eligible")),
            }
        )
    priority = {
        "grounding": 0,
        "conflict": 1,
        "categorical-confidence": 2,
        "agent-draft": 3,
        "legacy-unspecified": 4,
    }
    # Reason priority still leads, but WITHIN a band the newest work comes first: this is a
    # worklist someone opens to see what just arrived, and ascending `updated` buried every
    # new item under the whole backlog. `updated` is inverted rather than reverse-sorting the
    # whole key, which would also invert the priority bands and the subject tiebreak.
    rows.sort(
        key=lambda row: (
            min((priority.get(str(r.get("code")), 9) for r in row["reasons"]), default=9),
            _desc(row["updated"]),
            row["subject"],
        )
    )
    return rows, all_records


def api_reviews(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    reason: str = Query(""),
    page_type: str = Query(""),
    state: str = Query(""),
    assignment: str = "",
    source_resolution: str = "",
    age: str = "",
    machine_eligible: str = "",
    page_types: str = "",
):
    """Complete, paginated review worklist; the Markdown dashboard remains only a projection."""
    snapshot, all_records = _review_queue_snapshot()
    rows = []
    type_scope = {value.strip() for value in page_types.split(",") if value.strip()}
    for row in snapshot:
        codes = [str(r.get("code") or "") for r in row["reasons"] if isinstance(r, dict)]
        row_state, ptype = row["state"], row["type"]
        resolution, eligible, age_days = (
            row["source_resolution"],
            row["machine_eligible"],
            row["age_days"],
        )
        assigned_to = row["assigned_to"]
        if reason and reason not in codes:
            continue
        if type_scope and ptype not in type_scope:
            continue
        if page_type and page_type != ptype:
            continue
        if state and state != row_state:
            continue
        if assignment == "unassigned" and assigned_to:
            continue
        if assignment == "assigned" and not assigned_to:
            continue
        if assignment not in {"", "assigned", "unassigned"} and assignment != assigned_to:
            continue
        if source_resolution and source_resolution != resolution:
            continue
        if machine_eligible in {"true", "false"} and eligible != (machine_eligible == "true"):
            continue
        if age == "0-30" and (age_days is None or age_days > 30):
            continue
        if age == "31-90" and (age_days is None or not 31 <= age_days <= 90):
            continue
        if age == "91+" and (age_days is None or age_days < 91):
            continue
        rows.append(row)
    states = Counter(row["state"] for row in rows)
    reasons_count = Counter(
        code
        for row in rows
        for code in [str(r.get("code") or "") for r in row["reasons"] if isinstance(r, dict)]
    )
    ages = [row["age_days"] for row in rows if row["age_days"] is not None]
    reopened = sum(reasons_count[c] for c in ("changed-after-approval",))
    cutoff = TODAY() - datetime.timedelta(days=30)
    throughput = 0
    for rec in all_records:
        for event in rec.get("history") or []:
            if not isinstance(event, dict) or not event.get("decision"):
                continue
            when = _as_date(event.get("decision_at"))
            if when and when >= cutoff:
                throughput += 1
    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "items": rows[offset : offset + limit],
        "facets": {
            "states": dict(states),
            "reasons": dict(reasons_count),
            "types": dict(Counter(row["type"] for row in rows)),
            "source_resolution": dict(Counter(row["source_resolution"] for row in rows)),
        },
        "metrics": {
            "oldest_days": max(ages) if ages else None,
            "assigned": sum(1 for row in rows if row["assigned_to"]),
            "throughput_30d": throughput,
            "reopened": reopened,
            "reopen_rate": round(reopened / max(1, len(rows) + throughput), 4),
        },
    }


def _same_origin_review(request: Request) -> bool:
    if request.headers.get("x-okengine-review") != "1":
        return False
    origin = request.headers.get("origin")
    if not origin:  # non-browser/API tests; Basic auth still protects the endpoint
        return True
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return hmac.compare_digest(origin.rstrip("/"), expected.rstrip("/"))


def _same_origin_operation(request: Request) -> bool:
    if request.headers.get("x-okengine-operation") != "1":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    return hmac.compare_digest(origin.rstrip("/"), expected.rstrip("/"))


def _operation_request(path: str, *, data: dict | None = None, method: str = "GET"):
    request = urllib.request.Request(
        _OPERATION_API + path,
        data=json.dumps(data).encode("utf-8") if data is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {_OPERATION_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=310 if path.endswith("/plan") else 20
        ) as response:  # nosec B310 - configured internal HTTP API
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"detail": "operation service rejected the request"}
        raise HTTPException(exc.code, str(detail.get("detail") or detail.get("error") or detail))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(503, f"operation service unavailable: {exc}")


def api_operations():
    if not _OPERATION_ENABLED:
        raise HTTPException(404, "operation capability is disabled")
    return _operation_request("/operations")


async def api_operation_plan(name: str, request: Request):
    if not _OPERATION_ENABLED:
        raise HTTPException(404, "operation capability is disabled")
    if not _same_origin_operation(request):
        raise HTTPException(403, "operation request failed same-origin protection")
    data = await request.json()
    return _operation_request(f"/operations/{name}/plan", data=data, method="POST")


async def api_operation_run(name: str, request: Request):
    if not _OPERATION_ENABLED:
        raise HTTPException(404, "operation capability is disabled")
    if not _same_origin_operation(request):
        raise HTTPException(403, "operation request failed same-origin protection")
    data = await request.json()
    return _operation_request(f"/operations/{name}/run", data=data, method="POST")


def api_operation_request(request_id: str):
    if not _OPERATION_ENABLED:
        raise HTTPException(404, "operation capability is disabled")
    return _operation_request(f"/operations/requests/{request_id}")


async def api_review_decision(request: Request):
    if not _REVIEW_ENABLED:
        raise HTTPException(404, "review write capability is disabled")
    if not _same_origin_review(request):
        raise HTTPException(403, "review request failed same-origin protection")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    payload = {
        "path": str(data.get("path") or ""),
        "decision": str(data.get("decision") or ""),
        "note": str(data.get("note") or ""),
        "expected_version": data.get("expected_version"),
        "expected_hash": str(data.get("expected_hash") or ""),
        "review_id": str(data.get("review_id") or ""),
        "reviewer": _REVIEWER,
        "service": "cockpit",
    }
    req = urllib.request.Request(
        _REVIEW_API + "/review/resolve",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {_REVIEW_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # nosec B310 - configured internal review API
            result = json.loads(response.read().decode("utf-8"))
            _invalidate_review_snapshot()
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": "review service rejected the decision"}
        raise HTTPException(exc.code, str(detail.get("error") or detail))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(503, f"review service unavailable: {exc}")


async def api_review_assign(request: Request):
    if not _REVIEW_ENABLED:
        raise HTTPException(404, "review write capability is disabled")
    if not _same_origin_review(request):
        raise HTTPException(403, "review request failed same-origin protection")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    payload = {
        "path": str(data.get("path") or ""),
        "expected_version": data.get("expected_version"),
        "expected_hash": str(data.get("expected_hash") or ""),
        "review_id": str(data.get("review_id") or ""),
        "reviewer": _REVIEWER,
        "service": "cockpit",
    }
    req = urllib.request.Request(
        _REVIEW_API + "/review/assign",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {_REVIEW_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # nosec B310 - configured internal review API
            result = json.loads(response.read().decode("utf-8"))
            _invalidate_review_snapshot()
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": "review service rejected assignment"}
        raise HTTPException(exc.code, str(detail.get("error") or detail))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(503, f"review service unavailable: {exc}")


def _subject_path(value: Any) -> str:
    raw = str(value or "").strip()
    match = _WIKILINK.search(raw)
    if match:
        raw = match.group(1).strip()
    raw = raw.removeprefix("wiki/").removesuffix(".md")
    return raw


def _subject_key(value: Any) -> str:
    """Identify a subject by (namespace, slug), IGNORING the shard segments between them.

    A partitioned namespace re-files pages as it grows, and back again as it shrinks --
    `entities/q/i/qilin-ransomware` became `entities/q/qilin-ransomware` on this vault. An
    assessment records `subject:` as the path AT WRITE TIME, so every reshard silently orphans every
    judgment pointing into the old shard: the record stays `active` on disk and the panel reports
    "Review not run", which is indistinguishable from never having been assessed. Measured: 10 of 43
    live attributions went dark between two reads hours apart, including a version-45 `active` RU
    judgment.

    Same namespace + same slug IS the same page -- that is the partition invariant, and two pages
    sharing a slug in one namespace is a duplicate that `deployment_checks.check_partition_dups()`
    owns. So comparing on (namespace, slug) is safe and makes the lookup immune to reshards, which
    is what `corpus_audit._subject_key` already does for the same reason.
    """
    parts = [part for part in _subject_path(value).split("/") if part]
    if len(parts) < 2:
        return "/".join(parts)
    return f"{parts[0]}/{parts[-1]}"


def _assessment_subject_index() -> dict[str, list[dict]]:
    """Current assessment records keyed by their canonical subject page."""
    global _assessment_subject_cache
    cached_at, cached = _assessment_subject_cache
    if time.monotonic() - cached_at < 30:
        return cached
    with _assessment_subject_lock:
        cached_at, cached = _assessment_subject_cache
        if time.monotonic() - cached_at < 30:
            return cached
        out: dict[str, list[dict]] = {}
        base = WIKI / "assessments"
        for page in base.rglob("*.md") if base.is_dir() else []:
            try:
                fm, _ = split_fm(_read_head(page, 131072))
            except OSError:
                continue
            if not isinstance(fm, dict) or str(fm.get("type") or "") not in {
                "assessment",
                "actor-assessment",
            }:
                continue
            # Actor pages present current judgments. Superseded/retired records remain directly
            # browsable as audit history, but must not read as active evidence on the subject.
            if str(fm.get("status") or "").strip().lower() in {
                "superseded",
                "retired",
                "tombstoned",
            }:
                continue
            subject = _subject_path(fm.get("subject") or fm.get("subject_ref"))
            if not subject.startswith("entities/"):
                continue
            subject = _subject_key(subject)  # reshard-proof: (namespace, slug)
            rel = page.relative_to(WIKI).as_posix()[:-3]
            confidence = fm.get("confidence")
            out.setdefault(subject, []).append(
                {
                    "path": rel,
                    "title": str(fm.get("title") or fm.get("claim") or page.stem),
                    "claim": str(fm.get("claim") or ""),
                    "status": str(fm.get("status") or ""),
                    "assessment_kind": str(fm.get("assessment_kind") or ""),
                    "assessed_value": fm.get("assessed_value"),
                    "assessed_label": str(fm.get("assessed_label") or ""),
                    "epistemic_status": str(fm.get("epistemic_status") or "assessed"),
                    "confidence": round(float(confidence), 3)
                    if isinstance(confidence, (int, float))
                    else None,
                    "confidence_band": str(fm.get("confidence_band") or ""),
                    "as_of": str(fm.get("as_of") or "")[:10],
                    "last_updated": str(fm.get("last_updated") or fm.get("as_of") or ""),
                    "needs_review": bool(fm.get("needs_review")),
                    "reviewed_by": str(fm.get("reviewed_by") or ""),
                    "reviewed_on": str(fm.get("reviewed_on") or ""),
                    # The ANALYTIC SUBSTANCE (okengine#563). The panel used to carry only a headline,
                    # so an entity page showed a one-line claim while the reasoning behind it -- why
                    # this confidence, what else could explain the observation, what would move it --
                    # sat one click away and was never read. Surfaced, not copied into the entity: the
                    # assessment stays the single source of truth and states its own scope.
                    "question": str(fm.get("question") or ""),
                    "relationship_level": str(fm.get("relationship_level") or ""),
                    "evidence_state": str(fm.get("evidence_state") or ""),
                    "confidence_rationale": str(fm.get("confidence_rationale") or ""),
                    "alternatives": [str(x) for x in (fm.get("alternatives") or [])][:4],
                    "would_increase_confidence": [
                        str(x) for x in (fm.get("would_increase_confidence") or [])
                    ][:3],
                    "would_decrease_confidence": [
                        str(x) for x in (fm.get("would_decrease_confidence") or [])
                    ][:3],
                    # claim-specific evidence: what was actually observed, and where. Capped so a
                    # long-running assessment cannot bloat the page payload.
                    "evidence": [
                        {
                            "observation": str((e or {}).get("observation") or "")[:400],
                            "source": str((e or {}).get("source") or ""),
                            "confidence": str((e or {}).get("observation_confidence") or ""),
                            "lineage": str((e or {}).get("evidence_lineage") or ""),
                        }
                        for e in (fm.get("adversarial_evidence") or [])[:4]
                        if isinstance(e, dict)
                    ],
                }
            )
        for rows in out.values():
            rows.sort(
                key=lambda row: (row["last_updated"], row["as_of"], row["path"]), reverse=True
            )
        _assessment_subject_cache = (time.monotonic(), out)
        return out


def api_page(path: str = Query(...)):
    """Render any wiki page (entity/source/concept/...) for click-through navigation."""
    if ".." in path or path.startswith("/"):
        raise HTTPException(400, "bad path")
    cand = WIKI / (path + ".md")
    if not cand.is_file():
        name = Path(path).name + ".md"
        hits = [h for d in _content_dirs() for h in d.rglob(name)]
        if len(hits) > 1:
            raise HTTPException(409, "ambiguous page basename; use the full wiki-relative path")
        cand = hits[0] if hits else None
    if not cand:
        raise HTTPException(404, "page not found")
    cp = cand.resolve()
    try:
        cp.relative_to(WIKI.resolve())
    except ValueError:
        raise HTTPException(403, "blocked")
    fm, body = split_fm(cp.read_text(encoding="utf-8", errors="replace"))
    title = fm.get("title") or fm.get("name") or Path(path).name
    ptype = str(fm.get("type") or "")
    profiles = cockpit_config().get("profiles", {})
    m = _meta_panel_items(fm, profiles.get(ptype))
    slug = cp.stem.lower()
    prov = _provenance(fm, body)
    conflicts = _shape_conflicts(fm)
    rel = str(cp.relative_to(WIKI.resolve()))
    quality = _quality_badges(fm, body, ptype, prov, conflicts)
    trust = _page_trust_state(rel, fm, quality)
    subject = rel.removesuffix(".md")
    return {
        "path": path,
        "title": str(title),
        "type": ptype,
        "rel": rel,
        "html": render_md(body),
        # a type the pack gives a `profiles:` order to splits its fields into a primary fact
        # panel (`meta`) vs secondary Record details (`meta_aux`). The body always leads the
        # page; the fact panel follows it (see openPage in static/app.js).
        "profiled": ptype in profiles,
        "panel": _panel_for(fm, body),
        "provenance": prov,
        "meta": m["primary"],
        "meta_aux": m["secondary"],
        "conflicts": conflicts,
        "needs_review": bool(fm.get("needs_review")),
        "review_enabled": _REVIEW_ENABLED,
        "review_auth_mode": _REVIEW_AUTH_MODE,
        "observations": _observations_by_canonical().get(slug, []),
        "assessments": _assessment_subject_index().get(_subject_key(subject), []),
        "citations": _evidence_sources(fm),
        "quality": quality,
        "trust": trust,
    }
