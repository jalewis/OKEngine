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
from okengine.write_services.state import lane_actor
import output_contract_enforce as _output_contract
from okengine import attestation as _attestation
from okengine.field_values import same_value as _same_value
from okengine.file_fingerprint import fingerprint

import id_lib, schema_lib, id_index, converge, okf_migrate
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _today() -> str:
    """ISO date (YYYY-MM-DD) for the wiki/log.md ledger lines: the deployment's LOCAL day, from the
    process TZ. A stdio writer gets TZ only from its own `env:` map (Hermes filters the gateway's
    environment), which ensure-runtime binds and the `timezone` deployment check verifies
    (okengine#1042 [36]). Injectable for tests."""
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
    # okengine#983: the Chat agent's writer. ensure-runtime also pins OKENGINE_WRITE_TOOLS to this
    # set; the table is the fallback when that env is absent.
    "chat:api_server": {"create_entity", "update_entity", "append_to_section"},
}


def _chat_review_flags() -> list[str]:
    """okengine#983: every write by a model-facing chat writer needs a human disposition.

    Chat answers a person in real time and writes back what it found on the web, with no lane
    contract and no selector behind it; on one deployment it rewrote an actor's related-actor
    attribution unreviewed. So the write lands, and is always queued for review."""
    actor = str(_caller().get("actor") or "")
    # A literal, not a module constant: these helpers are rebound into the write_server facade,
    # which carries functions, not this module's constants.
    if not actor.startswith("chat:"):
        return []
    return [f"chat write by {actor} requires human disposition (okengine#983)"]


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
    """Load composed policy with cheap file-fingerprint invalidation for long-lived servers (an
    mtime-only key kept enforcing a policy edited within the same timestamp tick, okengine#1017)."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    paths = policy_plane.discover_documents(vault)
    key = tuple((str(path), fingerprint(path)) for path in paths)
    if _policy_cache["key"] != key:
        _policy_cache["value"] = policy_plane.compose_documents(paths)
        _policy_cache["key"] = key
    return _policy_cache["value"]


def _capability_reject(
    p: Path, operation: str, *, page_type: str = "", changed_fields=(), body_change: str = "none",
    values: Optional[dict] = None, stored: Optional[dict] = None,
) -> Optional[str]:
    """Enforce the authenticated caller's operation/type/field/body/value authority.

    This runs before every filesystem mutation.  A rejected attempt is emitted as
    the common structured finding and the human MCP response retains a concise
    policy rule ID and remediation. `values` is the frontmatter the write would store; it is
    judged after the write's own key and value aliasing (`_normalize_drift`), so a pack alias
    cannot carry a forbidden value past `forbidden_values`; `stored` is the page as stored, raw
    (okengine#993).
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
            # A composed instance (`cron:<job>@<pack>`) runs the same engine lane, so
            # without an entry of its own it inherits the lane's (okengine#961).
            capabilities = policy.get("capabilities", {})
            if actor not in capabilities:
                lane = lane_actor(actor)
                declared = (
                    capabilities[lane] if lane in capabilities
                    else _output_contract.capability(caller)
                )
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
    resulting = None if values is None else _normalize_drift(dict(values), p)[0]
    result = policy_plane.evaluate_capability(
        policy, actor, operation, _rel(p), page_type, changed_fields, body_change,
        _qualified_namespace(p),  # the namespace: sub-domain aware; shard dirs below never count
        values=resulting, stored=stored,
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


def _field_aliases(p: Path) -> dict:
    """The governing pack's key renames ({alias: canonical}), as `_normalize_drift` applies them."""
    aliases = drift_policy(str(p)).get("field_aliases") or {}
    return aliases if isinstance(aliases, dict) else {}


def _alias_renamed(fm: dict, aliases: dict) -> dict:
    """`fm` with the pack's KEY renames applied exactly as `_normalize_drift` will before the
    write commits: one pass in declaration order, so a chain (`{hx: hy, hy: tlp}`) lands where the
    write lands it. Only keys: value aliases cannot introduce a protected key, and applying them
    to a stored page would trip over legacy unhashable values. A rename to an empty or non-string
    name is not applied (the field is judged under its own name, which fails closed)."""
    out = dict(fm)
    for alias, canonical in aliases.items():
        if alias in out and isinstance(canonical, str) and canonical:
            value = out.pop(alias)
            if out.get(canonical) in (None, "", [], {}):
                out[canonical] = value
    return out


def _frontmatter_changes(p: Path, before: dict, after: dict) -> list[str]:
    """The fields a write changes: the page exactly as stored versus the page the write stores.

    Every write path's capability check reads this, so the check and the write cannot disagree.
    `before` is compared RAW, as it is on disk; `after` is renamed by the same key pass the write
    applies to the whole committed frontmatter (`_alias_renamed`, `_normalize_drift`'s key pass),
    stored keys included. So a key the write materializes counts as a change whoever wrote the
    alias: a stored `hy` that the write renames onto `tlp` is a change to `tlp` (okengine#983).
    Compared type-strictly (`field_values.same_value`): `0` over `false` is a change although
    `0 == False`, and a verbatim echo of canonically-keyed frontmatter is not. A field present on
    only one side has changed."""
    old = dict(before)
    new = _alias_renamed(after, _field_aliases(p))
    return sorted((key for key in set(old) | set(new)
                   if key not in old or key not in new or not _same_value(new[key], old[key])),
                  key=str)


def _stored_echoes(p: Path, stored: dict, patch: dict) -> dict:
    """`patch` as the write is JUDGED: every value that only re-sends the stored value is replaced
    by the stored value.

    A read-modify-write cannot always send a stored value back verbatim. JSON has no date type, so
    a tool echoes `reviewed_on: 2026-09-01` as the string "2026-09-01"; and the write path itself
    coerces what it receives (`_coerce_fm`: a scalar list field becomes a list, a `[[link]]` a plain
    path), so an echo of a legacy stored shape arrives reshaped. Neither is a mutation, and judged
    raw each read as a protected-field change. Judged against this mapping, an echo changes nothing
    and the type-strict comparison still decides every real change: `0` over `false` is not an
    echo (okengine#991). What the write STORES is `_stored_repairs`."""
    return {key: stored[key] if key in stored and _echo_kind(p, key, stored[key], sent) else sent
            for key, sent in patch.items()}


def _stored_repairs(p: Path, stored: dict, patch: dict) -> dict:
    """`patch` as the write STORES it: a JSON date echo keeps the stored date, and a reshaped echo
    stores the canonical shape (okengine#994 [19]).

    A sent value that is the write path's canonical shape of the stored value (`_coerce_fm` of the
    stored value: a legacy scalar list field as a list, a `[[link]]` as the plain path) is the same
    data, so `_stored_echoes` judges it no change; it is also exactly what the write path makes of
    every value it is sent. Keeping the legacy shape instead silently discarded the repair, and
    left such a page unrepairable through the write path. Operator-owned attestation fields are
    the exception: an MCP write must leave them exactly as stored (okengine#980), so an echo of one
    keeps the stored value byte for byte and only their importer repairs them."""
    frozen = _attestation_frozen(p, stored)
    return {key: stored[key]
            if key in stored and _keeps_stored(_echo_kind(p, key, stored[key], sent), key, frozen)
            else sent
            for key, sent in patch.items()}


def _keeps_stored(kind: Optional[str], key: Any, frozen: frozenset) -> bool:
    return kind == "date" or (kind == "reshape" and key in frozen)


def _attestation_frozen(p: Path, stored: dict) -> frozenset:
    """The patch keys (alias spellings included) whose stored value an MCP write must keep exactly:
    the operator-owned attestation fields, plus the record identity of an attested page."""
    fields = set(_attestation.OPERATOR_FIELDS)
    if _attestation.carries_attestation(stored):
        fields.update(_attestation.RECORD_IDENTITY_FIELDS)
    aliases = _field_aliases(p)
    return frozenset(fields | {alias for alias, canonical in aliases.items() if canonical in fields})


def _echo_kind(p: Path, key: Any, stored_value: Any, sent: Any) -> Optional[str]:
    """How `sent` only re-sends `stored_value`: "date" (the JSON spelling of a stored date),
    "reshape" (the write path's canonical shape of it), or None (a real change)."""
    if isinstance(stored_value, datetime.date) and isinstance(sent, str):
        # The JSON spelling of a YAML date or timestamp: the string the stored value loads from.
        try:
            return "date" if _same_value(yaml.safe_load(sent), stored_value) else None
        except Exception:  # YAMLError, or RecursionError on pathological nesting: not an echo
            return None
    reshaped = _coerce_fm({key: stored_value}, p)
    return "reshape" if reshaped is not None and _same_value(sent, reshaped.get(key)) else None


def _attestation_reject(p: Path, prev: dict, new: dict, body_changed: bool) -> Optional[str]:
    """okengine#980: refuse an MCP write that alters operator-owned attestation.

    The stored page is compared RAW against what the write will store: the new frontmatter
    renamed exactly as `_normalize_drift` will before the write commits. An alias whose
    canonical name is a protected key would otherwise carry the write past this check under its
    alias spelling, and a stored alias key the write renames onto one is a change too (#983)."""
    return _attestation.mcp_write_refusal(
        dict(prev), _alias_renamed(new, _field_aliases(p)), body_changed)


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
    # A control character is never part of a page path in any namespace (okengine#884). A model
    # whose tool-call XML leaked put the frontmatter INTO the path argument, and the write landed
    # as a file named `llm-assisted-cybercrime\nslug: ...\ntags: ...md` -- 15 such pages across
    # four live vaults, invisible to readers and breaking every line-based lane that lists paths.
    if any(ord(ch) < 32 or ch == "\x7f" for ch in rel):
        return None
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
