from __future__ import annotations
# ruff: noqa: F821

from okengine.corpus_transaction import touch as _corpus_touch

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
import output_contract_enforce as _output_contract

import id_lib, schema_lib, id_index, converge, okf_migrate
from okengine.write_services.convergence import _actor_admission_reject
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _create(
    path: str,
    frontmatter_yaml: Union[str, dict],
    body: str = "",
    _contract_operation: str = "create",
) -> str:
    p = _safe(path)
    if p is None:
        return (
            f"refused: unsafe wiki path (must stay inside wiki/; basename must contain no "
            f"whitespace and entity basenames must be ≤{_MAX_ENTITY_SLUG_LEN} characters)"
        )
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if p.exists():
        return f"refused: {_rel(p)} already exists — use update_entity"
    fm = _coerce_fm(frontmatter_yaml, p)
    if fm is None:
        return _fm_reject(frontmatter_yaml)
    receipt_reject = _source_receipt_refuse(fm, p)
    if receipt_reject:
        return receipt_reject
    # Route every partitioned namespace through the shared canonical writer
    # contract before the existence, authorization, and schema gates.  A caller
    # may supply a logical flat key; the physical write must never create a
    # flat-vs-sharded duplicate (#262).
    canonical_p = _partitioned_create_path(p, fm)
    if canonical_p != p:
        p = canonical_p
        redirected_reserved = _reserved_refuse(p)
        if redirected_reserved:
            return redirected_reserved
        _wa = _wauth_refusal(p)
        if _wa:
            return _wa
        if p.exists():
            return f"refused: {_rel(p)} already exists — use update_entity"
    cap = _capability_reject(
        p,
        "create",
        page_type=str(fm.get("type") or ""),
        changed_fields=_frontmatter_changes(p, {}, fm),
        body_change="replace" if body else "none",
        values=fm,
    )
    if cap:
        if _contract_operation == "converge":  # okengine#993: a new-id converge needs `create`
            cap = cap.replace("operation 'create' is not allowed",
                              "operation 'create' is not allowed (converging a new id is a create)")
        return f"rejected: {cap}"
    # Curated-authority attestation is operator-owned: no MCP caller may introduce it (okengine#980).
    attestation = _attestation_reject(p, {}, fm, bool(body))
    if attestation:
        return attestation
    # Underscore type spellings are the observed taxonomy-bypass class
    # (`threat_actor` beside canonical `threat-actor`/`actor`). Permit one only
    # when the governing schema explicitly declares it as a type or type_alias.
    ptype = str(fm.get("type") or "").strip()
    if "_" in ptype:
        try:
            schema = _governing(p)
            allowed_types = schema_lib.canonical_types(schema) | set(
                schema_lib.type_aliases(schema)
            )
        except Exception:
            allowed_types = set()
        if ptype not in allowed_types:
            return f"rejected: type {ptype!r} is not declared by the governing schema"
    # Enforce the page lands in a schema-declared namespace (no stray-namespace fork, #115).
    nsr = _namespace_reject(p)
    if nsr:
        return f"rejected: {nsr}"
    # ... and in the RIGHT declared namespace for its type (no type: source under concepts/, #276).
    tnr = _type_namespace_reject(p, fm)
    if tnr:
        return f"rejected: {tnr}"
    fsr = _fabricated_source_reject(
        p, fm
    )  # a cited source must exist — no fabricated `source/…` (#348)
    if fsr:
        return fsr
    msr = _missing_source_reject(p, fm)
    if msr:
        return msr
    alr = _alias_shape_reject(p, fm)  # an alias is a name, not a URL/prose/list (#845)
    if alr:
        return alr
    fdr = _future_date_reject(fm)
    if fdr:
        return f"rejected: {fdr}"
    blr = _briefing_link_reject(p, body)
    if blr:
        return f"rejected: {blr}"
    bir = _body_integrity_reject("", body)
    if bir:
        return f"rejected: {bir}"
    fm, drift = _normalize_drift(fm, p)  # converge on schema vocab (okengine#46)
    admission_reject = _actor_admission_reject(fm, body, p)
    if admission_reject:
        return admission_reject
    # Server stamps version/last_updated if absent, and an IMMUTABLE `created` on first write
    # (the OKF-envelope creation date = when the page was ingested; unlike last_updated it never
    # shifts on later edits, so "recent ingest" / age reporting is accurate).
    if "version" not in fm:
        fm["version"] = 1
    if "created" not in fm:
        fm["created"] = _now()
    if "last_updated" not in fm:
        fm["last_updated"] = _now()
    _stamp_maintainer(fm, creation=True)  # composition provenance (okengine#90 P3)
    # Provenance (okengine#132/#133): stamp the owning extension id when a networked
    # extension caller writes — the key disable/orphan/purge reads. Server-side, derived
    # from the scoped token, so a client can't spoof it (strips any supplied value first).
    _apply_extension_provenance(fm, creating=True)
    review_invalidation = _apply_review_governance(fm)
    # Ensure every page carries a human `name`. The ingest agent (esp. source ingest:
    # select_raw_batch -> agent -> okengine-write) puts the article title in the body's
    # `# H1` but doesn't always set a `name`/`title` field, leaving the page nameless in
    # the reader/backlinks/search. Derive `name` from the first true H1 when absent —
    # only when BOTH name and title are missing, so a curated name is never overridden,
    # and after id derivation, so the minted slug stays filename-based.
    if not str(fm.get("name") or fm.get("title") or "").strip():
        _h1 = _H1.search(body or "")
        if _h1:
            fm["name"] = _h1.group(1).strip()
    _enum_case_coerce(p, fm)
    isr = _int_shape_reject(p, fm)
    if isr:
        return f"rejected: {isr}"
    itr = _item_shape_reject(p, fm)
    if itr:
        return f"rejected: {itr}"
    pol = _policy_reject(p, fm, "create")
    if pol:
        return f"rejected: {pol}"
    flags = (
        review_invalidation
        + drift
        + _review_flags(p, fm, prev=None)
        + _identity_contradiction_flags(p, fm)
        + _unresolvable_link_flags(p, body)
        + _degeneration_flags(body)
        + _chat_review_flags()
    )
    contract_reject = _contract_reject(p, _contract_operation, fm, body, drift)
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        fm["needs_review"] = True
    # Stamp the content-derived id before schema validation (the OKF envelope
    # requires it). Duplicate routing itself stays after validation below.
    if _CONVERGE_OK:
        try:
            pid, _ = _page_id_and_kind(fm, _governing(p), _namespace(p), p.stem)
            if pid:
                fm["id"] = pid
        except Exception:
            pass
    content = _compose(fm, body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"
    # Dedup runs only AFTER the complete candidate passes schema validation. The
    # old order let an invalid type alias-match and converge before the validator
    # saw it. This stamps fm["id"], so recompose/revalidate the final new page.
    dedup = _dedup_on_create(path, p, fm, body, creating=_contract_operation == "create")
    if dedup is not None:
        return dedup
    content = _compose(fm, body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"
    # Judged only once dedup has decided this IS a new page: a create that resolves onto an
    # existing page is a merge, and the converge path judges a retype there (okengine#1012).
    grounding = _actor_grounding_reject(p, fm)
    if grounding:
        return grounding
    p.parent.mkdir(parents=True, exist_ok=True)
    _corpus_touch(p)
    _atomic_write_text(p, content)
    # Write-synchronous id claim: the new page is now resolvable by id, so a later
    # cosmetic-variant write dedupes against it instead of forking a canonical.
    if _CONVERGE_OK and isinstance(fm.get("id"), str):
        try:
            registry = _registry()
            registry.by_id[fm["id"]] = _rel(p)
            registry._add_slug_identity(_qualified_namespace(p), _rel(p), p.stem)
        except Exception:  # pragma: no cover - registry is best-effort
            pass
    # Write-synchronous name/alias claim (okengine#324): keep _alias_hits' index current WITHIN this
    # process so two matching entity pages created back-to-back dedup against each other (the pre-#324
    # rglob saw the just-written page on disk; the index must too). Tombstoned pages excluded.
    if (
        _CONVERGE_OK
        and _namespace(p) == "entities"
        and str(fm.get("status") or "").lower() != "tombstoned"
    ):
        try:
            _registry()._add_identity(_rel(p), fm)
        except Exception:  # pragma: no cover - registry is best-effort
            pass
    ver = fm.get("version", 1)
    _append_log(f"- {_today()} mcp-write create {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"created {_rel(p)} v{ver}{note}"


def _canonical_view(p: Path, fm: dict) -> dict:
    """`fm` in the write path's canonical shape (`_coerce_fm`), for the checks that compare a write
    with the stored page (okengine#994 [19]). Both sides are read this way: a field the write did
    not touch still holds its legacy shape, and a field it did touch now holds the canonical one,
    so comparing a raw side with a canonical side reads a reshape as a change (a removed alias, a
    new source). Raw on the one case `_coerce_fm` refuses (an unpaired surrogate)."""
    canonical = _coerce_fm(dict(fm), p)
    return fm if canonical is None else canonical


def _record_alias_removals_canonically(new_fm: dict, new_view: dict, prev_view: dict) -> None:
    """`_record_alias_removals` judged on the canonical views, its one output field written back."""
    if _record_alias_removals(new_view, prev_view):
        new_fm[schema_lib.REJECTED_ALIASES_FIELD] = new_view[schema_lib.REJECTED_ALIASES_FIELD]


def _preserve_immutable(new_fm: dict, cur_fm: dict, p: Path) -> list:
    """Force any immutable identity/provenance field that ALREADY EXISTS back to its stored value,
    overriding a caller change — so a read-modify-write that echoes the whole frontmatter is fine,
    but a forged CHANGE is silently reverted. A field the page LACKS may still be set (backfilling an
    id/created onto a legacy or non-compliant page is legitimate — and, since base-schema makes `id`
    required, necessary). The invariant is 'never CHANGES', not 'never appears'. The canonical shape
    of the stored value (`discovered_by: pack` as `[pack]`) is the same value, not a change: it is
    kept, unflagged, so a legacy shape is repairable (okengine#994 [19]). Returns the fields whose
    change was reverted, so the caller can flag the attempt for review."""
    reverted = []
    for key in _IMMUTABLE_KEYS:
        if key in cur_fm:  # exists -> immutable: restore (no-op if unchanged)
            if key not in new_fm or not _same_value(new_fm[key], cur_fm[key]):
                if key in new_fm and _echo_kind(p, key, cur_fm[key], new_fm[key]) == "reshape":
                    continue  # the canonical shape of the stored value: kept
                reverted.append(key)
            new_fm[key] = cur_fm[key]
    return reverted


def _write_precondition(path: Path, expected_sha256: str) -> str:
    if not expected_sha256:
        return ""
    observed = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if not hmac.compare_digest(observed, expected_sha256):
        return (
            f"deferred: concurrent mutation of {_rel(path)} "
            f"(expected {expected_sha256}, observed {observed})"
        )
    return ""


def _update(
    path: str,
    frontmatter_yaml: Union[str, dict, None] = None,
    body: Optional[str] = None,
    expected_sha256: str = "",
) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if not p.is_file():
        return f"refused: {_rel(p)} does not exist — use create_entity"
    utf8 = _utf8_refusal(p)
    if utf8:
        return utf8
    precondition = _write_precondition(p, expected_sha256)
    if precondition:
        return precondition
    ferr = _frontmatter_error(p)
    if ferr:
        return f"rejected: {ferr} — repair the frontmatter before updating"
    cur_fm, cur_body = _read_page(p)
    tr = _tombstone_refuse(cur_fm, p)  # never resurrect a tombstoned page (invariant-audit M18)
    if tr:
        return tr
    new_fm = dict(cur_fm)
    patch: dict = {}
    judged: dict = {}
    if frontmatter_yaml is not None:
        coerced_patch = _coerce_fm(frontmatter_yaml, p)
        if coerced_patch is None:
            return _fm_reject(frontmatter_yaml)
        # A JSON or coerced echo of a stored value is judged no change (okengine#991); a reshaped
        # echo still STORES the canonical shape, so a legacy shape is repairable (okengine#994).
        judged = _stored_echoes(p, cur_fm, coerced_patch)
        patch = _stored_repairs(p, cur_fm, coerced_patch)
        # Future-date guard on ONLY the fields this patch supplies: a legacy page that already
        # carries a bad future date must stay fixable by an update that doesn't touch dates.
        fdr = _future_date_reject(patch, fields=tuple(k for k in _RECORD_DATE_FIELDS if k in patch))
        if fdr:
            return f"rejected: {fdr}"
        new_fm.update(patch)
    receipt_reject = _source_receipt_refuse(new_fm, p)
    if receipt_reject:
        return receipt_reject
    cap = _capability_reject(
        p,
        "update",
        page_type=str(new_fm.get("type") or cur_fm.get("type") or ""),
        # A read-modify-write echoes the whole frontmatter: a key whose value is unchanged is no
        # mutation, so it is not a protected-field change either. Compared alias-renamed and
        # type-strictly, so `0` over `false` is a change (okengine#983).
        changed_fields=_frontmatter_changes(p, cur_fm, {**cur_fm, **judged}),
        body_change="replace" if body is not None else "none",
        values={**cur_fm, **patch}, stored=cur_fm,
    )
    if cap:
        return f"rejected: {cap}"
    attestation = _attestation_reject(  # okengine#980: operator-owned attestation
        p, cur_fm, new_fm, body is not None and body != cur_body
    )
    if attestation:
        return attestation
    ttr = _tombstone_transition_refuse(cur_fm, new_fm, p)  # okengine#863: retire only via tombstone_entity
    if ttr:
        return ttr
    # extension_id is server-derived: strip any client forge, keep the create-time stamp (M14).
    _apply_extension_provenance(
        new_fm,
        creating=False,
        existing_ext_id=cur_fm.get("extension_id"),
        existing_producer_lane=cur_fm.get("producer_lane"),
    )
    # id + created/created_by/discovered_by are immutable — revert any caller change (audit HIGH #3).
    # The checks below compare the write with the stored page on canonical views of both, so an
    # echo or a repair of a legacy shape is not a removed alias, a new source or a review-worthy
    # change (okengine#994 [19]).
    prev_view = _canonical_view(p, cur_fm)
    reverted_immutable = _preserve_immutable(new_fm, cur_fm, p)
    identity_flags, hold_body, _held = _identity_hold(p, cur_fm, new_fm)  # okengine#1028
    if hold_body and body is not None and body.strip() != cur_body.strip():
        identity_flags.append(_IDENTITY_BODY_HELD)
        body = None
    review_invalidation = _apply_review_governance(new_fm, prev_view)
    new_fm, drift = _normalize_drift(new_fm, p)  # converge on schema vocab (okengine#46)
    new_body = cur_body if body is None else body
    admission_reject = (_actor_admission_reject(new_fm, new_body, p, cur_fm)
                        or _actor_grounding_reject(p, new_fm, cur_fm))  # okengine#1012
    if admission_reject:
        return admission_reject
    if body is not None:  # only when this update REWRITES the body
        blr = _briefing_link_reject(p, new_body)
        if blr:
            return f"rejected: {blr}"
        bir = _body_integrity_reject(cur_body, new_body)
        if bir:
            return f"rejected: {bir}"
    # Bump version, stamp last_updated.
    try:
        new_fm["version"] = int(new_fm.get("version", 1)) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()
    _stamp_maintainer(new_fm, creation=False)  # add this pack as a maintainer (okengine#90 P3)
    _enum_case_coerce(p, new_fm)
    isr = _int_shape_reject(p, new_fm)
    if isr:
        return f"rejected: {isr}"  # existing file left untouched
    itr = _item_shape_reject(p, new_fm)
    if itr:
        return f"rejected: {itr}"  # existing file left untouched
    new_view = _canonical_view(p, new_fm)
    pol = _policy_reject(p, new_view, "update", prev=prev_view)
    if pol:
        return f"rejected: {pol}"  # existing file left untouched
    tnr = _type_ns_reject_on_change(
        p, new_view, prev_view
    )  # type can't drift out of its home ns (audit)
    if tnr:
        return f"rejected: {tnr}"  # existing file left untouched
    fsr = _fabricated_source_reject(
        p, new_view, prev_fm=prev_view
    )  # block NEW fabricated source refs (#348)
    if fsr:
        return fsr  # existing file left untouched
    msr = _missing_source_reject(p, new_view, prev_fm=prev_view)
    if msr:
        return msr
    alr = _alias_shape_reject(p, new_view, prev_fm=prev_view)  # #845
    if alr:
        return alr
    _record_alias_removals_canonically(new_fm, new_view, prev_view)  # a review-removed alias stays out (TAA#48)
    flags = (
        review_invalidation
        + identity_flags
        + drift
        + _review_flags(p, new_view, prev=prev_view)
        + _identity_contradiction_flags(p, new_fm)
        + (
            _unresolvable_link_flags(p, new_body) + _degeneration_flags(new_body)
            if body is not None
            else []
        )
        + (
            [f"immutable field change reverted: {', '.join(reverted_immutable)}"]
            if reverted_immutable
            else []
        )
        + _chat_review_flags()
    )
    contract_reject = _contract_reject(
        p, "update", new_fm, new_body, drift, body_links_changed=body is not None
    )
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, new_body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"  # existing file left untouched
    precondition = _write_precondition(p, expected_sha256)
    if precondition:
        return precondition
    _corpus_touch(p)
    _atomic_write_text(p, content)
    if _namespace(p) == "entities":  # #962: aliases a write adds feed the next merge's hijack check
        converge.record_identity(_registry(), _rel(p), new_fm)
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write update {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"updated {_rel(p)} v{ver}{note}"


def _tombstone(path: str, reason: str, superseded_by: Optional[str] = None) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    if not p.is_file():
        return f"refused: {_rel(p)} does not exist — nothing to tombstone"
    utf8 = _utf8_refusal(p)
    if utf8:
        return utf8
    ferr = _frontmatter_error(p)  # invariant-audit M18: malformed YAML -> refuse, don't wipe it
    if ferr:
        return f"refused: {ferr} — fix the page's frontmatter before tombstoning (would silently wipe it)"
    cur_fm, cur_body = _read_page(p)
    succ_reject = _tombstone_successor_reject(p, cur_fm, reason, superseded_by)  # #101
    if succ_reject:
        return succ_reject  # file left untouched
    if superseded_by:
        successor = _safe(superseded_by)
        if successor and successor.is_file():
            successor_fm, _ = _read_page(successor)
            receipt_reject = _source_receipt_refuse(successor_fm, successor)
            if receipt_reject:
                return (
                    f"rejected: superseded_by target is a duplicate receipt, not canonical "
                    f"intelligence — {_rel(p)} left active"
                )
    changed = {"status", "tombstone_reason", "last_updated", "version"}
    if superseded_by:
        changed.add("superseded_by")
    new_fm = dict(cur_fm)
    new_fm["status"] = "tombstoned"
    new_fm["tombstone_reason"] = reason
    if superseded_by:
        new_fm["superseded_by"] = superseded_by
    cap = _capability_reject(
        p, "tombstone", page_type=str(cur_fm.get("type") or ""), changed_fields=changed,
        values=new_fm, stored=cur_fm,  # judged against forbidden_values (okengine#993)
    )
    if cap:
        return f"rejected: {cap}"
    # a tombstone IS an update — it must clear the same namespace permission
    # matrix as every other mutation (found via okengine#166: an agent-read-only
    # lookup/ namespace could still be tombstoned through this one path)
    pol = _policy_reject(p, cur_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"  # file left untouched
    _apply_extension_provenance(
        new_fm,
        creating=False,
        existing_ext_id=cur_fm.get("extension_id"),
        existing_producer_lane=cur_fm.get("producer_lane"),
    )
    try:
        new_fm["version"] = int(new_fm.get("version", 1)) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()
    contract_reject = _contract_reject(p, "tombstone", new_fm, cur_body, [])
    if contract_reject:
        return f"rejected: {contract_reject}"
    content = _compose(new_fm, cur_body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"  # file left untouched
    _corpus_touch(p)
    _atomic_write_text(p, content)
    # Keep the in-process id registry write-synchronous — like create/converge mutate reg.by_id —
    # so the converge "never resurrect a tombstoned id" guard sees THIS tombstone within the same
    # server process, not only after a cold rebuild. Without it a tombstone-then-converge in one
    # process resurrected the page (invariant-audit HIGH). Best-effort: the registry is a cache
    # that self-heals on the next build(); a mint/schema-lib gap must never block the tombstone.
    try:
        pid, _kind = _page_id_and_kind(cur_fm, _governing(p), _namespace(p), p.stem)
        if pid:
            reg = _registry()
            reg.tombstoned.add(pid)
            reg.by_id.setdefault(pid, _rel(p))
    except Exception:
        pass
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write tombstone {_rel(p)} v{ver} — {reason}")
    return f"tombstoned {_rel(p)} v{ver} (file retained, not deleted)"


def _non_reviewable_flag(p: Path, note: str) -> bool:
    """True for non-page infrastructure failures and routine run receipts.

    A real page can legitimately be flagged for content that mentions an error, so this deflection
    is intentionally gated on the target not existing. Human review is page-centric: when there is
    no page to open and the note describes machinery or a completed/no-op run, the durable ledger
    is the truthful destination and a queue row would be un-actionable.
    """
    infrastructure = re.search(
        r"(?:\b(?:script|command|cron|ingest|collection|data[- ]collection|scanner)\b.{0,80}"
        r"\b(?:failed|failure|error|exited|exit code)\b|"
        r"\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\s*\d+\b|"
        r"\b(?:directory|path|folder)\b.{0,80}\b(?:does not exist|missing|not found)\b|"
        r"\b(?:does not exist|missing|not found)\b.{0,80}\b(?:directory|path|folder)\b|"
        r"(?:^|\s)[\"']?/(?:[^\s\"']+/)*[^\s\"']+[’\"']?\s+"
        r"(?:does not exist|is missing|was not found)\b)",
        note, re.I,
    )
    receipt = re.search(
        r"(?:\bno\s+.+\s+needed\s+this\s+run\b|"
        r"\b(?:scan|batch|run|job)\s+(?:is\s+)?(?:complete|completed|finished)\b|"
        r"\bnot\s+meant\s+to\s+be\s+manually\s+reviewed\b|"
        r"\ball\s+.+\s+already\s+(?:scored|processed|reviewed|present)\b)",
        note, re.I,
    )
    return not p.is_file() and bool(infrastructure or receipt)


def _existing_page_type(p: Path) -> str:
    """The stored `type` of an existing page, for capability checks that do not change it
    (okengine#885). Flag and review checks passed no type at all, so any lane with a concrete type
    list was refused with "page type '' is not allowed" even when the operation was granted. A
    missing or unreadable page has no type; the tool's own existence/frontmatter checks answer."""
    try:
        if p.is_file():
            fm, _body = _read_page(p)
            return str(fm.get("type") or "")
    except Exception:  # noqa: BLE001 -- the caller's own frontmatter guard reports the defect
        return ""
    return ""


def _flag(path: str, note: str) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    cap = _capability_reject(p, "flag", page_type=_existing_page_type(p))
    if cap:
        return f"rejected: {cap}"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    if p.is_file():
        utf8 = _utf8_refusal(p)
        if utf8:
            return utf8
        ferr = _frontmatter_error(p)
        if ferr:
            return f"refused: {ferr} — fix the page's frontmatter before flagging"
        cur_fm, cur_body = _read_page(p)
    else:
        cur_fm, cur_body = {}, ""
    contract_reject = _contract_reject(p, "flag", cur_fm, cur_body, [])
    if contract_reject:
        return f"rejected: {contract_reject}"
    clean_note = " ".join((note or "").split())
    if _non_reviewable_flag(p, clean_note):
        _append_log(f"- {_today()} mcp-write operational-note {_rel(p)} — {clean_note}")
        return f"recorded operational note for {_rel(p)} in log.md — not queued for human review"
    created = _append_review_queue_once(p, clean_note)
    action = "flag" if created else "flag already-queued"
    _append_log(f"- {_today()} mcp-write {action} {_rel(p)} — {clean_note}")
    if created:
        return f"flagged {_rel(p)} for review — queued in _review-queue.md"
    return f"already flagged {_rel(p)} for review — queue unchanged"


def _field_loss(prev_fm: dict, new_fm: dict) -> Optional[str]:
    """Reject an edit that DROPS a frontmatter key present before (server-stamped
    keys excepted). Value changes and additions are fine — only deletions block."""
    dropped = sorted(k for k in (prev_fm or {}) if k not in (new_fm or {}) and k not in _STAMP_KEYS)
    if dropped:
        return (
            "edit would drop existing frontmatter field(s): "
            + ", ".join(dropped)
            + " — curated fields must be preserved"
        )
    return None


def _stamp(new_fm: dict, cur_fm: dict) -> None:
    try:
        new_fm["version"] = int(new_fm.get("version", cur_fm.get("version", 1))) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()
