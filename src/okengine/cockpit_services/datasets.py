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
from okengine.actor_identity import actor_identity_error

_FM_SCAN_BYTES = 262_144


def _scan_dir_meta(sub: str) -> list[dict]:
    """Parse frontmatter of every page under wiki/<sub> (recursive, like Dataview FROM). The raw
    scan: on a 3k–6k-file namespace this is seconds of syscalls + YAML parses, so it must never run
    synchronously on a request — _load_dir keeps it behind the cache + a background refresh.
    (Distinct from the browse-layer _scan_dir below, which returns a lighter list-row shape.)"""
    out: list[dict] = []
    base = WIKI / sub
    if base.is_dir():
        for p in base.rglob("*.md"):
            name = p.name
            if name.startswith(("_", ".")) or ".bak." in name or _reserved_seg(p):
                continue
            try:
                fm, body = split_fm(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            fm["_name"] = p.stem
            fm["_sub"] = sub
            # the TRUE path under wiki/<sub> (shards included) — page links must carry it:
            # basename resolution papers over it until two shards collide on a stem
            fm["_rel"] = p.relative_to(base).as_posix()[:-3]
            # Scoped review launchers reuse this namespace cache instead of walking the whole
            # corpus. Preserve the one body-derived review signal while the file is already open.
            fm["_body_review_flag"] = bool(_GROUNDING_REVIEW.search(body or ""))
            if str(fm.get("type") or "").casefold() == "actor":
                title = str(fm.get("title") or fm.get("name") or p.stem)
                fm["_actor_identity_error"] = actor_identity_error(title, body)
            out.append(fm)
    return out


def _refresh_dir_async(sub: str) -> None:
    """Rescan wiki/<sub> in a daemon thread and swap the cache. In-flight guard so N concurrent
    requests to a stale dir spawn ONE rescan, not N."""
    with _DIR_LOCK:
        if sub in _DIR_REFRESHING:
            return
        _DIR_REFRESHING.add(sub)

    def _work():
        try:
            rows = _scan_dir_meta(sub)
            _DIR_CACHE[sub] = (time.monotonic(), rows)
        finally:
            with _DIR_LOCK:
                _DIR_REFRESHING.discard(sub)

    threading.Thread(target=_work, daemon=True).start()


def _load_dir(sub: str) -> list[dict]:
    """Frontmatter of every page under wiki/<sub>, cached, STALE-WHILE-REVALIDATE. A scan of a large
    namespace (entities/sources are thousands of pages) costs seconds; sorting a top-N table needs
    every row, so the scan is inherent. Keep it off the hot path: within _DIR_TTL serve the cache;
    once stale serve the stale copy immediately AND rescan in the background; only a cold miss (no
    cache at all) scans synchronously — and the startup warmer pre-populates the configured datasets
    so even the first request is warm. The vault is :ro and cron-refreshed, so bounded staleness is
    already the contract (previously every request that fell past the TTL blocked on the full scan)."""
    now = time.monotonic()
    hit = _DIR_CACHE.get(sub)
    if hit is not None:
        if now - hit[0] >= _DIR_TTL:
            _refresh_dir_async(sub)  # stale: refresh in the background, serve stale now
        return hit[1]
    rows = _scan_dir_meta(sub)  # cold miss (first ever load of this dir)
    _DIR_CACHE[sub] = (now, rows)
    return rows


def _configured_dataset_dirs(cfg: dict | None = None, *, landing_only: bool = False) -> list[str]:
    """Configured dataset namespaces in analyst-visible order, de-duplicated.

    Tab order is the warm priority. Streams follow tabs because the landing response is the
    readiness boundary and stream endpoints are independently cached. ``landing_only`` deliberately
    excludes streams and every later tab for the synchronous startup pass.
    """
    cfg = cfg or cockpit_config()
    tab_defs = cfg.get("tab_defs") or {}
    tabs = [str(tab) for tab in (cfg.get("tabs") or []) if str(tab)]
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        sub = str(value or "").strip().strip("/")
        if sub and sub not in seen:
            seen.add(sub)
            ordered.append(sub)

    selected_tabs = tabs[:1] if landing_only else tabs
    for key in selected_tabs:
        d = tab_defs.get(key)
        if not isinstance(d, dict):
            continue
        for b in d.get("boxes") or []:
            ds = b.get("dataset") or {}
            if isinstance(ds, dict):
                add(ds.get("dir"))
            if b.get("dir"):
                add(b.get("dir"))
    if not landing_only:
        for stream in cfg.get("streams") or []:
            if isinstance(stream, dict):
                add(stream.get("dir"))
    return ordered


def _warm_tab_datasets() -> None:
    """Synchronously pre-scan all configured namespaces.

    Kept as an explicit operator/test helper. Startup uses the bounded landing-only and paced
    post-ready paths below; it never calls this whole set synchronously.
    """
    for sub in _configured_dataset_dirs():
        try:
            _DIR_CACHE[sub] = (time.monotonic(), _scan_dir_meta(sub))
        except Exception:
            pass


def _warm_initial_tab_datasets() -> None:
    """Synchronously warm only the configured landing tab before the service becomes ready.

    Other namespaces remain lazy and typically cost milliseconds to under a second. Warming every
    tab plus the backlink graph in background threads made a nominally ready Cockpit contend on the
    GIL and disk for tens of seconds on large vaults.
    """
    for sub in _configured_dataset_dirs(landing_only=True):
        try:
            _DIR_CACHE[sub] = (time.monotonic(), _scan_dir_meta(sub))
        except Exception:
            pass


def _requests_active() -> bool:
    with _ACTIVE_REQUESTS_LOCK:
        return _ACTIVE_REQUESTS > 0


def _warm_remaining_tab_datasets() -> None:
    """Warm uncached configured namespaces serially, yielding to interactive traffic."""
    for sub in _configured_dataset_dirs():
        if sub in _DIR_CACHE:
            continue
        while _requests_active():
            time.sleep(_POST_READY_IDLE_POLL)
        started = time.monotonic()
        try:
            rows = _scan_dir_meta(sub)
            _DIR_CACHE[sub] = (time.monotonic(), rows)
            elapsed = time.monotonic() - started
            print(
                f"cockpit: warmed dataset {sub!r} ({len(rows):,} pages in {elapsed:.3f}s)",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            print(f"cockpit: dataset warm failed for {sub!r}: {exc}", file=sys.stderr, flush=True)
        if _POST_READY_WARM_GAP > 0:
            time.sleep(_POST_READY_WARM_GAP)


def _schedule_remaining_tab_warmup() -> None:
    """Start the post-ready warmer once; delay guarantees startup can return readiness first."""
    if getattr(_schedule_remaining_tab_warmup, "_started", False):
        return
    _schedule_remaining_tab_warmup._started = True

    def work():
        if _POST_READY_WARM_DELAY > 0:
            time.sleep(_POST_READY_WARM_DELAY)
        _warm_remaining_tab_datasets()

    threading.Thread(target=work, name="cockpit-tab-warmer", daemon=True).start()


def _disp(fm: dict) -> str:
    explicit = str(fm.get("title") or fm.get("name") or "").strip()
    return explicit or _humanize(str(fm.get("_name") or ""))


def _source_title(row: dict) -> str:
    """Human-readable source title, repairing obvious path/slug leakage at display time."""
    title = str(row.get("title") or row.get("name") or "").strip()
    if title and not re.search(r"\s", title) and ("-" in title or "_" in title):
        title = re.sub(r"[-_](?:md|html?|pdf)$", "", title, flags=re.I)
        return _humanize(title)
    return title or _humanize(str(row.get("_name") or ""))


def _source_publisher(row: dict) -> str:
    """Compact publisher truth, with the article host as a transparent fallback.

    Publisher is an organization/outlet label, not a synopsis. Failed metadata extraction can fold
    a paragraph into this scalar; trusting it makes every source table unreadable. Keep legitimate
    composite labels, but treat prose-sized values as malformed and show the source hostname.
    """
    publisher = str(row.get("publisher") or "").strip()
    placeholder = publisher.casefold().startswith(("unknown", "no publisher", "(no publisher"))
    prose_sized = len(publisher) > 64 or len(publisher.split()) > 10
    if publisher and publisher not in {"—", "-"} and not placeholder and not prose_sized:
        return publisher
    try:
        host = (urlparse(str(row.get("url") or "")).hostname or "").removeprefix("www.")
    except ValueError:
        host = ""
    return host or "Unknown publisher ⚠"


def _page_link(fm: dict) -> str:
    rel = fm.get("_rel") or fm["_name"]  # true sharded path when _load_dir provided it
    return f'<a class="wl" data-page="{_esc(fm["_sub"])}/{_esc(rel)}">{_esc(_disp(fm))}</a>'


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="empty" style="padding:10px;text-align:left">none</div>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)

    def _cell(c: str) -> str:
        cls = ' class="num"' if _NUMISH_CELL.fullmatch(str(c).strip()) else ""
        return f"<td{cls}>{c}</td>"

    body = "".join("<tr>" + "".join(_cell(c) for c in r) + "</tr>" for r in rows)
    return f'<table class="ledger"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "y", "1"))


def api_watchlist():
    wl = cockpit_config()["watchlist"]
    if not wl:  # no watchlist config -> tab is hidden
        return {"sections": [], "counts": {}}
    today = TODAY()
    L = wl["labels"]
    tier_f, rate_f, moved_f = wl["tier_field"], wl["rating_field"], wl["moved_field"]
    sections: list[dict] = []

    ents = _load_dir(wl["entity_dir"])
    if wl["entity_types"]:
        ents = [e for e in ents if str(e.get("type") or "").strip() in wl["entity_types"]]
    tiered = [e for e in ents if e.get(tier_f)]

    # rating × tier matrix (only when a rating field is configured)
    if rate_f:
        matrix: dict[str, dict[str, int]] = {}
        for e in tiered:
            tier = str(e.get(tier_f)).strip()
            r = str(e.get(rate_f) or "").strip().lower()
            b = matrix.setdefault(tier, {"high": 0, "medium": 0, "low": 0, "total": 0})
            if r in b:
                b[r] += 1
            b["total"] += 1
        mrows = [
            [_esc(t), str(b["high"]), str(b["medium"]), str(b["low"]), str(b["total"])]
            for t, b in sorted(matrix.items())
        ]
        sections.append(
            {
                "group": L["section"],
                "title": f"{L['rating']} matrix by {L['tier'].lower()}",
                "html": _html_table([L["tier"], "High", "Medium", "Low", "Total"], mrows),
            }
        )

    def _move_row(e):
        mv = _as_date(e.get(moved_f))
        days = (today - mv).days if mv else None
        cells = [_page_link(e), _esc(e.get(tier_f))]
        if rate_f:
            cells.append(_esc(e.get(rate_f)))
        cells += [
            _esc(mv.isoformat() if mv else "—"),
            (str(days) + "d" if days is not None else "—"),
        ]
        return (cells, mv, days)

    rate_hdr = [L["rating"]] if rate_f else []
    moved = [_move_row(e) for e in tiered]
    recent = sorted(
        [r for r in moved if r[2] is not None and r[2] <= 30], key=lambda r: r[1], reverse=True
    )[:25]
    sections.append(
        {
            "group": L["section"],
            "title": "Recently moved (≤30d)",
            "html": _html_table(
                [L["entity"], L["tier"], *rate_hdr, "Last move", "Days ago"], [r[0] for r in recent]
            ),
        }
    )
    quiet = sorted([r for r in moved if r[2] is not None and r[2] > 60], key=lambda r: r[1])[:15]
    sections.append(
        {
            "group": L["section"],
            "title": "Gone quiet (>60d)",
            "html": _html_table(
                [L["entity"], L["tier"], *rate_hdr, "Last move", "Days quiet"],
                [r[0] for r in quiet],
            ),
        }
    )

    if wl["acquirer_field"]:
        af = wl["acquirer_field"]
        acq = sorted(
            [e for e in ents if _truthy(e.get(af))],
            key=lambda e: str(_as_date(e.get(moved_f)) or ""),
            reverse=True,
        )
        arows = []
        for e in acq:
            row = [_page_link(e), _esc(e.get(tier_f))]
            if rate_f:
                row.append(_esc(e.get(rate_f)))
            row.append(_esc(_as_date(e.get(moved_f)) or "—"))
            arows.append(row)
        sections.append(
            {
                "group": L["section"],
                "title": L["acquirers"],
                "html": _html_table([L["acquirers"], L["tier"], *rate_hdr, "Last move"], arows),
            }
        )

    counts = {"tracked": len(tiered)}

    # --- trends (concepts of a configured type) ---
    tr = wl.get("trends")
    if tr:
        trends = [
            c
            for c in _load_dir(tr["concept_dir"])
            if str(c.get("type") or "").strip() == tr["type"]
        ]

        def _trend_row(c):
            anc = c.get("anchored_predictions")
            anc_n = len(anc) if isinstance(anc, list) else 0
            upd = _as_date(c.get("last_thesis_update"))
            thesis = str(c.get("thesis") or "").strip()
            return [
                _page_link(c),
                _esc(c.get("trend_status")),
                _esc(c.get("thesis_confidence")),
                _esc(thesis[:120] + ("…" if len(thesis) > 120 else "")),
                str(anc_n),
                _esc(upd.isoformat() if upd else "—"),
            ], upd

        thead = ["Trend", "Status", "Conf", "Thesis", "Anchored", "Updated"]
        closed_statuses = ("reversed", "dormant")
        active = [
            c
            for c in trends
            if c.get("trend_status")
            and str(c.get("trend_status")).strip().lower() not in closed_statuses
        ]
        active_rows = sorted(
            [_trend_row(c) for c in active], key=lambda r: str(r[0][2]), reverse=True
        )
        sections.append(
            {
                "group": "Trends",
                "title": "Active trends",
                "html": _html_table(thead, [r[0] for r in active_rows]),
            }
        )
        recent_t = sorted(
            [
                _trend_row(c)
                for c in trends
                if (lambda d: d is not None and (today - d).days <= 30)(
                    _as_date(c.get("last_thesis_update"))
                )
            ],
            key=lambda r: r[1] or datetime.date.min,
            reverse=True,
        )
        sections.append(
            {
                "group": "Trends",
                "title": "Recently updated (≤30d)",
                "html": _html_table(thead, [r[0] for r in recent_t]),
            }
        )
        closed = [
            c for c in trends if str(c.get("trend_status") or "").strip().lower() in closed_statuses
        ]
        sections.append(
            {
                "group": "Trends",
                "title": "Closed (reversed / dormant)",
                "html": _html_table(thead, [_trend_row(c)[0] for c in closed]),
            }
        )
        nostatus = [c for c in trends if not c.get("trend_status")]
        sections.append(
            {
                "group": "Trends",
                "title": "Needs status (no trend_status)",
                "html": _html_table(thead, [_trend_row(c)[0] for c in nostatus]),
            }
        )
        counts["trends"] = len(trends)

    return {"sections": sections, "counts": counts}


def api_competitors():
    out = []
    for view in cockpit_config()["competitors"]:
        rel = view["path"]
        rel = rel if rel.endswith(".md") else rel + ".md"
        p = WIKI / rel
        if not p.is_file():
            continue
        fm, body = split_fm(p.read_text(encoding="utf-8", errors="replace"))
        out.append(
            {
                "key": view["key"],
                "title": fm.get("title") or view["key"],
                "updated": str(fm.get("updated") or "")[:10],
                "html": render_md(body),
            }
        )
    return {"views": out}


def api_home():
    """Analyst home — the daily flow composed from the vault's LIVE surfaces, in triage order:
    latest briefs → what moved (watchlist) → trends → open predictions → knowledge gaps →
    curated dashboards. Sections use the watchlist tab's render contract ({group,title,html});
    an empty/unconfigured surface is OMITTED, so the tab shows only what this deployment
    actually maintains (a raw all-page-links dashboard is a map; this is the route)."""
    cfg = cockpit_config()
    sections: list[dict] = []

    # 1. start here — latest issue of every stream (click → the briefings tab)
    srows = []
    for s in cfg["streams"]:
        dates = _stream_dates(s["key"])
        if dates:
            srows.append(
                [
                    f'<a class="wl" data-stream="{_esc(s["key"])}">{_esc(s["label"])}</a>',
                    _esc(dates[0]),
                    str(len(dates)),
                ]
            )
    if srows:
        sections.append(
            {
                "group": "Start here",
                "title": "Latest briefings & digests",
                "html": _html_table(["Stream", "Latest", "Issues"], srows),
            }
        )

    # 2. what moved — cherry-picked from the watchlist (matrix + movement + active trends)
    if cfg.get("watchlist"):
        keep = ("matrix", "recently moved", "recently updated", "active")
        for s in api_watchlist().get("sections", []):
            t = s["title"].lower()
            if any(k in t for k in keep) and not s["html"].startswith('<div class="empty"'):
                sections.append(
                    {
                        "group": "What moved",
                        "title": f"{s['group']} — {s['title']}",
                        "html": s["html"],
                    }
                )

    # 3. open predictions (top by nearest resolution)
    pr = api_predictions()
    if pr["total"]:
        top = sorted(
            [r for r in pr["rows"] if r["status"] in _OPEN_STATUS],
            key=lambda r: r["resolves_by"] or "9999",
        )[:8]
        prows = [
            [
                f'<a class="wl" data-page="{_esc(r["id"])}">{_esc(r["subject"] or r["id"])}</a>',
                _esc(str(r["confidence"] or "—")),
                _esc(r["resolves_by"] or "—"),
            ]
            for r in top
        ]
        html = (
            f'<p class="home-note">{pr["total"]} tracked · {pr["due_soon"]} due ≤7d · '
            f"{pr['idle']} idle</p>" + _html_table(["Prediction", "Conf", "Resolves by"], prows)
        )
        sections.append({"group": "Predictions", "title": "Open predictions", "html": html})

    # 4. knowledge gaps — lacuna findings ranked by how FLESHED-OUT each is: field density
    #    (surround_density) → confidence → whether the proposed fill is already a testable
    #    prediction. Most-grounded first, so a well-supported gap that's close to actionable
    #    surfaces above a thin/speculative one — the maturity is legible without opening each page.
    gaps = _load_dir("lacuna")
    if gaps:
        _CONF = {"high": 3, "medium": 2, "low": 1}

        def _gap_density(c) -> int:
            mm = re.match(r"\s*(\d+)", str(c.get("surround_density") or ""))
            return int(mm.group(1)) if mm else 0

        ranked = sorted(
            gaps,
            key=lambda c: (
                _gap_density(c),
                _CONF.get(str(c.get("confidence") or "").lower(), 0),
                1 if c.get("prediction_candidate") else 0,
            ),
            reverse=True,
        )[:10]
        grows = [
            [
                _page_link(c),
                (str(_gap_density(c)) if _gap_density(c) else "—"),
                _esc(str(c.get("confidence") or "—")),
                ("✓" if c.get("prediction_candidate") else "—"),
                _esc(str(c.get("created") or "")[:10] or "—"),
            ]
            for c in ranked
        ]
        sections.append(
            {
                "group": "Knowledge gaps",
                "title": "Lacuna findings — most grounded first",
                "html": _html_table(["Gap", "Density", "Confidence", "Testable", "Found"], grows),
            }
        )

    # 5. jump-offs — the pack's CURATED dashboards (not the raw all-pages list). Two config
    # shapes exist: a flat slug list (["top-actors", ...] → dashboards/<slug>) and the grouped
    # form ([{group, items: [{path, title?}]}] — paths already namespace-qualified).
    chips: list[tuple[str, str]] = []  # (page path, label)
    for d in cfg.get("dashboards") or []:
        if isinstance(d, dict):
            for it in d.get("items") or []:
                path = str((it or {}).get("path") or "").strip().strip("/")
                if path:
                    chips.append(
                        (path, str(it.get("title") or path.split("/")[-1].replace("-", " ")))
                    )
        else:
            slug = str(d).strip().strip("/")
            if slug:
                chips.append((f"dashboards/{slug}", slug.replace("-", " ")))
    if chips:
        links = "".join(
            f'<a class="wl home-chip" data-page="{_esc(p)}">{_esc(lbl)}</a>' for p, lbl in chips
        )
        sections.append(
            {
                "group": "Jump off",
                "title": "Curated dashboards",
                "html": f'<div class="home-chips">{links}</div>',
            }
        )

    return {"sections": sections}


def _refine_rows(rows: list[dict], spec: dict) -> list[dict]:
    for f, v in (spec.get("where") or {}).items():
        rows = [r for r in rows if str(r.get(f)) == str(v)]
    for f in spec.get("has") or []:  # field-presence filter (e.g. theme pages vs
        rows = [r for r in rows if r.get(f) not in (None, "", [])]  # shift docs in one dir)
    for f in (
        spec.get("missing") or []
    ):  # field-ABSENCE filter (mirror of `has`) — e.g. unsourced actors
        rows = [r for r in rows if r.get(f) in (None, "", [])]
    for f, values in (spec.get("exclude_values") or {}).items():
        excluded = {
            str(value).casefold() for value in (values if isinstance(values, list) else [values])
        }
        rows = [
            r
            for r in rows
            if not any(str(value).casefold() in excluded for value in _gb_values(r.get(f)))
        ]
    tp = spec.get("today_prefix")  # e.g. published starts with today's date
    if tp:
        # UTC, not the container's LOCAL date: the fields this filters (published/created) are
        # normalized to a full UTC ISO timestamp by the engine's feed pipeline / write path, so a
        # LOCAL-date prefix drops rows for the hours between UTC midnight and local midnight on a
        # non-UTC deployment (okcti TZ=America/New_York — invariant-audit #61).
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        rows = [r for r in rows if str(r.get(tp) or "").startswith(today)]
    window = spec.get("within_hours")
    if isinstance(window, dict):
        field = str(window.get("field") or "").strip()
        try:
            hours = float(window.get("hours"))
        except (TypeError, ValueError):
            hours = 0.0
        if field and hours > 0:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)

            def _inside(row: dict) -> bool:
                raw = str(row.get(field) or "").strip()
                if not raw:
                    return False
                try:
                    stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    return False
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                return stamp.astimezone(datetime.timezone.utc) >= cutoff

            rows = [row for row in rows if _inside(row)]
    return rows


def _ds_rows(spec: dict) -> list[dict]:
    rows = _load_dir(str(spec.get("dir") or "").strip("/"))
    # Tombstones retain their original type so references can resolve, but analyst-facing
    # datasets must never render them as live entities.
    rows = [r for r in rows if str(r.get("status") or "").casefold() != "tombstoned"]
    # Contradictory actor pages remain addressable for repair, but never appear as live actors in
    # analyst datasets.  Evidence/news volume does not override an ontological contradiction.
    rows = [
        r for r in rows
        if not (
            str(r.get("type") or "").casefold() == "actor"
            and (r.get("_actor_identity_error") or r.get("actor_identity_validated") is False)
        )
    ]
    types = spec.get("types") or ([spec["type"]] if spec.get("type") else [])
    if types:
        ts = {str(t) for t in types}
        rows = [r for r in rows if str(r.get("type")) in ts]
    return _refine_rows(rows, spec)


def _configured_rows(owner: dict, dataset: dict | None = None) -> list[dict]:
    """Load a box/item dataset with its declarative filters applied at the same level.

    Pack schemas historically place dataset filters beside
    ``dataset``. Passing only the nested dataset silently ignored those filters in both rendering
    and drill-through. Dataset-local filters remain supported and owner-level values take
    precedence when both are present.
    """
    spec = dict(dataset if dataset is not None else (owner.get("dataset") or {}))
    for key in ("where", "has", "missing", "exclude_values", "today_prefix", "within_hours"):
        if key in owner:
            spec[key] = owner[key]
    return _ds_rows(spec)


def _ds_sorted(rows: list[dict], srt: dict) -> list[dict]:
    f = str(srt.get("field") or "")
    if not f:
        return rows
    if srt.get("require"):
        rows = [r for r in rows if r.get(f) not in (None, "", [])]

    desc = bool(srt.get("desc"))
    then = str(srt.get("then") or "")

    def _sec(r: dict) -> tuple[int, int, float, str]:
        if not then:
            return (0, 0, 0.0, "")  # constant key -> stable order preserved
        value = r.get(then)
        if value in (None, "", []):
            return (0, 0, 0.0, "")  # missing secondary sinks on descending boards
        try:
            return (1, 1, float(value), "")  # preserve existing numeric tie-break behavior
        except (TypeError, ValueError):
            return (1, 0, 0.0, str(value))  # ISO/RFC 3339 and other strings sort lexically

    # Explicit date mode validates before sorting. Legacy placeholders such as `[auto]`, `original`,
    # and prose in date fields must never outrank ISO timestamps merely because `[`/`o` sort after
    # digits. With `require: true`, invalid dates are excluded; otherwise they sink below valid rows.
    if srt.get("date"):
        dated, invalid = [], []
        for row in rows:
            parsed = _as_date(row.get(f))
            (dated if parsed else invalid).append((parsed, row))
        dated.sort(key=lambda item: (item[0], _sec(item[1])), reverse=desc)
        return [row for _date, row in dated] + (
            [] if srt.get("require") else [row for _date, row in invalid]
        )

    # TWO buckets — numeric, then everything-else — each honoring the sort direction WITHIN itself.
    # Two live incidents shaped this:
    #   1. `reverse=bool(desc)` flipped the buckets too, so ONE page with a malformed value (an
    #      agent hand-set `recent_reports:` to a list of source paths) took the #1 slot of the
    #      Most-active table — junk in a NUMERIC sort must rank below every real number.
    #   2. The first fix sorted the non-numeric bucket ascending unconditionally — which broke every
    #      DATE-sorted box (ISO dates aren't floatable, so a date box lives entirely in this bucket):
    #      `sort: {field: created, desc: true}` showed OLDEST gaps first. Direction must apply
    #      within the bucket; ISO date strings/objects order correctly via str().
    # An optional `then:` field breaks ties on the PRIMARY field (same direction). Without it a date
    # box where dozens of rows share one COARSE date (annual-report YYYY-01-01 -> 50+ actors on the
    # "Recently active" board) falls to arbitrary glob order; `then: recent_reports` ranks the most
    # active first WITHIN each date. Default (no `then`) keeps the prior stable-within-tie order.
    nums, others = [], []
    for r in rows:
        v = r.get(f)
        try:
            nums.append((float(v), _sec(r), r))
        except (TypeError, ValueError):
            others.append((str(v), _sec(r), r))
    nums.sort(key=lambda t: (t[0], t[1]), reverse=desc)
    others.sort(key=lambda t: (t[0], t[1]), reverse=desc)
    return [r for _, _, r in nums] + [r for _, _, r in others]
