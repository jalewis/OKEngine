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


def _reader_panels() -> dict:
    """Type-bound panel bindings staged by the deploy (okengine#160): VAULT/.okengine/
    reader-panels.json = {page_type: {kind, fields, ...}}. Cached briefly (refreshes on deploy)."""
    now = time.time()
    if _RPANELS_CACHE[1] is None or now - _RPANELS_CACHE[0] > 60:
        try:
            _RPANELS_CACHE[1] = json.loads((VAULT / ".okengine" / "reader-panels.json").read_text())
        except Exception:
            _RPANELS_CACHE[1] = {}
        _RPANELS_CACHE[0] = now
    return _RPANELS_CACHE[1] or {}


def _panel_for(fm: dict, body: str = "") -> dict | None:
    """The panel to render for a page. A GENERATED page self-declares `panel:` (e.g. viz's two-axis
    map, nodes included). Otherwise a type-bound `fields` panel is built from the staged bindings by
    pulling the declared frontmatter field values. Returns a render-ready dict or None."""
    p = fm.get("panel")
    if isinstance(p, dict) and p.get("kind"):
        # a body carrying the server-rendered chart (viz panel-svg block) supersedes the
        # client two-axis renderer — suppress to avoid a double chart. `fields` panels
        # have no embedded form and always render client-side.
        if p.get("kind") == "two-axis" and "<!-- panel-svg" in (body or ""):
            return None
        return p  # self-declared (carries its own data)
    b = _reader_panels().get(str(fm.get("type") or ""))
    if isinstance(b, dict) and b.get("kind") == "fields":
        items = [
            {"label": f, "value": fm.get(f)}
            for f in (b.get("fields") or [])
            if fm.get(f) is not None
        ]
        return (
            {"kind": "fields", "title": b.get("title") or "Details", "items": items}
            if items
            else None
        )
    return None


def _provenance(fm: dict, body: str) -> dict:
    """Trust strip for the page overlay (ported from the reader's provenance view, extended). Answers
    "can I trust this?" from fields the trust lanes + write path already stamp: source coverage
    (cited source PAGES vs total refs), the Tier-2 grounding-check tally, human sign-off, handling
    markers (tlp/sensitivity), Admiralty grading (reliability/credibility), and composition
    provenance (maintained_by/discovered_by). Returns {} for a plain page with no trust signals so
    the overlay shows no empty strip."""
    srcs = fm.get("sources")
    srcs = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
    # a cited SOURCE PAGE (vault-internal) vs a bare external URL — a URL also contains "/", so
    # exclude an http(s) scheme (tighter than the reader's port, which double-counted URLs as pages).
    page_srcs = sum(
        1
        for s in srcs
        if not str(s).lower().startswith(("http://", "https://"))
        and ("/" in str(s) or str(s).lower().endswith(".md"))
    )
    # A PROSE source that the pack's Admiralty `source_registry` grades is still evidence — it is
    # exactly what review_autoverify publishes on (1xA or 2xB). Counting only source PAGES here made
    # this surface disagree with the engine's own evidence policy and quarantined 861 of the 1120
    # actor pages that lane had auto-verified (okengine#563). Key resolution mirrors
    # review_autoverify._registry: strip the key, grade is truthiness-only here.
    _reg = _source_reliability()
    graded_srcs = sum(1 for s in srcs if _reg.get(str(s).strip())) if _reg else 0
    # Sources that COULD carry a grade: a publisher label, i.e. neither a source page nor a bare URL
    # (registry keys are publisher names, so a URL is definitively ungrounded whether or not the
    # registry loaded). Only these make an absent registry genuinely undetectable rather than a fact.
    gradeable_srcs = sum(
        1
        for s in srcs
        if not str(s).lower().startswith(("http://", "https://"))
        and not ("/" in str(s) or str(s).lower().endswith(".md"))
    )
    leads = fm.get("candidate_evidence")
    leads = leads if isinstance(leads, list) else []
    candidate_refs = [
        str(row.get("artifact") or "")
        for row in leads
        if isinstance(row, dict) and row.get("evidence_role") == "candidate-lead"
    ]
    candidate_pages = sum(1 for ref in candidate_refs if _ref_target(ref))
    grounding = None
    g = re.search(r"##\s+Grounding check(.*?)(?:\n##\s|\Z)", body, re.S | re.I)
    if g:
        seg = g.group(1)
        grounding = {
            "supported": len(re.findall(r"\*\*\s*supported", seg, re.I)),
            "unsupported": len(
                re.findall(r"\*\*\s*(?:unsupported|not[- ]found|contradict)", seg, re.I)
            ),
        }

    def _v(k):  # normalize a fm value to a display string, or None
        x = fm.get(k)
        if x in (None, "", [], {}):
            return None
        return ", ".join(str(i) for i in x) if isinstance(x, list) else str(x)

    prov = {
        "sources": len(srcs),
        "source_pages": page_srcs,
        "graded_sources": graded_srcs,
        "gradeable_sources": gradeable_srcs,
        "registry_available": bool(_reg),
        "candidate_leads": len(candidate_refs),
        "candidate_pages": candidate_pages,
        "grounding": grounding,
        "needs_review": bool(fm.get("needs_review")),
        "reviewed_by": _v("reviewed_by"),
        "reviewed_on": _v("reviewed_on"),
        "tlp": _v("tlp"),
        "sensitivity": _v("sensitivity"),
        "reliability": _v("reliability"),
        "credibility": _v("credibility"),
        "maintained_by": _v("maintained_by"),
        "discovered_by": _v("discovered_by"),
    }
    has_signal = (
        prov["sources"]
        or prov["candidate_leads"]
        or grounding
        or prov["needs_review"]
        or prov["reviewed_by"]
        or prov["tlp"]
        or prov["sensitivity"]
        or prov["reliability"]
        or prov["credibility"]
        or prov["maintained_by"]
        or prov["discovered_by"]
    )
    return prov if has_signal else {}


def _source_reliability() -> dict:
    """{source -> Admiralty reliability A–F} from the pack's schema.yaml `source_registry`, so the
    conflict view can label each claim. Domain-agnostic; cached (vault is :ro)."""
    global _SRC_REL_CACHE
    now = time.monotonic()
    if now - _SRC_REL_CACHE[0] < _DIR_TTL:
        return _SRC_REL_CACHE[1]
    out: dict = {}
    sp = _governing_schema_path()
    if sp.is_file():
        try:
            reg = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get(
                "source_registry"
            ) or {}
            for k, v in reg.items() if isinstance(reg, dict) else []:
                r = str((v or {}).get("reliability") or "").strip()
                if r:
                    out[str(k)] = r
        except Exception:
            pass
    _SRC_REL_CACHE = (now, out)
    return out


def _meta_compact_dict(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items() if v not in (None, "", [], {}))


def _val_text(v) -> str:
    return _meta_compact_dict(v) if isinstance(v, dict) else str(v)


def _url_label(url: str) -> str:
    """Friendly link text for a bare URL — its host minus 'www.' (e.g. attack.mitre.org)."""
    try:
        host = urlparse(url).netloc
    except Exception:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    return host or url


def _ref_target(s: str) -> str | None:
    """If `s` is a wiki-relative path that resolves to a vault page, return its canonical key (no
    `.md`) for an internal link; else None. Path-shaped only; basename fallback resolves a flat-form
    ref to a sharded page (entities/foo -> entities/f/foo)."""
    if not isinstance(s, str):
        return None
    key = s.strip()
    if "/" not in key or "://" in key or " " in key:
        return None
    key = key[:-3] if key.endswith(".md") else key
    if not WIKI.is_dir():
        return None
    try:
        cand = (WIKI / (key + ".md")).resolve()
        if cand.is_file() and _within(WIKI, cand):
            return key
        hits = [p for p in WIKI.rglob(Path(key).name + ".md") if not _skip(p.name)]
        if len(hits) == 1:
            return str(hits[0].resolve().relative_to(WIKI.resolve()))[:-3]
    except OSError:
        pass
    return None


def _id_index() -> dict:
    """Cached ``{bare id-or-slug -> {"page": key, "label": display}}`` over the reference namespaces
    (_ID_INDEX_NS), so a bare-id frontmatter ref that _ref_target can't linkify (it's path-shaped
    only) still becomes a page link in the overlay — e.g. a bare `[G0022, shinyhunters]` ref -> the
    entity pages. Keyed by BOTH the page `id` field and its stem/slug (a lane may stamp either — the
    id where present, else the slug). `label` is the page TITLE when the
    token is an OPAQUE id (differs from the slug — G0022 -> "Sandworm Team"), else the token itself
    (readable slugs and self-describing ids like CVE-2026-… stay as written). A token owned by >1 page
    is dropped — no guessed link. TTL-cached; -inf sentinel so a fresh host never serves an empty index
    as valid (the fresh-host cache trap)."""
    global _ID_INDEX_CACHE
    now = time.monotonic()
    if now - _ID_INDEX_CACHE[0] < _DIR_TTL:
        return _ID_INDEX_CACHE[1]
    idx: dict = {}
    dup: set = set()
    for sub in _ID_INDEX_NS:
        for r in _load_dir(sub):
            stem = str(r.get("_name") or "")
            key = f"{r.get('_sub', '')}/{r.get('_rel') or stem}".strip("/")
            title = str(r.get("title") or stem)
            for tok, is_id in ((r.get("id"), True), (stem, False)):
                if not isinstance(tok, str) or not tok.strip():
                    continue
                t = tok.strip()
                if t in dup:
                    continue
                if t in idx and idx[t]["page"] != key:
                    del idx[t]  # same token, different pages -> ambiguous
                    dup.add(t)
                    continue
                label = (
                    title if (is_id and t != stem) else t
                )  # opaque id -> name; slug/self-id -> keep
                idx[t] = {"page": key, "label": label}
    _ID_INDEX_CACHE = (now, idx)
    return idx


def _meta_values(v) -> list[dict]:
    """One frontmatter value -> display chips. http(s) scalars + url/href list items become external
    links; a value resolving to a vault page (path-shaped ref, else a bare id via _id_index) becomes
    an internal page link; dicts compact to k=v."""
    out: list[dict] = []
    for el in v if isinstance(v, list) else [v]:
        if isinstance(el, dict):
            url = el.get("url") or el.get("href")
            txt = (
                el.get("id")
                or el.get("value")
                or el.get("name")
                or el.get("std")
                or (_url_label(url) if url else None)
                or _meta_compact_dict(el)
            )
            out.append({"text": str(txt), "url": str(url)} if url else {"text": str(txt)})
        else:
            s = str(el)
            if s.startswith(("http://", "https://")):
                out.append({"text": _url_label(s), "url": s})
            else:
                tgt = _ref_target(s)
                if tgt:
                    out.append({"text": s, "page": tgt})
                else:
                    hit = _id_index().get(
                        s.strip()
                    )  # bare id/slug -> its page (exploiting_actors etc.)
                    out.append({"text": hit["label"], "page": hit["page"]} if hit else {"text": s})
    return out


def _meta_panel_items(fm: dict, order: list | None = None) -> dict:
    """Frontmatter split into `primary` (the page's intel — surfaced) and `secondary`
    (record-keeping — collapsed). Renders whatever fields exist. When the pack supplies a per-type
    `order`, that order IS the profile: only its fields are primary (in order); everything else
    (record-keeping — ids, urls, dates, provenance the pack didn't put in the profile) drops to
    secondary, so the top reads as a curated analyst card, not a field dump. Without an order, the
    split falls back to the `_META_SECONDARY` heuristic."""
    primary: list[dict] = []
    secondary: list[dict] = []
    if not isinstance(fm, dict):
        return {"primary": primary, "secondary": secondary}
    keys = list(fm.keys())
    rank = {f: i for i, f in enumerate(order)} if order else {}
    if order:
        keys.sort(
            key=lambda k: rank.get(k, len(order))
        )  # stable: declared first, rest keep fm order
    for k in keys:
        v = fm.get(k)
        if k in _META_PANEL_SKIP or v is None or v == "" or v == [] or v == {}:
            continue
        label = str(k).replace("_", " ").replace("-", " ").strip()
        item = {"label": label[:1].upper() + label[1:], "values": _meta_values(v)}
        if order:
            is_secondary = k not in rank  # profiled: only declared fields are the profile
        else:
            is_secondary = k in _META_SECONDARY  # unprofiled: heuristic record-keeping set
        (secondary if is_secondary else primary).append(item)
    return {"primary": primary, "secondary": secondary}


def _shape_conflicts(fm: dict) -> list[dict]:
    """The assembler's `conflicts:` frontmatter -> per-field 'what each source says', each value
    tagged with its source(s) + Admiralty reliability + rank (for the ≥B filter), headline flagged."""
    rel = _source_reliability()
    out: list[dict] = []
    conflicts = fm.get("conflicts")
    if not isinstance(conflicts, list):  # `conflicts: 42` -> `for c in 42` TypeError (M28)
        conflicts = []
    for c in conflicts:
        if not isinstance(c, dict):
            continue
        headline = c.get("headline")
        vals: list[dict] = []
        values = c.get("values")  # guard the container (`values: 42` = non-iterable scalar)
        if not isinstance(
            values, list
        ):  # AND each entry below — see reader's copy (invariant-audit M28)
            values = []
        for v in values:
            if not isinstance(v, dict):  # scalar entry -> .get() AttributeError 500s the page
                continue
            v_sources = v.get("sources")  # third container: `sources: 42` -> for s in 42 (M28)
            if not isinstance(v_sources, list):
                v_sources = []
            srcs = []
            for source in v_sources:
                grade = rel.get(str(source), "")
                normalized = str(grade).strip().upper()
                rank_key = normalized
                if normalized not in _REL_RANK and re.fullmatch(r"[A-F][1-6]", normalized):
                    rank_key = normalized[0]
                recognized = None if not normalized else rank_key in _REL_RANK
                srcs.append({
                    "name": str(source),
                    "reliability": grade,
                    "reliability_recognized": recognized,
                    "reliability_rank_key": rank_key,
                })
            rank = max(
                (_REL_RANK[s["reliability_rank_key"]] for s in srcs
                 if s["reliability_recognized"] is True), default=-1
            )
            vals.append(
                {
                    "value": _val_text(v.get("value")),
                    "sources": srcs,
                    "rank": rank,
                    "rank_known": any(s["reliability_recognized"] is True for s in srcs),
                    "reliability_oov": any(s["reliability_recognized"] is False for s in srcs),
                    "is_headline": v.get("value") == headline,
                }
            )
        out.append(
            {"field": str(c.get("field") or ""), "headline": _val_text(headline), "values": vals}
        )
    return out


def _evidence_sources(fm: dict) -> list[dict]:
    """A page's cited `sources:` as graded evidence rows: name, internal page (if it resolves),
    Admiralty reliability (from schema.yaml source_registry), and recency (the source page's date,
    when it's a page). Turns a bare source list into dated, graded citations. Reliability/date are
    "" when the deployment doesn't populate a registry or the source is a prose name."""
    srcs = fm.get("sources")
    srcs = srcs if isinstance(srcs, list) else ([srcs] if srcs else [])
    rel = _source_reliability()
    out: list[dict] = []
    for s in srcs:
        name = str(s).strip()
        if not name:
            continue
        page = _ref_target(name)
        date = ""
        page_reliability = ""
        if page:
            pfm, _ = split_fm(_read_head(WIKI / (page + ".md")))
            date = str(
                pfm.get("published")
                or pfm.get("date")
                or pfm.get("updated")
                or pfm.get("last_updated")
                or ""
            )[:10]
            # A citation is a concrete source record. Its reviewed reliability grade is more
            # specific than a registry default keyed by an importer/feed name.
            page_reliability = str(pfm.get("reliability") or "")
        out.append(
            {
                "name": name,
                "page": page,
                "reliability": page_reliability or rel.get(name, ""),
                "date": date,
            }
        )
    return out


def _observations_by_canonical() -> dict:
    """{canonical-slug -> [{source, key}]} over `observations/`, for canonical→source drill-down.
    Cached for _DIR_TTL (head-read only)."""
    global _OBS_INDEX_CACHE
    now = time.monotonic()
    if now - _OBS_INDEX_CACHE[0] < _DIR_TTL:
        return _OBS_INDEX_CACHE[1]
    idx: dict = {}
    base = WIKI / "observations"
    if base.is_dir():
        for p in base.rglob("*.md"):
            if _skip(p.name) or _reserved_seg(
                p
            ):  # skip _archive/ retired observations (batch-2 re-verify)
                continue
            fm, _ = split_fm(_read_head(p))
            canon = str(fm.get("canonical") or "").strip().lower()
            if canon:
                key = str(p.resolve().relative_to(WIKI.resolve()))[:-3]
                idx.setdefault(canon, []).append(
                    {"source": str(fm.get("source") or ""), "key": key}
                )
    _OBS_INDEX_CACHE = (now, idx)
    return idx


def _type_required_fields() -> dict:
    """{type -> [required field names]} from schema.yaml `types`, so a page missing a field its type
    requires can be flagged. 'type' itself is always present -> dropped. Cached (vault :ro)."""
    global _TYPE_REQ_CACHE
    now = time.monotonic()
    if now - _TYPE_REQ_CACHE[0] < _DIR_TTL:
        return _TYPE_REQ_CACHE[1]
    out: dict = {}
    sp = _governing_schema_path()
    if sp.is_file():
        try:
            types = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("types") or {}
            for k, v in types.items() if isinstance(types, dict) else []:
                req = (v or {}).get("required") if isinstance(v, dict) else None
                if isinstance(req, list):
                    out[str(k)] = [str(f) for f in req if str(f) != "type"]
        except Exception:
            pass
    _TYPE_REQ_CACHE = (now, out)
    return out


def _quality_badges(fm: dict, body: str, ptype: str, prov: dict, conflicts: list) -> list[dict]:
    """Generic page-health badges from data already present — a clean page gets no row.
    level: bad (red) | warn (amber) | info (neutral, non-blocking context — see `blocking` in
    _trust_state). Each carries a `title` tooltip."""
    b: list[dict] = []
    prov = prov or {}
    # required fields the schema declares for this type
    missing = [
        f for f in _type_required_fields().get(ptype or "", []) if fm.get(f) in (None, "", [], {})
    ]
    if missing:
        b.append(
            {
                "label": f"missing {', '.join(missing[:3])}",
                "level": "bad",
                "title": f"required field(s) absent for type '{ptype}': {', '.join(missing)}",
            }
        )
    # Knowledge pages must cite source records. A source page IS the evidence record: its
    # upstream URL/raw capture is its grounding, so requiring it to cite another source page
    # produces the nonsensical "no sources" badge on properly captured articles.
    nsrc = prov.get("sources", 0)
    source_record_grounded = ptype == "source" and bool(fm.get("url") or fm.get("raw"))
    candidate_leads = int(prov.get("candidate_leads") or 0)
    if not source_record_grounded:
        if not nsrc and candidate_leads:
            b.append(
                {
                    "label": f"{candidate_leads} candidate lead{'s' if candidate_leads != 1 else ''}",
                    "level": "warn",
                    "title": "collection leads informed prioritization but do not support an assessment",
                }
            )
        elif not nsrc:
            b.append({"label": "no sources", "level": "bad", "title": "no sources cited"})
        elif not prov.get("source_pages"):
            ngraded = int(prov.get("graded_sources") or 0)
            if ngraded:
                # graded prose = evidence, not a defect: informational, deliberately NOT in `blocking`
                b.append(
                    {
                        "label": f"{ngraded} graded source{'s' if ngraded != 1 else ''}",
                        "level": "info",
                        "title": f"{ngraded} of {nsrc} prose source(s) carry an Admiralty grade from "
                        f"source_registry — no source page to link, but the publisher is known",
                    }
                )
            elif not prov.get("registry_available") and prov.get("gradeable_sources"):
                # an empty registry cannot distinguish graded from ungraded. Blocking every page on it
                # would be a vacuous FAIL, so say so plainly instead of implying the page is unsourced.
                b.append(
                    {
                        "label": "grounding unverifiable",
                        "level": "warn",
                        "title": "no source_registry in the governing schema — grounding is "
                        "UNDETECTABLE here, not a pass and not a failure",
                    }
                )
            else:
                b.append(
                    {
                        "label": "ungrounded",
                        "level": "warn",
                        "title": f"{nsrc} prose source(s) — none link to a source page and none "
                        f"carry an Admiralty grade",
                    }
                )
    g = prov.get("grounding")
    if g and g.get("unsupported"):
        n = g["unsupported"]
        b.append(
            {
                "label": f"{n} unsupported claim{'s' if n != 1 else ''}",
                "level": "bad",
                "title": "the Grounding check flagged unsupported claims",
            }
        )
    if fm.get("needs_review"):
        b.append({"label": "needs review", "level": "warn", "title": "flagged for human review"})
    if conflicts:
        n = len(conflicts)
        b.append(
            {
                "label": f"{n} conflicting field{'s' if n != 1 else ''}",
                "level": "warn",
                "title": "sources disagree on one or more fields",
            }
        )
    if _STALE_DAYS:
        d = _as_date(fm.get("updated") or fm.get("last_updated") or fm.get("last_seen"))
        if d is not None:
            age = (TODAY() - d).days
            if age > _STALE_DAYS:
                b.append(
                    {
                        "label": f"stale {age}d",
                        "level": "warn",
                        "title": f"last updated {age} days ago (> {_STALE_DAYS}d)",
                    }
                )
    prose = _strip_md(body or "").strip()
    nfields = sum(
        1 for k, v in fm.items() if k not in _META_PANEL_SKIP and v not in (None, "", [], {})
    )
    if len(prose) < _THIN_CHARS and nfields < 4:
        b.append(
            {
                "label": "thin",
                "level": "warn",
                "title": f"sparse page (<{_THIN_CHARS} chars, few fields)",
            }
        )
    return b


def _page_trust_state(rel: str, fm: dict, quality: list[dict]) -> dict:
    """Keep unresolved entity records from presenting as verified canonical profiles."""
    status = str(fm.get("status") or "").strip().lower()
    redirect = str(fm.get("redirect_to") or fm.get("superseded_by") or "").strip()
    if status == "tombstoned":
        return {
            "state": "retired",
            "reasons": ["superseded record"],
            "redirect_to": redirect or None,
        }
    if not rel.startswith("entities/"):
        return {"state": "normal", "reasons": [], "redirect_to": None}
    blocking = {"no sources", "ungrounded", "needs review"}
    reasons = [
        str(b.get("label"))
        for b in quality
        if b.get("level") == "bad"
        or b.get("label") in blocking
        or str(b.get("label") or "").startswith(("missing ", "unsupported", "conflicting"))
    ]
    return {
        "state": "quarantined" if reasons else "verified",
        "reasons": reasons,
        "redirect_to": None,
    }


def _review_reasons(fm: dict, body: str) -> list[dict]:
    reasons = []
    conflicts = fm.get("conflicts") if isinstance(fm.get("conflicts"), list) else []
    for conflict in conflicts:
        if isinstance(conflict, dict):
            reasons.append(
                {
                    "code": "conflict",
                    "field": str(conflict.get("field") or ""),
                    "detail": "sources disagree on this field",
                }
            )
    if _GROUNDING_REVIEW.search(body or ""):
        reasons.append(
            {"code": "grounding", "detail": "grounding check flagged an unsupported claim"}
        )
    if fm.get("needs_review") is True and not reasons:
        reasons.append({"code": "legacy-unspecified", "detail": "legacy needs_review flag"})
    if str(fm.get("type") or "") in _review_required_types() and not reasons:
        reasons.append(
            {
                "code": "import-unvetted",
                "detail": f"{fm.get('type')} requires human review under pack policy",
            }
        )
    return reasons


def _legacy_review_current(fm: dict) -> bool:
    """Compatibility for pre-ledger sign-offs; new writes invalidate their projection."""
    reviewed = str(fm.get("reviewed_on") or "")[:10]
    updated = str(fm.get("last_updated") or fm.get("updated") or fm.get("created") or "")[:10]
    return bool(reviewed and updated and reviewed >= updated)


def _requires_review(fm: dict, body: str) -> bool:
    if fm.get("needs_review") is True or _GROUNDING_REVIEW.search(body or ""):
        return True
    if str(fm.get("type") or "") in _review_required_types():
        return not (str(fm.get("review_state") or "") == "approved" or _legacy_review_current(fm))
    return False


def _review_records() -> list[dict]:
    base = WIKI / "operational" / "reviews"
    out = []
    if not base.is_dir():
        return out
    for p in base.glob("*.yaml"):  # glob-ok: operational/reviews/ is a flat yaml dir (unsharded)
        try:
            rec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _review_record_for(subject: str, version: int, digest: str) -> dict | None:
    matches = [r for r in _review_records() if r.get("subject") == subject]
    exact = [
        r
        for r in matches
        if int(r.get("subject_version") or -1) == version and r.get("subject_hash") == digest
    ]
    rows = exact or matches
    rows.sort(key=lambda r: str(r.get("requested_at") or ""), reverse=True)
    return rows[0] if rows else None


def _review_decision_context(fm: dict) -> dict:
    """Project a page's bounded proposition into explicit, non-overclaiming review semantics."""
    ptype = str(fm.get("type") or "record")
    noun = "assessment" if ptype in {"assessment", "actor-assessment"} else "record"
    proposition = str(
        fm.get("review_proposition")
        or fm.get("claim")
        or fm.get("title")
        or fm.get("name")
        or "this record"
    )
    return {
        "question": str(
            fm.get("question") or "Is the current version supported by its cited evidence?"
        ),
        "proposition": proposition,
        "scope": str(
            fm.get("review_scope")
            or f"Decide whether the cited evidence supports this {noun} as written. Approval does not expand the claim beyond its stated scope or confidence."
        ),
        "approve": str(
            fm.get("review_approve_meaning")
            or f"The evidence supports this {noun} as written at its stated scope and confidence."
        ),
        "reject": str(
            fm.get("review_reject_meaning")
            or f"The evidence does not support this {noun} as written. Rejection does not prove the opposite proposition."
        ),
        "request_changes": str(
            fm.get("review_change_meaning")
            or "The proposition, evidence, scope, or confidence must be corrected before a decision."
        ),
        "defer": "More evidence or analysis is required before deciding.",
        "dismiss": "This review item is duplicate, out of scope, or not applicable.",
        "noun": noun,
    }


def _review_detail(cand: Path) -> dict:
    text = cand.read_text(encoding="utf-8", errors="replace")
    fm, body = split_fm(text)
    subject = cand.resolve().relative_to(WIKI.resolve()).as_posix()[:-3]
    try:
        version = int(fm.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = _review_record_for(subject, version, digest)
    current_record = (
        record
        if (
            record
            and int(record.get("subject_version") or -1) == version
            and record.get("subject_hash") == digest
        )
        else None
    )
    evidence = _evidence_sources(fm)
    return {
        "subject": subject,
        "title": str(fm.get("title") or fm.get("name") or cand.stem),
        "type": str(fm.get("type") or ""),
        "version": version,
        "hash": digest,
        "needs_review": bool(fm.get("needs_review")),
        "state": (current_record or {}).get("state") or fm.get("review_state") or "open",
        # A prior record remains visible as history, but must never be sent as the id for a
        # decision against the current version. The writer will create the current request.
        "review_id": (current_record or {}).get("review_id"),
        "reasons": (current_record or record or {}).get("reasons") or _review_reasons(fm, body),
        "evidence": evidence,
        "evidence_total": len(evidence),
        "evidence_resolved": sum(1 for row in evidence if row.get("page")),
        "machine_checks": (current_record or record or {}).get("machine_checks") or [],
        "history": (current_record or record or {}).get("history") or [],
        "assigned_to": (current_record or {}).get("assigned_to"),
        "decision_context": _review_decision_context(fm),
        "review_enabled": _REVIEW_ENABLED,
        "review_auth_mode": _REVIEW_AUTH_MODE,
    }
