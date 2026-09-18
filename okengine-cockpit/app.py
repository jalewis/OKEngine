#!/usr/bin/env python3
# ruff: noqa: F821
"""okengine-cockpit — standalone, read-only "intelligence cockpit" reader for an OKF vault.

A function-oriented companion to okengine-reader: instead of a generic browse rail,
it presents a 3-zone cockpit (a STREAM rail of dated briefings + function TABS:
briefings / dashboards / predictions, plus an OPTIONAL competitors / watchlist
tracker). It is DOMAIN-AGNOSTIC: every domain-specific surface (the streams, the
display title, the watchlist's tracked entity types / field names / labels, the
curated dashboard index, the competitor views) is driven by an OPTIONAL `cockpit:`
block in the pack's `<vault>/schema.yaml`. On any OKF vault with no `cockpit:`
block it falls back to generic defaults (a "Recent briefings" stream + the
briefings / predictions / dashboards tabs); the watchlist + competitors tabs stay
hidden until a `watchlist:` config lights them up.

Deliberately SEPARATE from the Hermes agent/console: imports no hermes modules,
makes no calls to the gateway or dashboard, reads `<vault>/schema.yaml` directly
(yaml only), and serves only from a READ-ONLY mount of the vault. It keeps working
even if the entire Hermes stack is down.

Env:
  VAULT_DIR   read-only vault root (default /vault); wiki at VAULT_DIR/wiki
  PORT        listen port (default 9200)
  OKENGINE_READER_PASSWORD  if set, require HTTP Basic auth (shared with the reader —
              one credential protects both UIs; see _BasicAuth). OKENGINE_READER_USER
              overrides the username (default `okengine`).
  OKENGINE_TRUST / OKENGINE_BIND  a PRIVATE vault (trust=private, the default) REFUSES to
              start when exposed off-loopback with no password — the same fail-safe the
              reader enforces (okengine#90 P4a). The cockpit is a SUPERSET of the reader
              (adds an agent-Chat relay), so it must not be laxer than the reader.
"""

from __future__ import annotations

import functools
import importlib as _service_importlib
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
# Service functions are rebound to THIS module's globals (see _bind_service), so every name a
# service body uses must be importable here too (okengine#659: the shared sanitizer).
from okengine.html_sanitize import restore_panel_svg, sanitize, stash_panel_svg  # noqa: F401
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from okengine.actor_identity import actor_identity_error
from okengine.schema_exclusions import excluded_namespaces_from_schema

from auth import BasicAuth as _BasicAuth, exposure_refusal

VAULT = Path(os.environ.get("VAULT_DIR", "/vault"))
WIKI = VAULT / "wiki"


def _governing_schema_path(vault: Path = VAULT) -> Path:
    """One schema authority for every Cockpit consumer.

    A composed deployment's generated artifact is engine + pack + enabled
    extensions and is also what the write path enforces. Falling back to the
    pack schema preserves plain, non-composed deployments.
    """
    artifact = vault / ".okengine" / "composed-schema.yaml"
    return artifact if artifact.is_file() else vault / "schema.yaml"


STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def _lifespan(_app):
    """Start Cockpit cache workers through FastAPI's supported lifespan contract."""
    # Populate the landing page before declaring readiness. Starting whole-vault/all-tab Python
    # scans in background threads here starves the first UI request on large deployments.
    _warm_initial_tab_datasets()
    # Do not eagerly warm the global review snapshot here. Its YAML-heavy whole-vault scan competes
    # with initial UI requests even in a background thread. Dashboard review launchers declare
    # namespace-scoped `review_dirs`; the complete global worklist initializes only when requested.
    # Remaining configured tabs warm only AFTER the readiness-critical landing scan. The worker is
    # serial, paced, and request-aware, so it cannot recreate #320's startup GIL/disk starvation.
    _schedule_remaining_tab_warmup()
    yield


app = FastAPI(title="OKEngine · cockpit", docs_url=None, redoc_url=None, lifespan=_lifespan)


_READER_PASSWORD = os.environ.get("OKENGINE_READER_PASSWORD", "")
_READER_USER = os.environ.get("OKENGINE_READER_USER", "okengine")
if _READER_PASSWORD:
    app.add_middleware(_BasicAuth, user=_READER_USER, password=_READER_PASSWORD)

# Review writes are opt-in and fail closed. Cockpit keeps its vault mount read-only and proxies only
# the narrow review state-machine operation to a bridge-only governed write service. A configured
# write service without browser authentication is deliberately treated as disabled.
_REVIEW_API = os.environ.get("OKENGINE_REVIEW_API", "").rstrip("/")
_REVIEW_TOKEN = os.environ.get("OKENGINE_REVIEW_TOKEN", "")
_REVIEWER_CONFIGURED = os.environ.get("OKENGINE_REVIEWER_NAME", "").strip()
_REVIEWER = _REVIEWER_CONFIGURED or _READER_USER
_REVIEW_TRUSTED_NETWORK = os.environ.get("OKENGINE_REVIEW_TRUSTED_NETWORK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
_REVIEW_AUTH_MODE = (
    "basic" if _READER_PASSWORD else "trusted-network" if _REVIEW_TRUSTED_NETWORK else "disabled"
)
_REVIEW_ENABLED = bool(
    _REVIEW_API
    and _REVIEW_TOKEN
    and ((_READER_PASSWORD and _REVIEWER) or (_REVIEW_TRUSTED_NETWORK and _REVIEWER_CONFIGURED))
)

# Mutating operation controls use a separate, bridge-only runner. They share the
# established operator authentication decision, but not the review state-machine API.
_OPERATION_API = os.environ.get("OKENGINE_OPERATION_API", "").rstrip("/")
_OPERATION_TOKEN = os.environ.get("OKENGINE_OPERATION_TOKEN", "")
_OPERATION_ENABLED = bool(
    _OPERATION_API
    and _OPERATION_TOKEN
    and ((_READER_PASSWORD and _REVIEWER) or (_REVIEW_TRUSTED_NETWORK and _REVIEWER_CONFIGURED))
)

# Trust enforcement (okengine#90 P4a): a PRIVATE vault must never be served unauthenticated when
# publicly exposed. Mirrors okengine-reader exactly — default trust=private → fail-safe: refuse to
# start rather than expose a private vault to the network without a password.
_TRUST = os.environ.get("OKENGINE_TRUST", "private").strip().lower()
_BIND_HOST = os.environ.get("OKENGINE_BIND", "127.0.0.1").strip()
if refusal := exposure_refusal(_TRUST, _BIND_HOST, _READER_PASSWORD):
    raise SystemExit(refusal)


from okengine.cockpit_services import state as _cockpit_state

_cockpit_state = _service_importlib.reload(_cockpit_state)

for _state_name in _cockpit_state.__all__:
    globals()[_state_name] = getattr(_cockpit_state, _state_name)
del _state_name

# ── markdown / frontmatter helpers ──────────────────────────────────────────
try:
    from yaml import CSafeLoader as _FAST_YAML_LOADER
except ImportError:  # pragma: no cover - minimal PyYAML builds without libyaml
    from yaml import SafeLoader as _FAST_YAML_LOADER

# Prediction "open" vocabulary — a cross-surface contract. config/base-schema.yaml
# `tier.namespaces.predictions.open_values: [open, active]` is the source of truth, mirrored by
# pred_lib.OPEN_VALUES (extensions/okengine.predictions) and read config-driven by tier_lib /
# build_hot_set / select_daily_brief. The cockpit is a FOURTH consumer: it must count `active`
# predictions as open too (predictions routinely carry status:active — migrated/drained sets), or
# the home 'Open predictions' section and due-soon tally silently undercount (invariant-audit M11).
# Same env override knob pred_lib uses, so a pack with a different vocabulary stays consistent.
# tests/test_cockpit_panels.py pins this set to pred_lib.OPEN_VALUES.


# ── cockpit config (the ONLY place domain knowledge enters — from the pack) ──
# Generic defaults so the cockpit works zero-config on any OKF vault; a pack
# overrides via an optional `cockpit:` block in <vault>/schema.yaml. See README.

# Common initialisms a naive .title() mangles when humanizing a slug/key for display (a vault dir
# name, a watchlist tab key). GENERIC computing/universal acronyms only — domain-specific ones
# (CVE, IOC, OT/ICS, …) belong in the pack that emits the content, not the engine display layer.
# Mixed-case forms (IoT, SaaS) are spelled out.


# `[APT41](entities/a/apt41)` — an INTERNAL vault link (the agent's linked-title citations). Not an
# image (`!` excluded), not external (http/mailto/# excluded).


# generic OKF/Obsidian namespaces an embed/page basename might resolve under; pure
# resolution fallback (no domain knowledge — just common wiki folder names).


# Agents across lanes "highlight" a wikilink by wrapping it in backticks (`[[x]]`). That makes
# _linkify inject the <a> INSIDE an inline-code span, so markdown escapes it to visible `<a …>` text
# in the UI. Strip the backticks around a bare wikilink first — the author meant a link, not code.


# ── config surface (frontend reads title + which tabs to show) ──────────────


# ── briefings (read mode) — streams are pack-config-driven ──────────────────


# The vault is mounted read-only, so on-demand deck renders cache under a writable dir, keyed by the
# source .md's mtime (a regenerated deck re-renders; stale renders are pruned).


# ── predictions (track mode) ────────────────────────────────────────────────


# evidence entries arrive as dicts (`{date, direction, note, source, confidence_*}`) OR as
# `[YYYY-MM-DD tag] free text` strings (the regrade lanes stamp this compact form). Parse both
# into one render-ready shape so the ledger tally and the detail drilldown agree.

# The sanctioned direction vocabulary is DERIVED from the vault's composed schema — single
# source: the predictions extension's schema fragment, enforced at the write path (okengine
# #211/#217). This map covers only PRE-BACKFILL history (okengine#219) and the compact
# string-form tags; it must never grow to absorb new producer drift — the write path now
# rejects that at the boundary, and laundering it here is exactly the D1 anti-pattern.


@functools.lru_cache(maxsize=1)
def _ev_direction_enum() -> frozenset:
    """Sanctioned `evidence[].direction` values from the vault's composed schema artifact
    (.okengine/composed-schema.yaml -> field_items.evidence.direction.enum). Falls back to
    the canonical four when no artifact declares it. Cached for the process lifetime — the
    composition changes only at (re)deploy, which recreates this container."""
    try:
        art = VAULT / ".okengine" / "composed-schema.yaml"
        doc = yaml.safe_load(art.read_text(encoding="utf-8")) if art.is_file() else None
        rule = (((doc or {}).get("field_items") or {}).get("evidence") or {}).get("direction") or {}
        ev = rule.get("enum")
        if isinstance(ev, list) and ev:
            return frozenset(str(v) for v in ev)
    except Exception:
        pass
    return _EV_DIR_FALLBACK


# ── frontmatter table helpers ───────────────────────────────────────────────


@app.middleware("http")
async def _track_active_requests(request: Request, call_next):
    """Expose interactive pressure to the post-ready warmer.

    A namespace scan is CPU/syscall heavy and cannot be preempted once started, so the worker checks
    this counter before each namespace and yields while any request is in flight. Requests never
    wait for the warmer; at worst one already-started namespace scan completes in the background.
    """
    global _ACTIVE_REQUESTS
    with _ACTIVE_REQUESTS_LOCK:
        _ACTIVE_REQUESTS += 1
    try:
        return await call_next(request)
    finally:
        with _ACTIVE_REQUESTS_LOCK:
            _ACTIVE_REQUESTS -= 1


# A cell that is a bare date (YYYY-MM-DD), a number/percentage, or the em-dash placeholder is a
# structured value that must never wrap or break across lines — dates like `2026-09-30` were breaking
# mid-token when a long first column squeezed the table. Tag those cells `.num` (nowrap + right-align).
# Matches only PLAIN values, so a cell holding HTML (a page link, a chip) stays a normal wrapping cell.


# ── watchlist & trends (OPTIONAL tracker — pack-config-driven, no domain literals) ──


# ── competitors (track mode — render pack-configured generated dashboards) ───


# ── dashboards grid (curated reading order, else auto-listed) ────────────────


# ── declarative dataset tabs ─────────────────────────────────────────────────
# The pack's cockpit config can define whole tabs as DATASET BOXES: each box names a
# dataset (a dir + optional type/where filters) and a view (table / bars / chips /
# bignums / cards / coverage / doc). The engine renders; the pack decides which datasets
# an analyst sees and how they're labeled — including value maps for opaque codes
# (e.g. NAICS sector numbers). A box whose dataset is EMPTY renders its `empty:` note
# when one is configured (pipeline state is information), otherwise it is omitted —
# never a wall of "none" placeholders. Design source: the okcti data-first redesign.


# Inline marker for a group_by value the pack's `labels:` map doesn't cover — the label falls
# back to the raw code, so flag it as degraded rather than let an opaque code masquerade as a
# curated label (okengine#188).
# Sentinel group value for the collapsed "unmapped (N)" bucket a `bucket_unmapped` box emits
# (okengine#259). Not a real frontmatter value; api_drill reads it as "pages whose group value is
# outside the labels vocabulary" — the drill to the drift offenders.


@functools.lru_cache(maxsize=4096)
def _ref_title(rel: str) -> str:
    """The `title` of the page at `rel`, or "" when it cannot be read."""
    path = WIKI / f"{rel}.md"
    if not path.is_file():
        return ""
    # Read to the END OF FRONTMATTER, not a fixed byte window. A first attempt capped at 2048 bytes
    # and silently missed `scattered-spider`, whose `title:` sits at line 67 behind a 60-alias
    # list — the board then fell back to the slug and displayed "scattered spider" beside properly
    # titled peers. Frontmatter length is bounded by the frontmatter; file length is not.
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            if fh.readline().rstrip("\n").strip() != "---":
                return ""
            for _ in range(400):  # a sane ceiling on frontmatter lines
                line = fh.readline()
                if not line or line.rstrip("\n").strip() == "---":
                    return ""
                if line.startswith("title:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        return ""
    return ""


# A doc box renders a whole vault page inline. A generated dashboard can grow without bound
# (okcti's adversarial-evidence-review hit 500KB source → a 650KB HTML panel that dominated the
# tab payload and stalled the browser render — 2026-07-19 UI sweep). Cap what a PANEL inlines;
# the full document stays one click away.


# Engine-generated operational/health artifacts, grouped for the Ops tab. These filenames are
# ENGINE outputs (produced by engine crons on any OKF vault) — not domain facts — so a curated
# map is legitimate here. Each group lists candidate page paths (without .md); only those that
# exist on disk are shown, and any remaining wiki/operational/*.md is swept into "Operational log"
# so a new artifact is never silently hidden.


# ── UI extension panels (okengine#160, ported from the reader) ───────────────


# ── page overlay: fact panel + multi-source conflict/observation view (ported from the reader) ──
# The reader treats a clicked page as a TYPED intel object: the surfaced frontmatter is its profile
# (fact panel), record-keeping is tucked away (record details), and the assembler's multi-source
# `conflicts:` + `observations/` records show "what each source says". All domain-agnostic — it
# renders whatever fields/conflicts/observations exist, in frontmatter order.
# Namespaces whose pages are the TARGETS of bare-id frontmatter refs. Cross-reference/enrichment
# lanes stamp bare ids or slugs (e.g. an entity id `G0022`, a `CVE-…` id) that _ref_target
# (path-shaped only) won't linkify; _id_index resolves them against these namespaces for the overlay.


# ── page quality/status badges (okengine — generic page health) ─────────────────────────────────
# A problem-only badge row atop the overlay, computed from data already present. Nothing
# domain-specific: which fields a type REQUIRES comes from schema.yaml; the rest are envelope
# signals (sources/grounding/review/conflicts/recency/size). A clean page gets no row.


@functools.lru_cache(maxsize=1)
def _review_required_types() -> frozenset[str]:
    for path in (VAULT / ".okengine" / "composed-schema.yaml", VAULT / "schema.yaml"):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        values = doc.get("review_required_types") if isinstance(doc, dict) else None
        if isinstance(values, list):
            return frozenset(str(v) for v in values)
    return frozenset()


# -inf, not 0.0 (fresh-host trap — see _DIR_TTL note): a finite init on a just-booted container
# reads the EMPTY snapshot as fresh and serves a blank review queue for the whole TTL.


@app.get("/api/rollup")
def api_rollup(stream: str = Query(...), days: int = Query(7)):
    """Stack the latest N days of a briefing stream into one scrollable review."""
    cfg = _streams().get(stream)
    if not cfg:
        raise HTTPException(404, "unknown stream")
    n = max(1, min(int(days), 31))
    parts = []
    for dt in _stream_dates(stream)[:n]:
        try:
            d = api_doc(stream=stream, date=dt)
        except HTTPException:
            continue
        parts.append(f'<section class="rollup-day"><h2 class="rday">{dt}</h2>{d["html"]}</section>')
    return {
        "title": f"{cfg['label']} — past {len(parts)} days",
        "count": len(parts),
        "html": "".join(parts),
    }


# ── downloads (md / docx / pdf via pandoc) ─────────────────────────────────


# Print stylesheet for the weasyprint PDF path. Pandoc ships no CSS, so a wide markdown table or a
# long unbreakable token (URL, hash, IOC) runs off the right edge of the page. Constrain everything
# to the page box: real margins, fixed-layout full-width tables, and word-breaking in every cell.


# A leading progress-narration line ("Checking the vault…", "Pulling the pages now", "Good leads").
# The contract asks the agent to keep these out of a report, but local models still emit them; we
# strip them from the EXPORTED report (they're fine as live feedback in the chat).


# ── global search (ripgrep across the vault) ───────────────────────────────
# Dir → rank (lower sorts first). Content pages rank above sources. Generic
# namespace names only (no domain knowledge); unlisted dirs get a mid rank.


# ── backlinks (knowledge-graph: "what links here") ─────────────────────────
# The cron-precomputed wiki/.backlinks.json (below) is served directly. The live FALLBACK builds
# the graph by scanning [[wikilinks]] over the vault directly (okengine#179) — it used to shell
# the heavy `iwe find -l 0` full-graph dump (~4GB/~550s on a big vault, and UNFILTERED). The scan
# MIRRORS scripts/cron/backlink_lib (keep in sync). Invert forward-refs into a {target:[referrers]}
# map, filter + curate titles, cache with a TTL. Read-only, so it works on the :ro mount.
# the fallback scan is cheap now, but backlinks change over days, so a day-stale "what links here"
# is fine. Tune per-deployment via the env var.

# Cron-precomputed graph (okengine#168): the `backlinks-refresh` engine cron
# writes the inverted+filtered+titled map to wiki/.backlinks.json once per
# deployment per day (scripts/cron/backlink_lib.py is the canonical logic —
# it also applies the generated-source filter and curated titles this app's
# live build never had). When present and fresh we serve it directly and never
# run iwe in this container; the live build below is only the fallback for a
# missing/stale artifact. Ceiling default 48h = two missed daily runs.


# Backlink filter + scanner — MIRRORS scripts/cron/backlink_lib (keep in sync so this fallback
# doesn't drift from the served artifact). See the reader for the annotated originals.


# ── generic browse (namespaces → pages, ported from okengine-reader) ─────────
# A function-agnostic explorer alongside the cockpit's curated tabs: the wiki/
# directory tree with per-namespace page lists + pack-declared "by kind" groups,
# discovered from the vault at runtime (ships no domain knowledge).


# init ts = -inf so the first call always misses (monotonic() can be < TTL on a fresh boot).
# `dashboards/` is generated but MEANT to be read (the payoff of the vault); schema `exclude:`
# scopes CONFORMANCE, not reader visibility, so surface it (flagged `derived`) rather than hide it.


# ── about (deployment identity, ported from okengine-reader) ─────────────────


# ── agent chat (relay to the Hermes OpenAI-compatible api_server) ────────────
# The cockpit runs NO model of its own. The Chat tab relays to THE agent (Hermes), which
# answers by NAVIGATING the OKF wiki via its graph tools — the wiki-as-memory demonstration,
# the deliberate counter to RAG. Configured by env so a deployment without an agent endpoint
# simply never shows the tab.


def _intenv(name: str, default: int, lo: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, v)


_CHAT_MAX_MSGS = _intenv("OKENGINE_READER_CHAT_MAX_MSGS", 24, lo=2)
_CHAT_MAX_CHARS = _intenv("OKENGINE_READER_CHAT_MAX_CHARS", 8000, lo=200)

# Grounding contract for the chat session (server-controlled — the browser cannot override it).
# The vault is the agent's memory and the FIRST stop; external research is written BACK so the
# corpus compounds. This is the OKF/agent-wiki thesis, not RAG.


# ── shell ───────────────────────────────────────────────────────────────────


# Service implementation binding. Functions are rebound to this facade's globals so
# existing public callables and test injection seams remain stable during incremental extraction.
import types as _service_types
from okengine.cockpit_services import (
    assessments,
    chat,
    configuration,
    dataset_views,
    datasets,
    documents,
    drill_ops,
    exports,
    navigation,
    page_metadata,
    reviews,
    streams,
    tid_views,
)


def _bind_service(module) -> None:
    names = (
        name
        for name, value in vars(module).items()
        if isinstance(value, _service_types.FunctionType) and value.__module__ == module.__name__
    )
    for name in names:
        implementation = getattr(module, name)
        rebound = _service_types.FunctionType(
            implementation.__code__,
            globals(),
            name,
            implementation.__defaults__,
            implementation.__closure__,
        )
        rebound.__kwdefaults__ = implementation.__kwdefaults__
        rebound.__annotations__ = implementation.__annotations__
        rebound.__doc__ = implementation.__doc__
        globals()[name] = rebound


for _service_module in (
    configuration,
    streams,
    datasets,
    assessments,
    tid_views,
    dataset_views,
    documents,
    drill_ops,
    page_metadata,
    reviews,
    exports,
    navigation,
    chat,
):
    _bind_service(_service_module)
del _service_module

app.get("/api/config")(api_config)
app.get("/api/streams")(api_streams)
app.get("/api/doc")(api_doc)
app.get("/api/stream.pdf")(api_stream_pdf)
app.get("/api/predictions")(api_predictions)
app.get("/api/prediction")(api_prediction)
app.get("/api/watchlist")(api_watchlist)
app.get("/api/competitors")(api_competitors)
app.get("/api/home")(api_home)
app.get("/api/application")(api_application)
app.get("/api/tab/{key}")(api_tab)
app.get("/api/drill/{tab}/{box}")(api_drill)
app.get("/api/dashboards")(api_dashboards)
app.get("/api/ops")(api_ops)
app.get("/api/policy")(api_policy)
app.get("/api/review")(api_review)
app.get("/api/reviews")(api_reviews)
app.get("/api/operations")(api_operations)
app.post("/api/operations/{name}/plan")(api_operation_plan)
app.post("/api/operations/{name}/run", status_code=202)(api_operation_run)
app.get("/api/operations/requests/{request_id}")(api_operation_request)
app.post("/api/review/decision")(api_review_decision)
app.post("/api/review/assign")(api_review_assign)
app.get("/api/page")(api_page)
app.get("/api/download")(api_download)
app.post("/api/chat_export")(api_chat_export)
app.get("/api/search")(api_search)
app.get("/api/backlinks")(api_backlinks)
app.get("/api/tree")(api_tree)
app.get("/api/groups")(api_groups)
app.get("/api/pages")(api_pages)
app.get("/api/about")(api_about)
app.post("/api/chat")(api_chat)
app.get("/favicon.ico", include_in_schema=False)(favicon)
app.get("/", response_class=HTMLResponse)(index)
app.get("/healthz")(healthz)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
