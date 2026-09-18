#!/usr/bin/env python3
# ruff: noqa: F821
# mypy: disable-error-code="name-defined"
"""Thin MCP transport for governed writes over stdio or authenticated HTTP.

Bounded services enforce schema validation, authorization, review, integrity,
transactions, patching, and convergence while this facade preserves MCP tools.
"""
from __future__ import annotations
import contextvars
import datetime
import difflib
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import fcntl
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional, Union, cast

import yaml
from starlette.requests import Request as StarletteRequest
from tools.schema_validator import (
    schema_reject_reason,
    governing_policy,
    drift_policy,
    canonicalize_enum_case,
)
from tools import policy_plane
from okengine.mcp import scope as _scope
from okengine.actor_identity import actor_identity_error, has_positive_actor_identity
from okengine.corpus_transaction import mutation as _corpus_mutation
from okengine.corpus_transaction import touch as _corpus_touch
import output_contract_enforce as _output_contract
# Converge-on-write (composable okpacks P2) needs the id + schema + merge libs.
# Optional: if any are absent, converge_entity is disabled but the rest works.
_CONVERGE_OK = True
try:
    import id_lib
    import schema_lib
    import id_index
    import converge
    import okf_migrate
except Exception:  # pragma: no cover - write-path libs absent in a host test env
    _CONVERGE_OK = False

# `or` (not a get() default): a set-but-blank WIKI_PATH must fall back too, else
# Path("")/"wiki" resolves to a *relative* wiki/ under CWD (okengine#34).
# A TRUE H1 (`# title`). `[ \t]+` after the single `#` means `## Summary` and deeper
# section headings never match — only a page-title H1 is captured (group 1).


# Per-request caller identity (okengine#132), set by the networked auth middleware.
# None (stdio — the trusted local gateway caller) = admin = FULL write, which is the
# pre-#132 behavior. A networked extension caller is limited to its write scopes.


# Field SHAPES are schema-DECLARED (base-schema `field_shapes`, pack-extensible) rather than
# hardcoded (okengine#196 generalized): a list field authored as a SCALAR string (e.g.
# `aliases: StealC, StealC info-stealer`) would otherwise sail through the open/untyped schema and
# crash a list-consuming lane. The write path coerces scalar -> list for every schema-declared list
# field at the single enforced-write chokepoint, so no such page can enter the vault.
# Fallback if the schema declares no `field_shapes` (older base-schema) — the set okengine#196 first
# hardcoded, now the safety net under the schema-driven resolution.


# INT-shaped fields are machine-owned COUNTS (a metrics lane stamps them). An agent that misreads
# the field name semantically writes garbage that a numeric-consuming dashboard then renders/sorts —
# live incident: `recent_reports:` hand-set to a LIST of source paths topped the cockpit's
# Most-active table. A digit-string coerces (the intent is unambiguous); anything else REJECTS with
# the field named, the same actionable-feedback loop as a schema reject. Pre-shape schemas declare
# no int fields, so the check is inert there.


# ITEM contracts (okengine#211): per-key rules for LIST-OF-DICT fields (field_items — e.g. the
# predictions `evidence:` records). The vocabulary a consumer buckets on (evidence[].direction)
# previously lived only in prompt text, so agent-authored values drifted and the cockpit tally
# silently mis-bucketed them (D1: 18 drifted entries). Enforcement lives HERE — the boundary every
# writer crosses — not in prompts (instruction, not enforcement) and not in consumer synonym maps
# (laundering that masks producer drift). Out-of-enum/wrong-shape REJECTS with field, index, and
# the allowed set named, so an agent retry self-corrects (and, on a page carrying legacy drifted
# entries, effectively backfills them — it must resubmit the full list clean). Schemas that declare
# no field_items make the check inert.


# OKF reserved + engine-managed structural files. These are NOT agent-writable
# knowledge pages — log.md is appended by the server itself (_append_log), the
# INDEX tree is rebuilt by a cron, HOT.md is derived, the review queue is
# server-managed, and `_`-prefixed files are internal. Writing them through the
# entity tools would (e.g.) inject a YAML frontmatter block into a plain
# changelog. Every agent-facing write helper refuses them up front.
# health.md + bundle.md are engine-generated root dashboards (build_index_tree.py regenerates
# HEALTH.md each run; BUNDLE.md is composed). schema_validator broadened its reserved DEFAULT to
# exempt them from conformance (876fceb) — so if the write path does NOT also refuse them the two
# guards compose into ZERO protection (validator skips, write path allows fabricated content).
# Keep the two lists in lockstep; test_write_server pins them. invariant-audit.


# --- G2/G3 write-governance: structural permissions + REVIEW FLAGS ---------
# The MCP write tools are the ENFORCED write path (the file-tool guard stays the
# schema/shape backstop). The pack declares policy in schema.yaml (read via
# tools.schema_validator.governing_policy, walk-up). Two distinct mechanisms:
#
#   1. STRUCTURAL permissions (HARD, rare) — `permissions.{default,namespaces}`:
#      per-namespace create/update rights (default: both allowed) and
#      delete:false everywhere (a knowledge page is tombstoned, never hard-rm'd —
#      data safety, not a review gate). A create/update-denied namespace is a real
#      structural boundary (e.g. a human-authored namespace), defaulting open.
#
#   2. REVIEW FLAGS (SOFT — flag, never gate) — `review.*`:  at 40k+ docs a hard
#      "needs human approval" GATE is impractical, so high-stakes agent
#      assertions are not blocked — the write SUCCEEDS and the page is FLAGGED
#      (`needs_review: true` + a wiki/_review-queue.md entry + a log note) so a
#      human / the UI can highlight it. Triggers: an agent asserting/escalating a
#      categorical `confidence` verdict (confirmed/false-positive/refuted —
#      numeric + low/med/high never flag), or setting/changing a configured
#      `review_on_change_field`. Preserving an already-present value never flags.


# OKF envelope + assembler/bookkeeping keys allowed on any page, so unknown-field flagging
# (okengine#46) targets only genuine domain drift, never the universal scaffolding.


# --- plain logic helpers (tested directly) -------------------------------


# --- future-date guard ------------------------------------------------------
# The envelope's record-keeping dates say when a page WAS written/touched — a future value is
# always fabricated (a weekly-brief lane hallucinated published: <next Sunday> onto an empty
# stub, despite its prompt explicitly forbidding a guessed date; prompts are the unenforced
# half). Enforced HERE — the boundary every writer crosses. Deliberately NARROW: only the
# record-keeping fields — domain dates (a KEV due_date, an event date, a contract end) are
# legitimately future and are never checked. +1 day tolerance absorbs TZ skew (a UTC-thinking
# model just past midnight UTC is "tomorrow" relative to a US-eastern host clock).


# --- briefing wikilink guard --------------------------------------------------------------
# Briefings are ANALYSIS pages that cite existing knowledge — every [[wikilink]] on one must
# resolve, or the flagship page a human reads daily ships dead links (live incident: the daily
# brief invented slugs from memory — [[entities/q/quimarat]] for the real quimat-rat page — and
# the broken-wikilinks drain's >=3-inbound wake gate treats 1-ref brief links as orphan noise
# forever). Scoped to briefings/ ONLY: source pages legitimately forward-reference entities that
# don't exist yet (the stub-creation drain depends on that), so a vault-wide check would break
# the ingest pattern. Rejection carries did-you-mean suggestions so the lane model can retry
# with the real slug — the same feedback loop schema rejections use.
# Namespaces where an unresolvable wikilink is a DEFECT worth a SOFT needs_review flag at write time
# (curated content), vs sources/indicators where a forward-ref to a not-yet-created page is the norm.
# Briefings get the HARD reject (_briefing_link_reject); these get a flag — surface broken links AT
# WRITE so they're attributable and don't just accrue for the drains to chase (link-audit 2026-07-09).
# A cheap per-link existence check, NOT a full-vault scan (that would reintroduce the per-write cost
# the id-index fix removed).


# A `sources:` entry that uses the SINGULAR `source/` page-path — a structurally-invalid spelling of
# the schema's plural `sources/` namespace. EVERY observed entity-backfill hallucination cited sources
# this way (apt35 → source/mandiant/…, nightshade → source/darkread-…, apt29 → source/cisa/…). Legit
# citations use plural `sources/…`, and a plural forward-ref to a not-yet-created source page is
# TOLERATED (importers write an entity before its source in the same batch — okengine#196's coercion
# test relies on it), so we do NOT touch plural — corpus-audit's dangling-ref detector (#336) covers
# plural fabrications after the fact. Provenance LABELS ("MITRE ATT&CK") carry spaces and never match.


# Degeneration guard: a model in a repetition loop emits a long unpunctuated word-salad. It
# renders a clean 200, so it slips past every render check and only the periodic content lint
# catches it — weeks after it lands. Flag it SOFTLY at the enforced write boundary instead,
# model-agnostic, so ANY lane's degenerate output is attributable at write. Mirrors
# scripts/cron/content_lint.py's predicate (same threshold); kept in sync by a cross-surface
# contract test. Precision-tuned on a real multilingual vault: commas terminate and wikilinks
# are stripped so a long legitimate LIST (MITRE techniques, killed services) is not flagged, and
# a CJK-latin-fusion signal was DROPPED (it can't tell code-switching from legitimate Chinese CTI).
# code-switching fuses a latin token to its CJK translation (`known漏洞`). Both render a clean 200,
# so they slip past every render check and only the periodic content lint catches them — weeks after
# they land. Flag them SOFTLY at the enforced write boundary instead, model-agnostic, so ANY lane's
# degenerate output is attributable at write. Mirrors scripts/cron/content_lint.py's predicate (same
# thresholds); kept in sync by a cross-surface contract test. Precision-tuned: a coherent long
# paragraph clears it (250 words is well above a verbose run-on, below a 500+-word loop).


# Identity + provenance fields that are IMMUTABLE after creation: `id` never changes (id_lib.py:22),
# and created/created_by/discovered_by are the create-time provenance the converge lane already
# protects (converge._PROVENANCE_KEYS, M19). But _update merged caller frontmatter wholesale
# (new_fm.update(patch)) and _patch re-parsed the edited text wholesale — neither preserved these, so
# update_entity/patch_entity could freely rewrite an id or forge provenance: exactly the class the
# audit closed on converge, left open on the other two mutating lanes (invariant-audit HIGH #3).
# extension_id is handled separately by _apply_extension_provenance; maintained_by is additive.


# --- G1.1: body-preserving surgical edits + field-loss guard --------------
# update_entity does whole-body REPLACE (fine for frontmatter-only / wholesale
# rewrites); these add the SURGICAL primitives the drains need without resending
# the whole page: patch_entity (exact one-shot replace, like the Edit tool) and
# append_to_section (append into a `## heading` block). Both re-validate against
# schema, run the review gate, and — unlike the file tool — enforce a hard
# FIELD-LOSS guard: an edit may not drop an existing frontmatter key.


# These headings are generated by the read MCP from the live backlink graph. Authoring them into a
# canonical page freezes derived state into prose and makes the same panel appear twice to readers.


# --- converge-on-write (P2): upsert by id, merge under page+field ownership ---
import importlib as _service_importlib
from okengine.write_services import (
    authorization,
    convergence,
    deduplication,
    integrity,
    patching,
    policy,
    review,
    state as _write_state,
    transactions,
    transport,
    validation,
)
from okengine.write_services.authorization import ACTOR_TOOLS as _ACTOR_TOOLS
from okengine.write_services.binding import (
    install_services as _install_services,
    install_state as _install_state,
)

_write_state = _service_importlib.reload(_write_state)
_install_state(globals(), _write_state)
_install_services(
    globals(),
    (
        authorization,
        validation,
        review,
        policy,
        deduplication,
        integrity,
        transactions,
        patching,
        convergence,
        transport,
    ),
)

# --- FastMCP wrappers (delegate to the plain helpers) --------------------

mcp: Any = None
try:
    def _resolve_fast_mcp() -> Any:
        try:
            # MCP <2.0 exposed the decorator-driven server under ``fastmcp``.
            from mcp.server.fastmcp import FastMCP

            return FastMCP
        except ImportError:
            # MCP 2.0 removed ``fastmcp`` and renamed the same tool decorator,
            # removal, run, and HTTP-app surface to MCPServer. Hermes v2026.9.14
            # ships this layout, so governed writers must support both SDKs.
            from mcp.server.mcpserver import MCPServer

            return MCPServer

    mcp = _resolve_fast_mcp()("okengine-write")

    mcp.tool = _fence_tool_registrations(mcp.tool, _caller, _wiki)

    @mcp.tool()
    def create_entity(path: str, frontmatter_yaml: str, body: str = "") -> str:
        """Create a new governed page, refusing an existing path or invalid schema."""
        admission_error = _actor_payload_reject(frontmatter_yaml, body, path)
        return admission_error or _create(path, frontmatter_yaml, body)

    @mcp.tool()
    def update_entity(
        path: str, frontmatter_yaml: str = "", body: Optional[str] = None, expected_sha256: str = ""
    ) -> str:
        """Update an EXISTING wiki page. Merges frontmatter keys (if given) and replaces the
        body when `body` is provided. Pass body="" to intentionally CLEAR the body; OMIT body
        (leave it null) to keep the current body. Bumps version, sets last_updated. Validates
        before writing; on reject the existing file is untouched."""
        fm = frontmatter_yaml if frontmatter_yaml else None
        return _update(path, fm, body, expected_sha256)  # None -> keep; "" -> clear

    @mcp.tool()
    def score_source(path: str, reliability: str, credibility: int) -> str:
        """Set only Admiralty reliability (A-F) and credibility (1-6) on an
        existing source. This narrow operation cannot mutate body, identity,
        provenance, lifecycle, or publication fields."""
        rel = str(reliability).strip().upper()
        try:
            cred = int(credibility)
        except (TypeError, ValueError):
            return "rejected: credibility must be an integer from 1 through 6"
        if rel not in {"A", "B", "C", "D", "E", "F"}:
            return "rejected: reliability must be one of A, B, C, D, E, F"
        if cred not in range(1, 7):
            return "rejected: credibility must be an integer from 1 through 6"
        result = _update(path, {"reliability": rel, "credibility": cred}, None)
        if _write_actor == "cron:source-quality-backfill" and not result.startswith(
            ("rejected:", "refused:", "error:")
        ):
            return (
                f"{result}\nSUCCESS: this selected source is complete. "
                "Do not call any tool again; emit the required receipt now."
            )
        return result

    @mcp.tool()
    def tombstone_entity(path: str, reason: str, superseded_by: str = "") -> str:
        """Tombstone (NOT delete) an existing page: sets status: tombstoned,
        tombstone_reason, optional superseded_by, bumps version. The file is
        retained on disk. Validates before writing; appends a log.md line."""
        return _tombstone(path, reason, superseded_by or None)

    @mcp.tool()
    def flag_for_review(path: str, note: str) -> str:
        """Queue a page for human review by appending to wiki/_review-queue.md
        (created if absent). Does NOT mutate the target page. Logs the flag."""
        return _flag(path, note)

    @mcp.tool()
    def patch_entity(path: str, old_string: str, new_string: str) -> str:
        """Surgically edit ONE place in an existing page (like an exact-match
        find/replace) — body-preserving, no need to resend the whole page. Use for
        fixing a wikilink, inserting a section before a heading, changing one
        field. `old_string` must occur EXACTLY ONCE (add surrounding context to
        disambiguate). Rejects if the edit drops an existing frontmatter field,
        breaks the YAML, or violates schema. Bumps version, logs, review-gates."""
        return _patch(path, old_string, new_string)

    @mcp.tool()
    def append_to_section(path: str, heading: str, text: str) -> str:
        """Append `text` to the end of the `## heading` section of an existing page
        (heading matched by text, any level; created at end if absent). The safe
        primitive for append-only logs (## Evidence log, ## Recent activity) and
        adding a section (## Postmortem) — preserves all existing content. Bumps
        version, logs, review-gates."""
        result = _append_section(path, heading, text)
        if _write_actor == "cron:page-quality-enrich" and not result.startswith(
            ("rejected:", "refused:", "error:")
        ):
            return (
                f"{result}\nWhen all requested sections are complete, return ONLY "
                "the exact fenced okengine-receipt printed in the prompt. Do not "
                "invent a summary object or add prose."
            )
        return result

    # Human review DECISIONS are deliberately NOT MCP tools (okengine#661). resolve/assign reach
    # _resolve_review/_assign_review only through the review-only HTTP sidecar
    # (OKENGINE_WRITE_REVIEW_ONLY=1, _review_http_app) and the `framework review` CLI. On this
    # model-facing surface the stdio caller is `admin` and `reviewer` is free text, so a lane
    # could clear its own flags with human-looking approvals. Only the machine-evidence hook
    # below, which cannot clear human-required state, stays exposed.
    @mcp.tool()
    def record_machine_review(path: str, evaluator: str, outcome: str, note: str = "") -> dict:
        """Attach a machine evidence check without clearing human-required review state."""
        return _record_machine_review(path, evaluator, outcome, note)

    @mcp.tool()
    def converge_entity(
        path: str,
        frontmatter_yaml: str,
        body: str = "",
        pack: str = "",
        remove: str = "",
        expected_sha256: str = "",
    ) -> str:
        """Upsert a page by its IDENTITY (not its path). The id is taken from the
        frontmatter `id` or derived (an external-authority id when the type binds
        one, else a minted slug). If a LIVE page already carries this id, MERGE
        into it under page+field ownership: the owning pack may change any field; a
        non-owner may ADD new keys or change only fields it is granted; conflicts
        are flagged, never clobbered. If the id is new, create + claim it.
        Authority ids converge across packs; minted-slug collisions are flagged,
        never auto-merged. A write to a tombstoned id is refused (never resurrect).
        `pack` is deployment-pinned (OKENGINE_PACK) — omit it; a differing value is refused.
        `remove` is a comma-separated list of fields to drop — permitted only for
        fields the caller owns (a non-owner removal is flagged, not applied)."""
        if _write_actor == "cron:entity-backfill":
            # The selector embeds source frontmatter as evidence. Local models
            # occasionally copy that page's `sources:*` identity into the new
            # entity payload, which creates a guaranteed cross-namespace ID
            # collision. A source ID is never a valid entity authority ID; drop
            # only that impossible value and let converge mint the entity's
            # canonical identity from its title/path.
            sanitized, error = _entity_backfill_frontmatter(path, frontmatter_yaml, body)
            if error:
                return error
            frontmatter_yaml = sanitized or ""
        result = _converge(path, frontmatter_yaml, body, pack, remove, expected_sha256)
        if _write_actor == "cron:entity-backfill":
            if result.startswith(("rejected:", "refused:", "error:")):
                return (
                    f"{result}\nTERMINAL: do not retry identical arguments. "
                    "Emit the required fenced receipt now with disposition rejected "
                    "and this concrete reason."
                )
            return (
                f"{result}\nSUCCESS: do not call any tool again. Emit ONLY the "
                "exact fenced okengine-receipt from the prompt now."
            )
        return result

    _write_actor = os.environ.get("OKENGINE_WRITE_ACTOR")

    if _write_actor == "cron:raw-backfill":

        @mcp.tool()
        def converge_source(
            path: str,
            frontmatter_yaml: Union[str, dict],
            body: str = "",
            pack: str = "",
            expected_sha256: str = "",
        ) -> str:
            """Create or update exactly one source page from the selected raw
            document. `path` must be beneath `sources/`; entity, concept,
            prediction, log, finding, and review paths are rejected."""
            rel = str(path).strip().lstrip("/")
            if rel.startswith("wiki/"):
                rel = rel[5:]
            if not rel.startswith("sources/"):
                return "rejected: raw-backfill may write only sources/<...> pages"
            fm, error = _raw_backfill_frontmatter(frontmatter_yaml)
            if error:
                return error
            # The helper's contract returns a mapping exactly when error is absent.
            fm = cast(dict, fm)
            selected, raw_ref = _raw_manifest_selection()
            if not raw_ref:
                return (
                    "rejected: raw-backfill requires exactly one existing raw/ capture "
                    "in its selection manifest"
                )
            # Runner-owned selection evidence is authoritative. Never accept a model-supplied
            # backlink that could cite an unrelated capture or leave the processed item unmarked.
            fm["raw"] = raw_ref
            url = str(fm.get("url") or "").strip()
            if not url:
                url = _selected_raw_url(selected)
                if url:
                    fm["url"] = url
            if not url:
                return "rejected: source url is required"
            identity_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
            fm["id"] = f"sources:url-{identity_hash[:20]}"
            requested = Path(rel)
            suffix = requested.suffix or ".md"
            stem = requested.stem
            canonical = requested.with_name(f"{stem}-{identity_hash[:8]}{suffix}").as_posix()
            result = _converge(
                canonical,
                yaml.safe_dump(fm, sort_keys=False, allow_unicode=True),
                body,
                pack,
                "",
                expected_sha256,
            )
            if result.startswith(("rejected:", "refused:", "error:")):
                return (
                    f"{result}\nTERMINAL: do not retry identical arguments. "
                    "Emit the required fenced receipt now with disposition rejected "
                    "and this concrete reason."
                )
            return (
                f"{result}\nSUCCESS: do not call any tool again. Emit ONLY the "
                "exact fenced okengine-receipt from the prompt now."
            )

    if _write_actor == "cron:concept-backfill":

        @mcp.tool()
        def converge_concept(
            path: str,
            frontmatter_yaml: str = "",
            body: str = "",
            pack: str = "",
            expected_sha256: str = "",
        ) -> str:
            """Create or update exactly one concept page grounded in the
            selected source. `path` must be beneath `concepts/`."""
            rel = str(path).strip().lstrip("/")
            if rel.startswith("wiki/"):
                rel = rel[5:]
            if not rel.startswith("concepts/"):
                return "rejected: concept-backfill may write only concepts/<...> pages"
            try:
                fm = yaml.safe_load(frontmatter_yaml) or {}
            except yaml.YAMLError as exc:
                return f"rejected: invalid frontmatter YAML: {exc}"
            if not isinstance(fm, dict):
                return "rejected: frontmatter_yaml must decode to a mapping"
            fm["type"] = "concept"
            supplied_id = str(fm.get("id") or "").strip()
            requested_slug_id = f"concepts:{Path(rel).stem}"
            if (
                supplied_id.startswith("sources:")
                or supplied_id.startswith("concepts:")
                and supplied_id != requested_slug_id
            ):
                fm.pop("id", None)
            fm.setdefault(
                "title",
                Path(rel).name.replace("-", " ").strip().title(),
            )
            result = _converge(
                path,
                yaml.safe_dump(fm, sort_keys=False, allow_unicode=True),
                body,
                pack,
                "",
                expected_sha256,
            )
            if result.startswith(("rejected:", "refused:", "error:")):
                return (
                    f"{result}\nTERMINAL: do not retry identical arguments. "
                    "Emit the required fenced receipt now with disposition rejected "
                    "and this concrete reason."
                )
            return (
                f"{result}\nSUCCESS: do not call any tool again. Emit ONLY the "
                "exact fenced okengine-receipt from the prompt now."
            )

    # Backfill actors have deliberately narrow operation contracts. Policy still guards every
    # call server-side, but hiding unrelated schemas stops local models from spending turns
    # probing/retrying tools they cannot use. ENGINE lanes come from _ACTOR_TOOLS; a PACK lane
    # declares `write_tools: [...]` on its cron def and ensure-runtime forwards it as
    # OKENGINE_WRITE_TOOLS on that lane's server-bound writer (okengine#664 — the table used to
    # carry a private deployment's cron name, which the publish guard aborts on). Once the env is
    # set it is the single runtime source, for engine and pack lanes alike.
    _actor_name = os.environ.get("OKENGINE_WRITE_ACTOR")
    _env_tools = os.environ.get("OKENGINE_WRITE_TOOLS", "")
    _allowed_actor_tools: set[str] | None
    if _env_tools.strip():
        _allowed_actor_tools = {t.strip() for t in _env_tools.split(",") if t.strip()}
    else:
        _allowed_actor_tools = _ACTOR_TOOLS.get(_actor_name) if _actor_name else None
    if _allowed_actor_tools is not None:
        for _tool in list(mcp._tool_manager._tools):
            if _tool not in _allowed_actor_tools:
                mcp.remove_tool(_tool)

except ImportError:  # pragma: no cover - mcp absent (e.g. host test env)
    mcp = None


# Keep in sync with okengine-mcp/server.py DEFAULT_LOCAL_TOKEN / _LOOPBACK (the read server). The
# well-known local token is PUBLIC (it ships in the source); the enforced WRITE path must never serve
# it beyond loopback (invariant-audit CRITICAL — the read server fails closed on exactly this, but
# write_server only checked the token was non-empty, so a networked write bound off-loopback with the
# seeded compose default served UNAUTHENTICATED full create/update/converge/tombstone access).


if __name__ == "__main__":  # pragma: no cover - entry guard, also in exclude_also
    if mcp is None:
        raise SystemExit("mcp package not installed; cannot run the server")
    transport = os.environ.get("OKENGINE_WRITE_TRANSPORT", "stdio")
    if transport in ("streamable-http", "http"):
        # Networked write surface for out-of-process sidecars. Requires a scoped or admin token;
        # refuses the built-in default off-loopback unless explicitly allowed (mirrors server.py).
        import uvicorn

        host = os.environ.get("OKENGINE_WRITE_HOST", "127.0.0.1")
        admin = _resolve_write_auth(os.environ, host)
        inner = (
            _review_http_app()
            if os.environ.get("OKENGINE_WRITE_REVIEW_ONLY") == "1"
            else mcp.streamable_http_app()
        )
        app = _ScopedWriteAuth(inner, admin)
        uvicorn.run(app, host=host, port=int(os.environ.get("OKENGINE_WRITE_PORT", "8731")))
    else:
        mcp.run(transport="stdio")
