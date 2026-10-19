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


def _review_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review_store() -> Path:
    return _wiki() / "operational" / "reviews"


def _review_record_path(review_id: str) -> Path:
    return _review_store() / f"{_review_digest(review_id)}.yaml"


def _review_page_state(p: Path) -> tuple[dict, str, str, int, str]:
    fm, body = _read_page(p)
    content = p.read_text(encoding="utf-8")
    try:
        version = int(fm.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    subject = _rel(p).removesuffix(".md")
    return fm, body, subject, version, _review_digest(content)


def _structured_review_reasons(fm: dict, body: str, flags=None) -> list[dict]:
    """Convert legacy booleans/write-path strings into durable, explainable reason records."""
    reasons: list[dict] = []
    for flag in flags or []:
        code = (
            "categorical-confidence"
            if "categorical" in flag
            else "changed-after-approval"
            if "changed after" in flag
            else "agent-draft"
            if "degenerate" in flag
            else "manual"
        )
        reasons.append({"code": code, "detail": str(flag)})
    raw_conflicts = fm.get("conflicts")
    conflicts = ([raw_conflicts] if isinstance(raw_conflicts, dict)
                 else raw_conflicts if isinstance(raw_conflicts, list) else [])
    for conflict in conflicts:
        if isinstance(conflict, dict):
            reasons.append(
                {
                    "code": "conflict",
                    "field": str(conflict.get("field") or ""),
                    "detail": "sources disagree on this field",
                }
            )
    if re.search(
        r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)",
        body or "",
        re.S | re.I,
    ):
        reasons.append(
            {"code": "grounding", "detail": "grounding check flagged an unsupported claim"}
        )
    if not reasons:
        reasons.append({"code": "legacy-unspecified", "detail": "legacy needs_review flag"})
    # Stable de-duplication: the same write-path flag may be surfaced by more than one guard.
    out, seen = [], set()
    for reason in reasons:
        key = (reason.get("code"), reason.get("field"), reason.get("detail"))
        if key not in seen:
            seen.add(key)
            out.append(reason)
    return out


def _subject_refusal(subject: str) -> str | None:
    """Refuse a record key that is not vault-relative (okengine#832).

    The ledger's `subject` IS the join key the Cockpit worklist uses to attach a decision to its
    page. An absolute path silently produces a record nothing can ever find, while the page's own
    projection still reads as reviewed -- so the operator sees a decided page over a ledger that
    cannot corroborate it. `_rel` no longer produces one, and this makes any future regression fail
    loudly at the write instead of quietly at the join.
    """
    if not subject or subject.startswith("/") or subject.startswith("../"):
        return (f"refusing to record a review under a non-vault-relative subject {subject!r}; "
                "the decision would not be joinable to its page (okengine#832)")
    return None


def _ensure_review_request(p: Path, flags=None) -> dict:
    fm, body, subject, version, digest = _review_page_state(p)
    reasons = _structured_review_reasons(fm, body, flags)
    reason_key = json.dumps(reasons, sort_keys=True, ensure_ascii=False)
    review_id = f"review:{subject}:{version}:{digest[:16]}:{_review_digest(reason_key)[:12]}"
    rp = _review_record_path(review_id)
    if rp.is_file():
        rec = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
        return rec if isinstance(rec, dict) else {}
    caller = _caller()
    requested_by = caller.get("ext_id") or caller.get("kind") or "unknown"
    evidence = fm.get("sources") or fm.get("source") or []
    if not isinstance(evidence, list):
        evidence = [evidence]
    rec = {
        "version": 1,
        "review_id": review_id,
        "subject": subject,
        "subject_version": version,
        "subject_hash": digest,
        "state": "open",
        "reasons": reasons,
        "evidence": [str(v) for v in evidence if str(v).strip()],
        "requested_by": str(requested_by),
        "requested_at": _now(),
        "assigned_to": None,
        "history": [],
        "machine_checks": [],
    }
    rp.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(rp, yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
    return rec


def _load_review_record(review_id: str) -> tuple[Path, dict] | tuple[None, None]:
    rp = _review_record_path(review_id)
    if not rp.is_file():
        return None, None
    try:
        rec = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
    except Exception:
        return None, None
    return (rp, rec) if isinstance(rec, dict) else (None, None)


def _assign_review(
    path: str,
    reviewer: str,
    expected_version: int,
    expected_hash: str,
    review_id: str | None = None,
    service: str = "cli",
) -> dict:
    """Claim the current review request without changing the subject page."""
    candidate = _safe(path)
    if candidate is not None:
        scope_refusal = _wauth_refusal(candidate)
        if scope_refusal:
            return {"ok": False, "status": 403, "error": scope_refusal}
        cap = _capability_reject(candidate, "review", page_type=_existing_page_type(candidate))
        if cap:
            return {"ok": False, "status": 403, "error": cap}
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        return {"ok": False, "status": 400, "error": "reviewer identity is required"}
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError):
        return {"ok": False, "status": 400, "error": "expected page version is required"}
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash or "")):
        return {"ok": False, "status": 400, "error": "expected page hash is required"}
    p = _safe(path)
    if p is None or not p.is_file():
        return {"ok": False, "status": 404, "error": "subject page not found"}
    utf8 = _utf8_refusal(p)
    if utf8:
        return {"ok": False, "status": 422, "error": utf8}
    lock = _wiki().parent / ".okengine" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        _, _, subject, version, digest = _review_page_state(p)
        if version != expected_version or not hmac.compare_digest(digest, str(expected_hash)):
            return {
                "ok": False,
                "status": 409,
                "error": "subject changed; refresh before assigning",
            }
        rec = _ensure_review_request(p)
        if review_id and review_id != rec.get("review_id"):
            return {"ok": False, "status": 409, "error": "review request is stale"}
        if rec.get("state") in {"approved", "rejected", "dismissed"}:
            return {"ok": False, "status": 409, "error": "closed review cannot be assigned"}
        stamp = _now()
        rec["state"] = "in-review"
        rec["assigned_to"] = reviewer
        rec.setdefault("history", []).append(
            {
                "action": "assign",
                "state": "in-review",
                "assigned_to": reviewer,
                "at": stamp,
                "service": service,
            }
        )
        rec["version"] = int(rec.get("version") or 1) + 1
        rp = _review_record_path(rec["review_id"])
        rp.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=rp.parent, prefix=".review-record-", delete=False
        ) as f:
            yaml.safe_dump(rec, f, sort_keys=False, allow_unicode=True)
            tmp = Path(f.name)
        try:
            # NamedTemporaryFile is 0600. Review records are shared vault data consumed by the
            # unprivileged cockpit, so publish the replacement with the normal vault read mode.
            os.chmod(tmp, 0o644)
            os.replace(tmp, rp)
        finally:
            tmp.unlink(missing_ok=True)
        _append_log(f"- {_today()} review assign {subject} v{version} to {reviewer} via {service}")
        return {
            "ok": True,
            "status": 200,
            "review_id": rec["review_id"],
            "state": "in-review",
            "assigned_to": reviewer,
        }


def _resolve_review(
    path: str,
    decision: str,
    reviewer: str,
    note: str,
    expected_version: int,
    expected_hash: str,
    review_id: str | None = None,
    service: str = "cli",
) -> dict:
    """Apply one version-locked human decision and its audit record as a single governed action."""
    candidate = _safe(path)
    if candidate is not None:
        scope_refusal = _wauth_refusal(candidate)
        if scope_refusal:
            return {"ok": False, "status": 403, "error": scope_refusal}
        cap = _capability_reject(candidate, "review", page_type=_existing_page_type(candidate))
        if cap:
            return {"ok": False, "status": 403, "error": cap}
    decision = str(decision or "").strip().lower()
    reviewer = str(reviewer or "").strip()
    note = str(note or "").strip()
    if decision not in _REVIEW_DECISIONS:
        return {"ok": False, "status": 400, "error": "invalid review decision"}
    if not reviewer:
        return {"ok": False, "status": 400, "error": "reviewer identity is required"}
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError):
        return {"ok": False, "status": 400, "error": "expected page version is required"}
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash or "")):
        return {"ok": False, "status": 400, "error": "expected page hash is required"}
    if decision != "approve" and not note:
        return {"ok": False, "status": 400, "error": f"{decision} requires a decision note"}
    p = _safe(path)
    if p is None or not p.is_file():
        return {"ok": False, "status": 404, "error": "subject page not found"}
    utf8 = _utf8_refusal(p)
    if utf8:
        return {"ok": False, "status": 422, "error": utf8}
    lock = _wiki().parent / ".okengine" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        fm, body, subject, version, digest = _review_page_state(p)
        if refusal := _subject_refusal(subject):
            return {"ok": False, "status": 500, "error": refusal}
        if version != expected_version or not hmac.compare_digest(digest, str(expected_hash)):
            return {
                "ok": False,
                "status": 409,
                "error": "subject changed; refresh before deciding",
                "current_version": version,
                "current_hash": digest,
            }
        rec = _ensure_review_request(p)
        if review_id and review_id != rec.get("review_id"):
            return {"ok": False, "status": 409, "error": "review request is stale"}
        if rec.get("state") in {"approved", "dismissed"}:
            return {"ok": False, "status": 409, "error": "review request is already closed"}
        state, remains_flagged = _REVIEW_DECISIONS[decision]
        stamp = _now()
        event = {
            "decision": decision,
            "state": state,
            "decision_by": reviewer,
            "decision_at": stamp,
            "decision_note": note or None,
            "service": service,
            "subject_version": version,
            "subject_hash": digest,
        }
        rec["state"] = state
        rec["decision_by"] = reviewer
        rec["decision_at"] = stamp
        rec["decision_note"] = note or None
        rec["decision_service"] = service
        rec.setdefault("history", []).append(event)
        rec["version"] = int(rec.get("version") or 1) + 1
        new_fm = dict(fm)
        new_fm["needs_review"] = remains_flagged
        new_fm["review_state"] = state
        new_fm["review_id"] = rec["review_id"]
        new_fm["reviewed_version"] = version
        if state == "approved":
            new_fm.update({"reviewed_by": reviewer, "reviewed_on": _today(), "reviewed_at": stamp})
        else:
            # A prior approval must never remain current after a non-approval disposition.
            for key in ("reviewed_by", "reviewed_on", "reviewed_at"):
                new_fm.pop(key, None)
        new_fm["version"] = version + 1
        new_fm["last_updated"] = stamp
        new_content = _compose(new_fm, body)
        reject = schema_reject_reason(str(p), new_content)
        if reject:
            return {"ok": False, "status": 422, "error": f"review update violates schema: {reject}"}
        rp = _review_record_path(rec["review_id"])
        rp.parent.mkdir(parents=True, exist_ok=True)
        old_content = p.read_text(encoding="utf-8")
        old_record = rp.read_text(encoding="utf-8") if rp.exists() else None
        page_tmp = record_tmp = None
        page_published = record_published = False
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=p.parent, prefix=".review-page-", delete=False
            ) as f:
                f.write(new_content)
                page_tmp = Path(f.name)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=rp.parent, prefix=".review-record-", delete=False
            ) as f:
                yaml.safe_dump(rec, f, sort_keys=False, allow_unicode=True)
                record_tmp = Path(f.name)
            # Atomic tempfiles default to 0600. The review writer and read-plane containers run as
            # distinct UIDs, so replacing a page without normalizing its mode makes a successful
            # decision unreadable and turns the reader into HTTP 500. Vault publications are 0644.
            os.chmod(page_tmp, 0o644)
            os.chmod(record_tmp, 0o644)
            _corpus_touch(p)
            os.replace(page_tmp, p)
            page_tmp = None
            page_published = True
            os.replace(record_tmp, rp)
            record_tmp = None
            record_published = True
        except Exception as exc:
            _restore_review_outputs(
                p, old_content, rp, old_record, page_published, record_published
            )
            return {"ok": False, "status": 500, "error": f"atomic review write failed: {exc}"}
        finally:
            if page_tmp:
                page_tmp.unlink(missing_ok=True)
            if record_tmp:
                record_tmp.unlink(missing_ok=True)
        _append_log(
            f"- {_today()} review {decision} {subject} v{version} by {reviewer} via {service}"
        )
        return {
            "ok": True,
            "status": 200,
            "review_id": rec["review_id"],
            "state": state,
            "subject": subject,
            "reviewed_version": version,
            "page_version": version + 1,
        }


def _restore_review_outputs(
    page: Path,
    old_content: str,
    record: Path,
    old_record: str | None,
    page_published: bool,
    record_published: bool,
) -> None:
    """Restore either output that crossed its atomic publish boundary before a failure."""
    if page_published:
        _corpus_touch(page)
        _atomic_write_text(page, old_content)
    if record_published:
        if old_record is None:
            record.unlink(missing_ok=True)
        else:
            _atomic_write_text(record, old_record)


def _record_machine_review(path: str, evaluator: str, outcome: str, note: str = "") -> dict:
    """Attach a machine check without clearing or impersonating human approval."""
    if outcome not in {"supported", "unsupported", "unresolved"}:
        return {"ok": False, "status": 400, "error": "invalid machine review outcome"}
    p = _safe(path)
    if p is not None:
        scope_refusal = _wauth_refusal(p)
        if scope_refusal:
            return {"ok": False, "status": 403, "error": scope_refusal}
        cap = _capability_reject(p, "review", page_type=_existing_page_type(p))
        if cap:
            return {"ok": False, "status": 403, "error": cap}
    if p is None or not p.is_file():
        return {"ok": False, "status": 404, "error": "subject page not found"}
    utf8 = _utf8_refusal(p)
    if utf8:
        return {"ok": False, "status": 422, "error": utf8}
    lock = _wiki().parent / ".okengine" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        rec = _ensure_review_request(p)
        check = {
            "evaluator": str(evaluator or "machine"),
            "outcome": outcome,
            "note": str(note or ""),
            "checked_at": _now(),
        }
        rec.setdefault("machine_checks", []).append(check)
        rec["version"] = int(rec.get("version") or 1) + 1
        rp = _review_record_path(rec["review_id"])
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=rp.parent, prefix=".review-record-", delete=False
        ) as f:
            yaml.safe_dump(rec, f, sort_keys=False, allow_unicode=True)
            tmp = Path(f.name)
        try:
            os.chmod(tmp, 0o644)
            os.replace(tmp, rp)
        finally:
            tmp.unlink(missing_ok=True)
        _append_log(
            f"- {_today()} review-machine {outcome} {rec['subject']} by {check['evaluator']}"
        )
        return {
            "ok": True,
            "status": 200,
            "review_id": rec["review_id"],
            "state": rec["state"],
            "machine_check": check,
        }


def _identity_edit_grant() -> "Any":
    """The caller's identity-edit permission (okengine#1028). The operator principal (the
    actor-less admin caller, which no model-facing surface binds) holds every grant. A cron lane's
    grant is `identity_edit` in its server-bound output contract; a networked extension's is the
    same key in its scoped write capability. Anything else, chat included, holds none."""
    from okengine import identity_guard

    caller = _caller()
    kind = caller.get("kind")
    if kind in ("admin",):
        return identity_guard.ALL
    if kind in ("extension",):
        capability = caller.get("write_capability")
        if not isinstance(capability, dict) or "create" in (capability.get("operations") or []):
            # A page-creating extension never holds a grant, as for a cron lane (enforced here
            # whatever the manifest says; extension_manifest refuses the declaration too).
            return identity_guard.NONE
        return identity_guard.permission(capability.get("identity_edit"))
    contract, _lane = _output_contract.resolve(caller)
    return identity_guard.permission(
        contract.get("identity_edit") if isinstance(contract, dict) else None)


def _identity_view(p: Path, fm: dict) -> dict:
    """`fm` in the canonical shape (`aliases: A, B` is two aliases), with the pack's key renames
    and the canonical type, as the write will store them. Not a full drift normalization, which
    trips over legacy unhashable values on the stored page."""
    view = _alias_renamed(_canonical_view(p, dict(fm)), _field_aliases(p))
    if "type" in view:
        try:
            view["type"] = schema_lib.canonical_type(_governing(p), view.get("type"))
        except Exception:  # an unreadable schema leaves the stored spelling, judged as is
            pass
    return view


def _dangling_reference_removal(change: "Any", p: Path) -> bool:
    """Removing a reference that names no page at all is repair, not an identity change
    (okengine#1028): a lane clearing the dangling `prediction_candidate` its own contract refuses
    must not be held. It fails closed, so a link to a page that EXISTS is never removed this way:
    - only a plain field removal (never `type`, `title`, `name`, `aliases` or a citation list,
      which are judged apart), of a field the caller's server-bound contract declares as a
      relationship;
    - every stored value must be a wiki PATH (`namespace/.../stem`, wikilink brackets allowed). A
      bare stem (`[[p1]]`), an id (`predictions:p1`) or free text is never judged dangling;
    - no other page anywhere in the vault may carry that stem, compared case-insensitively. That
      covers an exact, sharded, moved, case-variant, duplicated or tombstoned target."""
    if change.kind not in ("removal",):
        return False
    contract, _lane = _output_contract.resolve(_caller())
    if not isinstance(contract, dict):
        return False
    declared = {*contract.get("required_relationships", []),
                *contract.get("optional_relationships", [])}
    if change.field not in declared:
        return False
    values = change.before if isinstance(change.before, list) else [change.before]
    return all(_names_no_page(value, p) for value in values)


def _names_no_page(value: "Any", p: Path) -> bool:
    """True only for a well-formed wiki path whose stem no OTHER page in the vault carries (the
    page under write may share the stem: a lacuna names the prediction it would become)."""
    from okengine import identity_guard

    target = identity_guard._citation_key(value)
    parts = target.split("/")
    if ":" in target or len(parts) < 2 or not all(parts):
        return False
    stem = parts[-1].casefold()
    own = p.resolve()
    return not any(page.stem.casefold() == stem and page.resolve() != own
                   for page in _wiki().rglob("*.md"))  # glob-ok: an existence probe, every shard


def _identity_hold(p: Path, before: dict, after: dict) -> tuple[list[str], bool, set]:
    """Hold every identity change `after` makes to the stored page `before` that the caller may
    not apply directly (okengine#1028): restore the stored value in `after` (in place) and return
    the review reasons, whether a body replacement in the same write must be held too, and the
    held fields. Both
    sides are judged on the pack's canonical vocabulary, so a key or value the write path itself
    renames is never read as the caller's change."""
    from okengine import identity_guard

    before_view = _identity_view(p, before)
    after_view = _identity_view(p, after)
    # A key alias needs no exemption: the views above have already renamed it away.
    exempt = set().union(converge._SERVER_KEYS, _STAMP_KEYS, _REVIEW_MANAGED_FIELDS)
    changes = identity_guard.identity_changes(before_view, after_view, exempt, stem=p.stem,
                                              name_key=id_lib.normalize_key)
    if not changes:
        return [], False, set()
    grant = _identity_edit_grant()
    catchall = _governing(p).get("classify_catchall") or []
    held = [change for change in changes
            if not identity_guard.permits(grant, change, before_type=before_view.get("type"),
                                          catchall=catchall if isinstance(catchall, list) else [])
            and not _dangling_reference_removal(change, p)]
    identity_guard.hold(after, before_view, held)
    # A held field must not come back under a key alias the write path renames onto it later
    # (`display_name: name`): the alias spelling of a held change is held with it (!1305 QA N1).
    held_fields = {change.field for change in held}
    for alias, canonical in _field_aliases(p).items():
        if canonical in held_fields:
            after.pop(alias, None)
    return ([identity_guard.flag(change) for change in held], identity_guard.holds_body(held),
            {change.field for change in held})
