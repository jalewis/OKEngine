from __future__ import annotations
# ruff: noqa: F821

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


def _today() -> str:
    """ISO date (YYYY-MM-DD) for the wiki/log.md ledger lines. Injectable for tests."""
    override = os.environ.get("OKENGINE_MCP_WRITE_DATE")
    if override:
        return override
    return datetime.date.today().isoformat()


def _now() -> str:
    """ISO-8601 UTC TIMESTAMP (YYYY-MM-DDTHH:MM:SSZ) for `last_updated`/`created`/`updated` — the
    OKF envelope fields the spec defines as timestamps (guide-2), so the UI can track *when*, not
    just *which day*. Injectable via OKENGINE_MCP_WRITE_NOW; falls back to the date override (so a
    date-only test override still works) then real UTC now."""
    override = os.environ.get("OKENGINE_MCP_WRITE_NOW")
    if override:
        return override
    date_override = os.environ.get("OKENGINE_MCP_WRITE_DATE")
    if date_override:
        return date_override
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wiki() -> Path:
    """Re-read WIKI from env each call so tests can repoint WIKI_PATH at runtime."""
    return Path(os.environ.get("WIKI_PATH") or str(VAULT)) / "wiki"


# Per-ENGINE-lane MCP tool allowlists (okengine#664): engine lanes only — a pack lane's surface is
# pack config (`write_tools:` on its cron def -> OKENGINE_WRITE_TOOLS), never an engine literal.
ACTOR_TOOLS: dict[str, set[str]] = {
    "cron:source-quality-backfill": {"score_source"},
    "cron:raw-backfill": {"converge_source"},
    "cron:entity-backfill": {"converge_entity"},
    "cron:concept-backfill": {"converge_concept"},
    "cron:page-quality-enrich": {
        "update_entity",
        "patch_entity",
        "append_to_section",
        "converge_entity",
    },
    "cron:okengine.predictions:prediction-structural-backfill": {
        "update_entity",
        "patch_entity",
        "append_to_section",
        "converge_entity",
    },
}


def _caller() -> dict:
    c = _caller_var.get()
    if c is not None:
        return c
    # A dedicated stdio MCP process may be bound to one scheduler identity in
    # config.yaml.  The agent cannot supply or alter this value as a tool argument.
    actor = os.environ.get("OKENGINE_WRITE_ACTOR", "").strip()
    if actor:
        return {"kind": "job", "actor": actor, "write_scopes": None, "ext_id": None}
    return {"kind": "admin", "actor": "admin", "write_scopes": None, "ext_id": None}


def _effective_policy() -> dict:
    """Load composed policy with cheap mtime invalidation for long-lived servers."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    paths = policy_plane.discover_documents(vault)
    key = tuple((str(path), path.stat().st_mtime_ns if path.exists() else None) for path in paths)
    if _policy_cache["key"] != key:
        _policy_cache["value"] = policy_plane.compose_documents(paths)
        _policy_cache["key"] = key
    return _policy_cache["value"]


def _capability_reject(
    p: Path, operation: str, *, page_type: str = "", changed_fields=(), body_change: str = "none"
) -> Optional[str]:
    """Enforce the authenticated caller's operation/type/field/body authority.

    This runs before every filesystem mutation.  A rejected attempt is emitted as
    the common structured finding and the human MCP response retains a concise
    policy rule ID and remediation.
    """
    caller = _caller()
    if caller.get("kind") == "admin":
        return None
    actor = str(
        caller.get("actor")
        or (f"extension:{caller.get('ext_id')}" if caller.get("ext_id") else "unknown")
    )
    policy = _effective_policy()
    declared = None
    match caller.get("kind"):
        case "job":
            # A catalog capability is an intentional, potentially narrower engine
            # policy (for example source-quality's two-field update). Only derive the
            # generic cron capability when the authenticated actor has no such entry.
            if actor not in policy.get("capabilities", {}):
                declared = _output_contract.capability(caller)
        case "extension":
            declared = caller.get("write_capability")
            # Existing extension manifests remain path-scoped until they opt into
            # the richer contract. Their path authority is still enforced by
            # _wauth_refusal.
            if not declared:
                return None
    if declared:
        policy = dict(policy)
        policy["capabilities"] = dict(policy.get("capabilities", {}))
        policy["capabilities"][actor] = declared
    result = policy_plane.evaluate_capability(
        policy, actor, operation, _rel(p), page_type, changed_fields, body_change
    )
    if result is None:
        return None
    try:
        policy_plane.append_event(Path(os.environ.get("WIKI_PATH") or str(VAULT)), result)
    except OSError:
        pass
    return policy_plane.finding_message(result)


def _contract_reject(
    p: Path,
    operation: str,
    fm: dict,
    body: str,
    drift: list[str],
    *,
    body_links_changed: bool = True,
) -> Optional[str]:
    """Evaluate the authenticated lane contract immediately before mutation."""
    unknown: list[str] = []
    for flag in drift:
        if flag.startswith("unknown field(s)") and ":" in flag:
            unknown.extend(x.strip() for x in flag.split(":", 1)[1].split(","))
    caller = dict(_caller())
    caller["body_links_changed"] = body_links_changed
    findings = _output_contract.evaluate(
        caller,
        operation=operation,
        namespace=_namespace(p),
        page_type=str(fm.get("type") or ""),
        frontmatter=fm,
        body=body or "",
        unknown_fields=unknown,
        wiki=_wiki(),
    )
    if not findings:
        return None
    detail = "; ".join(f"{f['code']}: {f['message']}" for f in findings)
    configured_mode = os.environ.get("OKENGINE_OUTPUT_CONTRACT_MODE")
    mode = configured_mode or ("enforce" if caller.get("kind") == "job" else "report")
    _append_log(
        f"- {_today()} output-contract {'report' if mode != 'enforce' else 'reject'} "
        f"{_rel(p)} — {detail}"
    )
    if mode != "enforce":
        return None
    return "output_contract." + detail


def _apply_extension_provenance(
    fm: dict,
    *,
    creating: bool,
    existing_ext_id: Optional[str] = None,
    existing_producer_lane: Optional[str] = None,
) -> None:
    """Stamp server-derived extension and cron-lane provenance.

    `extension_id` is SERVER-DERIVED provenance (okengine#132/#133): the key disable/orphan/purge
    read, and `extensions purge --yes` HARD-deletes by it — the ONE non-tombstone delete in a
    tombstone-only contract. A client must never set or change it, or (invariant-audit) a scoped
    token forges another extension's id onto a curated page and a later `purge` unlink()s a page
    that extension never wrote, or an extension orphan-proofs its own pages so its purge misses them.
    The create-path stamp alone (only in _create, only for extension callers) left update/patch/
    converge/admin-create wide open. So: strip the incoming value unconditionally at EVERY write, then
    derive it from the scoped token on CREATE, or preserve the immutable create-time stamp on a
    mutation. Stdio/admin writes get no stamp."""
    fm.pop("extension_id", None)  # never client-settable — kill any forge
    fm.pop("producer_lane", None)
    caller = _caller()
    if creating and caller.get("kind") == "job":
        actor = str(caller.get("actor") or "")
        if actor.startswith("cron:") and actor.removeprefix("cron:"):
            fm["producer_lane"] = actor.removeprefix("cron:")
    elif not creating and existing_producer_lane:
        # The producer is the lane that created the artifact, not whichever authenticated job
        # most recently changed a field. Re-attributing a raw page after a narrow quality update
        # makes the offline audit select the wrong (weaker) output contract.
        fm["producer_lane"] = existing_producer_lane
    if creating:
        if caller.get("kind") == "extension" and caller.get("ext_id"):
            fm["extension_id"] = caller["ext_id"]  # server-derived from the scoped token
    elif existing_ext_id:
        fm["extension_id"] = existing_ext_id


def _apply_review_governance(fm: dict, prev: dict | None = None) -> list[str]:
    """Keep human-decision projections server-owned and invalidate them on content writes.

    Ordinary entity mutation may raise ``needs_review`` but cannot clear an existing flag or forge
    reviewer identity. Any edit after a review request/decision opens a new version-scoped request;
    only ``_resolve_review`` may write the managed projection fields.
    """
    for key in _REVIEW_MANAGED_FIELDS:
        fm.pop(key, None)
    if prev is None:
        return []
    if prev.get("needs_review") is True:
        fm["needs_review"] = True
    if any(prev.get(key) not in (None, "") for key in _REVIEW_MANAGED_FIELDS):
        fm["needs_review"] = True
        return ["content changed after a prior review request or decision"]
    return []


def _authorize_write(path: str) -> bool:
    """May the current caller write this wiki-relative path? Admin (stdio gateway) =
    always; an extension = only within its declared write scopes."""
    c = _caller()
    if c.get("kind") in {"admin", "job"}:
        # Jobs are path/type/operation/field/body-gated by _capability_reject.
        # They do not use extension path scopes, which are a separate token model.
        return True
    return _scope.path_in_scopes(str(path), c.get("write_scopes") or [])


def _wauth_refusal(path) -> Optional[str]:
    """Refuse if the caller can't write this path. Authorize on the NORMALIZED target, not the
    raw agent string: _safe() collapses '..' and strips redundant prefixes, so a raw
    'entities/../predictions/x' textually matches an 'entities/**' scope yet WRITES to
    predictions/ — scope must gate the real destination, not the spelling (okengine#178). Accepts
    a str or a resolved Path (converge re-auth passes the redirected canonical)."""
    sp = _safe(str(path))
    check = str(sp.relative_to(_wiki().resolve())) if sp is not None else str(path)
    if not _authorize_write(check):
        c = _caller()
        return (
            f"refused: '{path}' is outside extension '{c.get('ext_id')}'"
            f"'s write scope (declared: {c.get('write_scopes')})"
        )
    return None


def _normalize_entity_shard(rel: str) -> str:
    """Canonicalize an entity path to the shard layout the reshard drain + assembler use, so an
    agent that picks the wrong shard doesn't create a stale DUPLICATE (okengine#48). The shard
    letters are always recomputed from the SLUG (not trusted from the path). The vault may RESHARD
    a hot first-letter leaf to two levels (`entities/<l>/<2nd>/<slug>.md`, 2nd = slug[1]) once it
    exceeds the threshold (reshard_oversized.py); this must NOT collapse a valid resharded canonical
    back to one level — that refuses/duplicates writes on a mature vault (okengine invariant-audit).
    Choose one- vs two-level by what's actually on disk. Other namespaces are left untouched."""
    parts = rel.split("/")
    # entities/ shard scheme: the FLAT form `entities/<slug>` (2 parts, the most common wrong shape —
    # okengine invariant-audit) OR an already-sharded form with single-char intermediate segments.
    # A multi-char intermediate segment is some other layout and is left alone.
    if not (
        parts[0] == "entities"
        and (len(parts) == 2 or (len(parts) >= 3 and all(len(seg) == 1 for seg in parts[1:-1])))
    ):
        return rel
    stem = parts[-1][:-3] if parts[-1].endswith(".md") else parts[-1]
    if not stem:
        return rel
    l1 = stem[0].lower()
    one = f"entities/{l1}/{stem}.md"
    second = stem[1].lower() if len(stem) > 1 and stem[1].isalnum() else "_"
    two = f"entities/{l1}/{second}/{stem}.md"
    try:
        wiki = _wiki()
        if (wiki / two).exists():
            return two  # already at the resharded canonical
        leaf = wiki / "entities" / l1
        if (
            not (wiki / one).exists()
            and leaf.is_dir()
            and any(d.is_dir() and len(d.name) == 1 for d in leaf.iterdir())
        ):
            return two  # this first-letter leaf HAS been resharded
    except OSError:
        pass
    return one


def _safe(path: str) -> Optional[Path]:
    """Resolve a wiki-relative path, refusing escapes outside wiki/, forcing .md.

    Paths are relative to wiki/ (e.g. `sources/2026/06/x`). A caller (or pack
    ingest prompt) that prefixes a redundant leading `wiki/` must NOT stack into
    `wiki/wiki/...` — strip it. The escape guard can't catch that because the
    doubled path is still *inside* wiki/, so it would silently misfile every page
    and break raw-drain dedup (okengine#31). The same applies to an OVER-QUALIFIED
    path: an agent that follows the persona's "prefer the absolute form" guidance
    for file_read may pass the full `/opt/vault/wiki/sources/x` (or the vault-relative
    `opt/vault/wiki/...`) to a write tool — that would land in a shadow
    `wiki/opt/vault/wiki/...` tree (still inside wiki/, so the escape guard misses
    it), creating duplicate canonicals. Collapse any leading absolute/relative
    vault-or-wiki prefix to the wiki-relative tail first (the over-qualified-path
    variant of okengine#31/#34). Entity paths are also normalized to the one-level
    shard layout to prevent duplicate canonicals (okengine#48)."""
    wiki = _wiki()
    try:
        wiki_abs = wiki.resolve()
    except OSError:
        wiki_abs = wiki
    rel = path.strip()
    # Strip the longest matching over-qualified prefix: the absolute wiki path,
    # the absolute vault path, or either without the leading slash. Longest-first
    # so `/opt/vault/wiki` wins over `/opt/vault`; the redundant-`wiki/` loop below
    # then mops up any residual (e.g. a stripped vault prefix leaving `wiki/...`).
    _prefixes = []
    for _b in (wiki_abs, wiki_abs.parent):
        _s = str(_b)
        _prefixes += [_s, _s.lstrip("/")]
    for _cand in sorted({p for p in _prefixes if p}, key=len, reverse=True):
        if rel == _cand or rel.startswith(_cand + "/"):
            rel = rel[len(_cand) :]
            break
    rel = rel.lstrip("/")
    while rel == "wiki" or rel.startswith("wiki/"):
        rel = rel[len("wiki") :].lstrip("/")
    # Page basenames are identifiers, not prose. Whitespace and run-on names are
    # strong evidence that an extraction lane passed a sentence as the slug
    # (okengine#240). Reject at the boundary before such pages become links.
    raw_name = rel.rsplit("/", 1)[-1]
    raw_stem = raw_name[:-3] if raw_name.endswith(".md") else raw_name
    if rel.startswith("entities/") and (
        any(ch.isspace() for ch in raw_stem) or len(raw_stem) > _MAX_ENTITY_SLUG_LEN
    ):
        return None
    rel = _normalize_entity_shard(rel)
    p = wiki / rel
    if p.suffix != ".md":
        # APPEND '.md' — never with_suffix(), which strips everything after the LAST dot and so
        # truncates a dotted slug ('sources/2026/07/openssl-3.0.7-advisory' -> '...openssl-3.0.md',
        # colliding distinct prefixes onto one file and dead-linking every wikilink). invariant-audit.
        p = p.with_name(p.name + ".md")
    try:
        p = p.resolve()
        p.relative_to(wiki.resolve())
    except (OSError, ValueError):
        return None
    return p
