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
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _patch(path: str, old_string: str, new_string: str) -> str:
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
    if not old_string:
        return "rejected: old_string is empty"
    if old_string == new_string:
        return "rejected: old_string and new_string are identical"
    text = p.read_text(encoding="utf-8")
    n = text.count(old_string)
    if n == 0:
        return "rejected: old_string not found in the page (verify with a read first)"
    if n > 1:
        return (
            f"rejected: old_string matches {n} places — add surrounding context to make it unique"
        )
    cur_fm, _cur_body = _read_page(p)
    tr = _tombstone_refuse(cur_fm, p)  # invariant-audit M18
    if tr:
        return tr
    new_text = text.replace(old_string, new_string, 1)
    m = _FM.match(new_text)
    if not m:
        return "rejected: edit would remove or corrupt the YAML frontmatter"
    try:
        new_fm = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        return f"rejected: edit produced invalid frontmatter YAML: {str(e)[:120]}"
    if not isinstance(new_fm, dict):
        return "rejected: edit produced non-mapping frontmatter"
    changed_fields = {
        key for key in set(cur_fm) | set(new_fm) if cur_fm.get(key) != new_fm.get(key)
    }
    old_match = _FM.match(text)
    old_body = old_match.group(2).lstrip("\n") if old_match else ""
    candidate_body = m.group(2).lstrip("\n")
    cap = _capability_reject(
        p,
        "patch",
        page_type=str(new_fm.get("type") or cur_fm.get("type") or ""),
        changed_fields=changed_fields,
        body_change="replace" if candidate_body != old_body else "none",
    )
    if cap:
        return f"rejected: {cap}"
    # patch_entity is a full write chokepoint: apply the SAME shape coercion _create/_update/_converge
    # do (#196 — a schema-declared list field authored as a scalar becomes a list; bare [[wikilink]]
    # values are stripped), so an edit can't land a malformed shape that poisons a downstream lane.
    # new_fm was parsed and type-checked as a mapping immediately above, so this call cannot
    # take _coerce_fm's invalid-YAML/shape None branch.
    new_fm = cast(dict, _coerce_fm(new_fm, p))
    fl = _field_loss(
        cur_fm, new_fm
    )  # compare in the pre-normalized space (cur_fm is un-normalized)
    if fl:
        return f"rejected: {fl}"
    # patch is a full write chokepoint — converge on the schema vocabulary (okengine#46) exactly like
    # create/update/converge, or an aliased field/value introduced by a surgical edit lands raw and
    # forks the vault silently (invariant-audit). After field-loss so a rename isn't seen as a drop.
    new_fm, drift = _normalize_drift(new_fm, p)
    # extension_id is server-derived: strip any patched-in forge, keep the create-time stamp (M14).
    _apply_extension_provenance(
        new_fm,
        creating=False,
        existing_ext_id=cur_fm.get("extension_id"),
        existing_producer_lane=cur_fm.get("producer_lane"),
    )
    # id + created/created_by/discovered_by are immutable — revert any patched-in change (audit HIGH #3).
    reverted_immutable = _preserve_immutable(new_fm, cur_fm)
    review_invalidation = _apply_review_governance(new_fm, cur_fm)
    _enum_case_coerce(p, new_fm)
    isr = _int_shape_reject(p, new_fm)
    if isr:
        return f"rejected: {isr}"
    itr = _item_shape_reject(p, new_fm)
    if itr:
        return f"rejected: {itr}"
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"
    tnr = _type_ns_reject_on_change(
        p, new_fm, cur_fm
    )  # type can't drift out of its home ns (audit)
    if tnr:
        return f"rejected: {tnr}"
    receipt_reject = _source_receipt_refuse(new_fm, p)
    if receipt_reject:
        return receipt_reject
    fsr = _fabricated_source_reject(p, new_fm, prev_fm=cur_fm)
    if fsr:
        return fsr
    msr = _missing_source_reject(p, new_fm, prev_fm=cur_fm)
    if msr:
        return msr
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    bir = _body_integrity_reject(_cur_body, body)
    if bir:
        return f"rejected: {bir}"
    blr = _briefing_link_reject(p, body)  # briefings must have only resolvable links + a citation
    if blr:
        return f"rejected: {blr}"
    _stamp(new_fm, cur_fm)
    fd = _future_date_reject(new_fm)  # the boundary every writer crosses (invariant-audit)
    if fd:
        return f"rejected: {fd}"  # file left untouched
    # Same review gate as create/update: drift (aliased field/value), degenerate body, and dead
    # wikilinks must be attributable at THIS write, not only via a nightly report-only lint that
    # carries no write attribution (invariant-audit — patch bypassed all three).
    flags = (
        review_invalidation
        + drift
        + _review_flags(p, new_fm, prev=cur_fm)
        + _identity_contradiction_flags(p, new_fm)
        + _unresolvable_link_flags(p, body)
        + _degeneration_flags(body)
        + (
            [f"immutable field change reverted: {', '.join(reverted_immutable)}"]
            if reverted_immutable
            else []
        )
    )
    contract_reject = _contract_reject(p, "patch", new_fm, body, drift)
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"  # file left untouched
    _corpus_touch(p)
    _atomic_write_text(p, content)
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write patch {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"patched {_rel(p)} v{ver}{note}"


def _body_integrity_counts(body: str) -> tuple[int, Counter]:
    """Count structural defects outside fenced code blocks."""
    malformed = 0
    counts: Counter = Counter()
    fence: tuple[str, int] | None = None
    for line in (body or "").splitlines():
        marker = _FENCE_RE.match(line)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        if _MALFORMED_HEADING_RE.match(line):
            malformed += 1
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            name = match.group(2).strip().casefold()
            if name in _DERIVED_PANEL_HEADINGS:
                counts[name] += 1
    return malformed, counts


def _body_integrity_reject(previous: str, proposed: str) -> Optional[str]:
    """Reject newly introduced malformed or reader-derived H2s, while allowing legacy pages to be
    edited or repaired. Counts matter: adding a second copy is also an introduction."""
    old_bad, old_panels = _body_integrity_counts(previous)
    new_bad, new_panels = _body_integrity_counts(proposed)
    if new_bad > old_bad:
        return "body introduces malformed `## ##` heading — pass a plain section name"
    introduced = sorted(name for name, count in new_panels.items() if count > old_panels[name])
    if introduced:
        return (
            "body introduces reader-derived panel heading(s): "
            + ", ".join(introduced)
            + " — backlink/reference panels are computed and must not be authored"
        )
    return None


def _insert_into_section(body: str, heading: str, block: str) -> tuple[str, str]:
    """Append `block` at the end of the `## heading` section (matched by heading
    text, any level), before the next heading of the same-or-higher level. If the
    heading is absent, create the section at the end of the body."""
    lines = body.split("\n")
    # Normalize the heading argument: an agent may pass an already-`##`-prefixed name
    # (`## Recent activity`). The MATCH stripped `#`, but the section-CREATE path below
    # wrote `## {heading}` from the raw arg — double-prefixing to `## ## Recent activity`
    # (okengine#242, 38 corrupted pages fleet-wide). Strip here so create and match agree.
    heading = heading.strip().lstrip("#").strip()
    want = heading.lower()
    block = block.rstrip("\n")
    idx = None
    level = 0
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and m.group(2).strip().lower() == want:
            idx, level = i, len(m.group(1))
            break
    if idx is None:
        base = body.rstrip("\n")
        prefix = (base + "\n\n") if base else ""
        return f"{prefix}## {heading}\n\n{block}\n", "section created"
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    seg = lines[:end]
    while len(seg) > idx + 1 and seg[-1].strip() == "":
        seg.pop()
    tail = lines[end:]
    new_lines = seg + ["", block] + (([""] + tail) if tail else [""])
    return "\n".join(new_lines), "appended to existing section"


def _append_section(path: str, heading: str, text: str) -> str:
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
    if not (text or "").strip():
        return "rejected: text is empty"
    ferr = _frontmatter_error(p)  # invariant-audit M18: malformed YAML -> refuse, don't wipe it
    if ferr:
        return f"refused: {ferr} — fix the page's frontmatter before appending (would silently wipe it)"
    cur_fm, cur_body = _read_page(p)
    cap = _capability_reject(
        p, "append", page_type=str(cur_fm.get("type") or ""), body_change="append"
    )
    if cap:
        return f"rejected: {cap}"
    tr = _tombstone_refuse(cur_fm, p)  # tombstone-guard (a separate concern)
    if tr:
        return tr
    new_body, where = _insert_into_section(cur_body, heading, text)
    bir = _body_integrity_reject(cur_body, new_body)
    if bir:
        return f"rejected: {bir}"
    new_fm = dict(cur_fm)
    blr = _briefing_link_reject(p, new_body)  # append is the hot path for growing a briefing's
    if blr:  # `## Recent activity` — apply the same dead-link guard
        return f"rejected: {blr}"
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"
    _apply_extension_provenance(
        new_fm,
        creating=False,
        existing_ext_id=cur_fm.get("extension_id"),
        existing_producer_lane=cur_fm.get("producer_lane"),
    )
    _stamp(new_fm, cur_fm)
    fd = _future_date_reject(new_fm)  # the boundary every writer crosses (invariant-audit)
    if fd:
        return f"rejected: {fd}"  # file left untouched
    # append is the documented hot path for growing a briefing's `## Recent activity`, so the
    # SAME degenerate-content + dead-link review gate create/update apply must fire here on the
    # newly-appended text — else a degenerate run lands unflagged (invariant-audit).
    flags = (
        _review_flags(p, new_fm, prev=cur_fm)
        + _unresolvable_link_flags(p, text)
        + _degeneration_flags(text)
    )
    contract_reject = _contract_reject(p, "append", new_fm, new_body, [])
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, new_body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"
    _corpus_touch(p)
    _atomic_write_text(p, content)
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write append {_rel(p)} v{ver} ({heading})")
    note = _queue_review(p, flags)
    return f"appended to '{heading}' in {_rel(p)} v{ver} ({where}){note}"
