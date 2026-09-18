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


def _link_page_map(box: dict) -> dict:
    """For a group_by box with `link_page: {dir, by}`, map each bucket value -> `{"path": <page
    path>, "label": <display label>}` for the page in <dir> whose <by> field equals the value. Lets
    an aggregate bar open the entity it NAMES (e.g. an ATT&CK technique id -> its technique page)
    instead of drilling to the members that share it.

    okengine#259: when link_page also declares `label_field`, `label` carries that field from the
    linked page (e.g. `label_field: title` -> "Ingress Tool Transfer") so the bar/chip DISPLAYS the
    human name instead of the opaque id; unset -> "" and the raw group value stays the label. {}
    when link_page not configured; values with no matching page keep the normal group_by drilldown."""
    lp = box.get("link_page")
    if not isinstance(lp, dict) or not lp.get("dir"):
        return {}
    by = str(lp.get("by") or "id")
    lf = str(lp.get("label_field") or "")
    out: dict = {}
    for r in _load_dir(str(lp["dir"])):
        k = r.get(by)
        if k in (None, ""):
            continue
        path = f"{r.get('_sub', '')}/{r.get('_rel') or r.get('_name', '')}".strip("/")
        label = str(r.get(lf) or "").strip() if lf else ""
        out.setdefault(str(k), {"path": path, "label": label})
    return out


def _v_bars(box: dict, rows: list[dict], drill=None) -> str:
    pairs = _ds_pairs(box, rows)
    if not pairs:
        return ""
    mx = max(v for _, v, _, _ in pairs) or 1
    tone = box.get("tone") if box.get("tone") in _TONES else "acc"
    grp = bool(box.get("group_by")) or isinstance(box.get("assessment"), dict)
    lpm = _link_page_map(box) if box.get("group_by") else {}
    out = []
    for l, v, um, key in pairs:
        if grp:
            info = lpm.get(str(key))
            pg = info.get("path") if info else None
            if info and info.get("label"):
                l = info["label"]  # okengine#259: linked page's name, not the id
            dc, da = _drill_attrs(drill, page=pg) if pg else _drill_attrs(drill, value=key)
        else:
            dc, da = _drill_attrs(drill, page=key)
        out.append(
            f'<div class="brow{" um" if um else ""}{dc}"{da}>'
            f'<span class="bl">{_esc(l)}{_UM_FLAG if um else ""}</span>'
            f'<span class="btrk"><i class="bfill t-{tone}" style="width:{100 * v / mx:.0f}%"></i></span>'
            f'<span class="bnum">{v:,}</span></div>'
        )
    rendered = "".join(out)
    if isinstance(box.get("assessment"), dict):
        rendered += _assessment_aggregate_legend()
    return rendered


def _v_chips(box: dict, rows: list[dict], drill=None) -> str:
    pairs = _ds_pairs(box, rows)
    if not pairs:
        return ""
    grp = bool(box.get("group_by")) or isinstance(box.get("assessment"), dict)
    lpm = _link_page_map(box) if box.get("group_by") else {}
    out = []
    for l, v, um, key in pairs:
        if grp:
            info = lpm.get(str(key))
            pg = info.get("path") if info else None
            if info and info.get("label"):
                l = info["label"]  # okengine#259: linked page's name, not the id
            dc, da = _drill_attrs(drill, page=pg) if pg else _drill_attrs(drill, value=key)
        else:
            dc, da = _drill_attrs(drill, page=key)
        out.append(
            f'<span class="dchip{" um" if um else ""}{dc}"{da}>'
            f"{_esc(l)}{_UM_FLAG if um else ''} <b>{v:,}</b></span>"
        )
    rendered = '<div class="dchips">' + "".join(out) + "</div>"
    if isinstance(box.get("assessment"), dict):
        rendered += _assessment_aggregate_legend()
    return rendered


def _assessment_aggregate_legend() -> str:
    return (
        '<p class="assessment-legend"><span aria-hidden="true">◇</span> Assessment-backed rollup. '
        "Values count the newest current judgment per subject; disputed, inconclusive, unknown, and "
        "not-assessed subjects remain separate. Confidence is the mean of contributing judgments; "
        "⚠ marks judgments awaiting human review. "
        '<a class="wl" data-page="assessments/_about">How assessments work</a>.</p>'
    )


def _v_bignums(box: dict, rows: list[dict], drill=None) -> str:
    out = []
    for i, it in enumerate(box.get("items") or []):
        if not isinstance(it, dict):
            continue
        rs = _configured_rows(it) if it.get("dataset") else _refine_rows(rows, it)
        if it.get("stat") == "top" and it.get("group_by"):
            cnt = Counter(str(r.get(it["group_by"])) for r in rs if r.get(it["group_by"]))
            val = cnt.most_common(1)[0][0] if cnt else "—"
        else:
            val = f"{len(rs):,}"
        tone = it.get("tone")
        cls = f" t-{tone}" if tone in _TONES else ""
        dc, da = _drill_attrs(drill, item=i)
        out.append(
            f'<div class="bn-item{dc}"{da}><div class="bn-v{cls}">{_esc(val)}</div>'
            f'<div class="bn-l">{_esc(str(it.get("label") or ""))}</div></div>'
        )
    return f'<div class="bignums">{"".join(out)}</div>' if out else ""


def _v_cards(box: dict, rows: list[dict]) -> str:
    """Trend-style cards: name + direction glyph + status chip + per-bucket mini bars."""
    tf = str(box.get("title_field") or "title")
    df = str(box.get("dir_field") or "direction")
    sf = str(box.get("status_field") or "trend_status")
    series = str(box.get("series_field") or "count_by_year")
    # Trend vocab varies by generator (up/down/flat/emerging vs rising/falling/steady). Cover both so
    # a card shows a DIRECTION glyph, not the default → for every value (the glyph map only knew
    # rising/falling, but theme_trends writes up/down/flat/emerging — so all arrows were →).
    glyph = {
        "up": ("▲", "ok"),
        "rising": ("▲", "ok"),
        "down": ("▼", "crit"),
        "falling": ("▼", "crit"),
        "emerging": ("◆", "acc"),
        "flat": ("→", "mut"),
        "steady": ("→", "mut"),
    }
    cards = []
    for r in rows[: _box_limit(box, "cards")]:
        comparison = str(r.get("comparison") or box.get("comparison") or "full-period")
        if comparison == "partial-period":
            g, gc, direction = "◒", "warn", "partial period"
        else:
            g, gc = glyph.get(str(r.get(df)), ("→", "mut"))
            direction = str(r.get(df) or "—")
        series_name = (
            "count_ytd_by_year"
            if comparison == "ytd" and isinstance(r.get("count_ytd_by_year"), dict)
            else series
        )
        counts = r.get(series_name) if isinstance(r.get(series_name), dict) else {}
        mini = ""
        if counts:
            try:
                mx = max(int(v) for v in counts.values()) or 1
                mini = (
                    '<div class="dmini">'
                    + "".join(
                        f'<i style="height:{max(3, 26 * int(v) / mx):.0f}px" title="{_esc(str(y))}: {_esc(str(v))}"></i>'
                        for y, v in sorted(counts.items())
                    )
                    + "</div>"
                )
            except (TypeError, ValueError):
                mini = ""
        name = str(r.get(tf) or r.get("name") or r.get("_name") or "?")
        cutoff = str(r.get("comparison_as_of") or "").strip()
        period = (
            f"YTD through {_esc(cutoff)}"
            if comparison == "ytd" and cutoff
            else "partial period · direction suppressed"
            if comparison == "partial-period"
            else "full-period comparison"
        )
        cards.append(
            f'<div class="dcard"><div class="dc-n">{_page_link(r) if box.get("link") else _esc(name)}</div>'
            f'<div class="dc-m"><span class="t-{gc}" title="comparison: {_esc(comparison)}">'
            f"{g} {_esc(direction)}</span>"
            f'<span class="dchip">{_esc(str(r.get(sf) or "—"))}</span></div>'
            f'<div class="dc-period">{period}</div>{mini}</div>'
        )
    return f'<div class="dcards">{"".join(cards)}</div>' if cards else ""


def _v_coverage(box: dict, rows: list[dict], drill=None) -> str:
    """Join coverage: this dataset's `list_field` values vs a `versus` dataset's key field,
    grouped by the versus dataset's group field (e.g. detections' covers_techniques vs
    techniques' attack_id, grouped by tactic) — covered/total ratio bars, health-toned."""
    lf = str(box.get("list_field") or "")
    vs = box.get("versus") or {}
    key_f = str(vs.get("key") or "")
    grp_f = str(vs.get("group_by") or "")
    if not (lf and key_f and grp_f):
        return ""
    covered = set()
    for r in rows:
        for t in r.get(lf) or []:
            covered.add(str(t))
    cov: dict = {}
    for t in _ds_rows(vs):
        tid = str(t.get(key_f) or "")
        tac = t.get(grp_f)
        for x in tac if isinstance(tac, list) else [tac]:
            if x:
                c = cov.setdefault(str(x), [0, 0])
                c[1] += 1
                if tid in covered:
                    c[0] += 1
    ranked = sorted(cov.items(), key=lambda kv: -kv[1][1])[: _box_limit(box, "coverage")]
    out = []
    for grp, (cvd, tot) in ranked:
        pct = 100 * cvd / tot if tot else 0
        tone = "ok" if pct >= 50 else "warn" if pct >= 30 else "crit"
        dc, da = _drill_attrs(drill, value=grp)
        out.append(
            f'<div class="brow{dc}"{da}><span class="bl">{_esc(grp)}</span>'
            f'<span class="btrk"><i class="bfill t-{tone}" style="width:{pct:.0f}%"></i></span>'
            f'<span class="bnum">{cvd}/{tot}</span></div>'
        )
    return "".join(out)
