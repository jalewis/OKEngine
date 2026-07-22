#!/usr/bin/env python3
"""OKEngine MCP WRITE surface (ENGINE, conformance gap G1).

A SEPARATE, local stdio MCP server that exposes vault-WRITE tools. It is the
server-enforced conformance contract for agent writes: every write tool
validates the composed page against the governing `schema.yaml` (walk-up, via
`tools.schema_validator.schema_reject_reason`) BEFORE touching the filesystem,
and appends one line to `wiki/log.md` on success. This is the unbypassable
counterpart to the `file`-tool write-guard (which stays as the backstop).

Distinct from the read-only `server.py` (networked, vault mounted `:ro`). Default
transport is STDIO (the trusted local gateway caller — full write). Set
OKENGINE_WRITE_TRANSPORT=streamable-http to expose a NETWORKED, token-authenticated
write surface so an out-of-process sidecar extension can write (okengine#132): the
admin token keeps full write; an extension's minted token is limited to its declared
write scopes, and its pages are stamped with `extension_id` provenance.

Tools (each returns a short human-readable status string):
  create_entity(path, frontmatter_yaml, body)         — refuses if file exists
  update_entity(path, frontmatter_yaml=None, body=None) — bumps version
  converge_entity(path, frontmatter_yaml, body, pack) — upsert by id; merge under
                                                        page+field ownership (P2)
  tombstone_entity(path, reason, superseded_by=None)  — never deletes
  flag_for_review(path, note)                          — appends to review queue

The real logic lives in plain module-level helpers (`_create`/`_update`/
`_tombstone`/`_flag`); the `@mcp.tool()` wrappers merely delegate. This keeps
the helpers unit-testable without the `mcp` package installed.

Env: WIKI_PATH (/opt/vault), OKENGINE_MCP_WRITE_DATE (override today()).
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
from typing import Optional, Union

import yaml
from starlette.requests import Request as StarletteRequest

# Robust import of the engine validator regardless of CWD: repo root is the
# parent of this file's directory (okengine-mcp/'s parent).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.schema_validator import schema_reject_reason, governing_policy, drift_policy, \
    canonicalize_enum_case  # noqa: E402
from tools import policy_plane  # noqa: E402
import scope as _scope  # noqa: E402  per-extension token resolution (okengine#132)
import output_contract_enforce as _output_contract  # noqa: E402

# Converge-on-write (composable okpacks P2) needs the id + schema + merge libs.
# Optional: if any are absent, converge_entity is disabled but the rest works.
_CONVERGE_OK = True
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "cron"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import id_lib       # noqa: E402
    import schema_lib   # noqa: E402
    import id_index     # noqa: E402
    import converge     # noqa: E402
    import okf_migrate  # noqa: E402  — canonical partition contract for every writer
except Exception:       # pragma: no cover
    _CONVERGE_OK = False

# `or` (not a get() default): a set-but-blank WIKI_PATH must fall back too, else
# Path("")/"wiki" resolves to a *relative* wiki/ under CWD (okengine#34).
VAULT = Path(os.environ.get("WIKI_PATH") or "/opt/vault")
WIKI = VAULT / "wiki"
_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)
# A TRUE H1 (`# title`). `[ \t]+` after the single `#` means `## Summary` and deeper
# section headings never match — only a page-title H1 is captured (group 1).
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)
_MAX_ENTITY_SLUG_LEN = 80
_REVIEW_STATES = {"open", "in-review", "changes-requested", "approved", "rejected", "dismissed"}
_REVIEW_DECISIONS = {
    "approve": ("approved", False),
    "request-changes": ("changes-requested", True),
    "reject": ("rejected", True),
    "dismiss": ("dismissed", False),
    "defer": ("open", True),
}


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


# Per-request caller identity (okengine#132), set by the networked auth middleware.
# None (stdio — the trusted local gateway caller) = admin = FULL write, which is the
# pre-#132 behavior. A networked extension caller is limited to its write scopes.
_caller_var: contextvars.ContextVar = contextvars.ContextVar("okengine_write_caller", default=None)


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


_policy_cache: dict = {"key": None, "value": None}


def _effective_policy() -> dict:
    """Load composed policy with cheap mtime invalidation for long-lived servers."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    paths = policy_plane.discover_documents(vault)
    key = tuple((str(path), path.stat().st_mtime_ns if path.exists() else None) for path in paths)
    if _policy_cache["key"] != key:
        _policy_cache["value"] = policy_plane.compose_documents(paths)
        _policy_cache["key"] = key
    return _policy_cache["value"]


def _capability_reject(p: Path, operation: str, *, page_type: str = "",
                       changed_fields=(), body_change: str = "none") -> Optional[str]:
    """Enforce the authenticated caller's operation/type/field/body authority.

    This runs before every filesystem mutation.  A rejected attempt is emitted as
    the common structured finding and the human MCP response retains a concise
    policy rule ID and remediation.
    """
    caller = _caller()
    if caller.get("kind") == "admin":
        return None
    actor = str(caller.get("actor") or
                (f"extension:{caller.get('ext_id')}" if caller.get("ext_id") else "unknown"))
    policy = _effective_policy()
    if caller.get("kind") == "extension":
        declared = caller.get("write_capability")
        # Existing extension manifests remain path-scoped until they opt into the
        # richer contract.  Their path authority is still enforced by _wauth_refusal.
        if not declared:
            return None
        policy = dict(policy)
        policy["capabilities"] = dict(policy.get("capabilities") or {})
        policy["capabilities"][actor] = declared
    elif actor not in (policy.get("capabilities") or {}):
        contract, _lane = _output_contract.resolve(caller)
        if isinstance(contract, dict) and not contract.get("_missing") \
                and not contract.get("_invalid_digest"):
            return None
    result = policy_plane.evaluate_capability(
        policy, actor, operation, _rel(p), page_type,
        changed_fields, body_change)
    if result is None:
        return None
    try:
        policy_plane.append_event(Path(os.environ.get("WIKI_PATH") or str(VAULT)), result)
    except OSError:
        pass
    return policy_plane.finding_message(result)


def _contract_reject(p: Path, operation: str, fm: dict, body: str, drift: list[str]) -> Optional[str]:
    """Evaluate the authenticated lane contract immediately before mutation."""
    unknown = []
    for flag in drift:
        if flag.startswith("unknown field(s)") and ":" in flag:
            unknown.extend(x.strip() for x in flag.split(":", 1)[1].split(","))
    findings = _output_contract.evaluate(
        _caller(), operation=operation, namespace=_namespace(p),
        page_type=str(fm.get("type") or ""), frontmatter=fm, body=body or "",
        unknown_fields=unknown, wiki=_wiki())
    if not findings:
        return None
    detail = "; ".join(f"{f['code']}: {f['message']}" for f in findings)
    _append_log(f"- {_today()} output-contract {'report' if os.environ.get('OKENGINE_OUTPUT_CONTRACT_MODE', 'report') != 'enforce' else 'reject'} {_rel(p)} — {detail}")
    if os.environ.get("OKENGINE_OUTPUT_CONTRACT_MODE", "report") != "enforce":
        return None
    return "output_contract." + detail


def _apply_extension_provenance(fm: dict, *, creating: bool,
                                existing_ext_id: Optional[str] = None) -> None:
    """Stamp/preserve `extension_id` server-side and STRIP any caller-supplied value.

    `extension_id` is SERVER-DERIVED provenance (okengine#132/#133): the key disable/orphan/purge
    read, and `extensions purge --yes` HARD-deletes by it — the ONE non-tombstone delete in a
    tombstone-only contract. A client must never set or change it, or (invariant-audit) a scoped
    token forges another extension's id onto a curated page and a later `purge` unlink()s a page
    that extension never wrote, or an extension orphan-proofs its own pages so its purge misses them.
    The create-path stamp alone (only in _create, only for extension callers) left update/patch/
    converge/admin-create wide open. So: strip the incoming value unconditionally at EVERY write, then
    derive it from the scoped token on CREATE, or preserve the immutable create-time stamp on a
    mutation. Stdio/admin writes get no stamp."""
    fm.pop("extension_id", None)                    # never client-settable — kill any forge
    if creating:
        _c = _caller()
        if _c.get("kind") == "extension" and _c.get("ext_id"):
            fm["extension_id"] = _c["ext_id"]        # server-derived from the scoped token
    elif existing_ext_id:
        fm["extension_id"] = existing_ext_id         # provenance is set once, never re-stamped


_REVIEW_MANAGED_FIELDS = {
    "review_state", "review_id", "reviewed_by", "reviewed_on", "reviewed_at", "reviewed_version",
}


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
        return (f"refused: '{path}' is outside extension '{c.get('ext_id')}'"
                f"'s write scope (declared: {c.get('write_scopes')})")
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
    if not (parts[0] == "entities"
            and (len(parts) == 2 or (len(parts) >= 3 and all(len(seg) == 1 for seg in parts[1:-1])))):
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
            return two                              # already at the resharded canonical
        leaf = wiki / "entities" / l1
        if (not (wiki / one).exists() and leaf.is_dir()
                and any(d.is_dir() and len(d.name) == 1 for d in leaf.iterdir())):
            return two                              # this first-letter leaf HAS been resharded
    except OSError:
        pass
    return one                                      # default / un-resharded: one level


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
            rel = rel[len(_cand):]
            break
    rel = rel.lstrip("/")
    while rel == "wiki" or rel.startswith("wiki/"):
        rel = rel[len("wiki"):].lstrip("/")
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


def _partitioned_create_path(p: Path, fm: dict) -> Path:
    """Return the schema-canonical destination for a newly created page.

    ``_safe`` historically normalized only ``entities``.  That left the same
    enforced MCP boundary able to mint flat ``concepts/<slug>`` pages and
    year-only ``sources/YYYY/<slug>`` pages beside their canonical shards.  Use
    the exact helper shared by importers, reshelve, and collision cleanup so all
    namespaces follow one partition contract.  Existing pages remain where
    they are (``write_key`` deliberately converges on them); migration owns
    moving stale layouts.
    """
    if not _CONVERGE_OK:
        return p
    try:
        # SUB-DOMAIN AWARE (walk-up multipack #173): a page at wiki/<subdomain>/entities/<slug> lives
        # in namespace 'entities', not '<subdomain>'. Using rel.parts[0] read the CONTAINER as the
        # namespace, so okf_migrate.is_partitioned('<subdomain>') found no partition config and the
        # page was written FLAT — while the reshelve drain (reshelve.py walks every sub-domain's
        # schema and reshards <subdomain>/entities) then sharded it, re-opening the #54 duplicate-
        # canonical ping-pong for every co-installed vault (invariant-audit #351). write_key preserves
        # the full prefix, so the page shards WITHIN its sub-domain. A flat vault has no container, so
        # _qualified_namespace == rel.parts[0] — byte-identical for every single-pack deployment.
        namespace = _qualified_namespace(p)
        vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
        if not namespace or not okf_migrate.is_partitioned(vault, namespace):
            return p
        key = okf_migrate.write_key(vault, namespace, p.stem, fm)
        return (_wiki() / f"{key}.md").resolve()
    except (OSError, ValueError, IndexError):
        return p


_WIKILINK_FULL = re.compile(r"^\[\[\s*([^\]|#]+?)\s*(?:[#|][^\]]*)?\]\]$")


def _strip_wikilink(s):
    """'[[concepts/x]]' -> 'concepts/x' (drops any #anchor / |display); a non-wikilink value is
    returned unchanged."""
    if isinstance(s, str):
        m = _WIKILINK_FULL.match(s.strip())
        if m:
            return m.group(1).strip()
    return s


def _looks_like_ref_list(v) -> bool:
    """A list that needs canonicalizing: it either contains nested lists (the shape YAML produces
    when a bare `[[x]]` wikilink is used as a value — `[[x]]` parses as a nested flow sequence) or
    holds `[[..]]` wikilink strings."""
    return isinstance(v, list) and (
        any(isinstance(x, list) for x in v)
        or any(isinstance(x, str) and x.strip().startswith("[[") for x in v))


def _flatten_strip(v) -> list:
    """Flatten arbitrarily-nested lists (the `[[..]]` YAML mangling) into a flat list of plain
    wiki-relative path strings, wikilink-stripped, dropping blanks + dups (order-preserving)."""
    out: list = []
    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
            return
        s = _strip_wikilink(x)
        if isinstance(s, str):
            s = s.strip()
        if s and s not in out:
            out.append(s)
    walk(v)
    return out


# Field SHAPES are schema-DECLARED (base-schema `field_shapes`, pack-extensible) rather than
# hardcoded (okengine#196 generalized): a list field authored as a SCALAR string (e.g.
# `aliases: StealC, StealC info-stealer`) would otherwise sail through the open/untyped schema and
# crash a list-consuming lane. The write path coerces scalar -> list for every schema-declared list
# field at the single enforced-write chokepoint, so no such page can enter the vault.
# Fallback if the schema declares no `field_shapes` (older base-schema) — the set okengine#196 first
# hardcoded, now the safety net under the schema-driven resolution.
_FALLBACK_LIST_FIELDS = frozenset({"aliases", "tags", "maintained_by", "discovered_by"})
_base_list_fields_cache = None


def _base_list_fields() -> frozenset:
    """The universal list fields, read once from base-schema `field_shapes` (schema_lib may be
    absent, or the base may predate field_shapes — fall back to the known set either way)."""
    global _base_list_fields_cache
    if _base_list_fields_cache is None:
        try:
            lf = schema_lib.list_fields(schema_lib.base_schema())
        except Exception:
            lf = set()
        _base_list_fields_cache = frozenset(lf) or _FALLBACK_LIST_FIELDS
    return _base_list_fields_cache


def _list_fields_for(page_path) -> set:
    """List fields governing a page: the universal base set ∪ any the page's COMPOSED schema declares
    (so a pack's domain list field is honoured too). Base-only fallback if the schema can't load."""
    lf = set(_base_list_fields())
    if page_path is not None:
        try:
            lf |= schema_lib.list_fields(_governing(page_path))
        except Exception:
            pass
    return lf


# INT-shaped fields are machine-owned COUNTS (a metrics lane stamps them). An agent that misreads
# the field name semantically writes garbage that a numeric-consuming dashboard then renders/sorts —
# live incident: `recent_reports:` hand-set to a LIST of source paths topped the cockpit's
# Most-active table. A digit-string coerces (the intent is unambiguous); anything else REJECTS with
# the field named, the same actionable-feedback loop as a schema reject. Pre-shape schemas declare
# no int fields, so the check is inert there.
_base_int_fields_cache = None


def _base_int_fields() -> frozenset:
    global _base_int_fields_cache
    if _base_int_fields_cache is None:
        try:
            _base_int_fields_cache = frozenset(schema_lib.int_fields(schema_lib.base_schema()))
        except Exception:
            _base_int_fields_cache = frozenset()
    return _base_int_fields_cache


def _int_fields_for(page_path) -> set:
    fields = set(_base_int_fields())
    if page_path is not None:
        try:
            fields |= schema_lib.int_fields(_governing(page_path))
        except Exception:
            pass
    return fields


def _enum_case_coerce(p, fm) -> None:
    """Case-canonicalize enum values IN PLACE before validation (okengine#226): `tlp: clear`
    lands as `CLEAR` instead of rejecting — a case-insensitive match to exactly one allowed
    value is unambiguous intent (same philosophy as the digit-string int coercion). Genuinely
    unknown values still reject downstream (schema_reject_reason/_enum_reject_reason).
    Never raises: a broken schema must not brick a write (the runtime gate is fail-open)."""
    if not isinstance(fm, dict):
        return
    try:
        canonicalize_enum_case(_governing(p), str(fm.get("type") or ""), fm)
    except Exception:
        pass


# ITEM contracts (okengine#211): per-key rules for LIST-OF-DICT fields (field_items — e.g. the
# predictions `evidence:` records). The vocabulary a consumer buckets on (evidence[].direction)
# previously lived only in prompt text, so agent-authored values drifted and the cockpit tally
# silently mis-bucketed them (D1: 18 drifted entries). Enforcement lives HERE — the boundary every
# writer crosses — not in prompts (instruction, not enforcement) and not in consumer synonym maps
# (laundering that masks producer drift). Out-of-enum/wrong-shape REJECTS with field, index, and
# the allowed set named, so an agent retry self-corrects (and, on a page carrying legacy drifted
# entries, effectively backfills them — it must resubmit the full list clean). Schemas that declare
# no field_items make the check inert.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_base_item_rules_cache = None


def _base_item_rules() -> dict:
    global _base_item_rules_cache
    if _base_item_rules_cache is None:
        try:
            _base_item_rules_cache = schema_lib.item_rules(schema_lib.base_schema())
        except Exception:
            _base_item_rules_cache = {}
    return _base_item_rules_cache


def _item_rules_for(page_path) -> dict:
    rules = dict(_base_item_rules())
    if page_path is not None:
        try:
            rules.update(schema_lib.item_rules(_governing(page_path)))
        except Exception:
            pass
    return rules


def _item_shape_reject(p, fm) -> Optional[str]:
    """Validate schema-declared ITEM contracts on list-of-dict fields; None = clean.
    Coerces an unambiguous numeric string in place; rejects out-of-enum / wrong-shape values with
    the exact location named. Non-dict items (legacy prose strings) and absent keys pass — item
    requiredness is not this guard's job, vocabulary/shape integrity is."""
    if not isinstance(fm, dict):
        return None
    for field, keyrules in _item_rules_for(p).items():
        items = fm.get(field)
        if not isinstance(items, list):
            continue
        item_spec = keyrules.get("_item") or {}
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                if item_spec.get("shape") == "dict":
                    return (f"`{field}[{i}]` must be an object — got "
                            f"{type(item).__name__}: {str(item)[:60]!r}")
                continue
            missing = [key for key in sorted(item_spec.get("required") or set())
                       if key not in item or item[key] is None
                       or (isinstance(item[key], str) and not item[key].strip())]
            if missing:
                return (f"`{field}[{i}]` is missing required item field(s): "
                        f"{', '.join(missing)}")
            for key, rule in keyrules.items():
                if key == "_item":
                    continue
                v = item.get(key)
                if v is None:
                    continue
                allowed = rule.get("enum")
                if allowed is not None:
                    if isinstance(v, str) and v not in allowed:
                        # case-variant of exactly one allowed value -> coerce (#226)
                        ci = [a for a in allowed if a.casefold() == v.casefold()]
                        if len(ci) == 1:
                            item[key] = ci[0]
                            continue
                    if not isinstance(v, str) or v not in allowed:
                        return (f"`{field}[{i}].{key}` = {str(v)[:60]!r} is not in the sanctioned "
                                f"vocabulary ({', '.join(sorted(allowed))}). Resubmit the complete "
                                f"list using only those values.")
                    continue
                shape = rule.get("shape")
                if shape == "number":
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        coerced = None
                        if isinstance(v, str):
                            try:
                                coerced = float(v.strip())
                            except ValueError:
                                pass
                        if coerced is None:
                            return (f"`{field}[{i}].{key}` must be a number — got "
                                    f"{type(v).__name__}: {str(v)[:60]!r}")
                        item[key] = coerced                 # unambiguous intent — coerce
                elif shape == "date":
                    if isinstance(v, (datetime.date, datetime.datetime)):
                        continue                            # yaml parses bare ISO dates natively
                    if not (isinstance(v, str) and _ISO_DATE_RE.match(v.strip())):
                        return (f"`{field}[{i}].{key}` must be an ISO date (YYYY-MM-DD) — got "
                                f"{str(v)[:60]!r}")
                elif shape == "str":
                    if not isinstance(v, str):
                        return (f"`{field}[{i}].{key}` must be a string — got {type(v).__name__}")
                elif shape == "bool":
                    if not isinstance(v, bool):
                        return (f"`{field}[{i}].{key}` must be a boolean — got {type(v).__name__}")
                elif shape == "list":
                    if not isinstance(v, list):
                        return (f"`{field}[{i}].{key}` must be a list — got {type(v).__name__}")
                elif shape == "dict":
                    if not isinstance(v, dict):
                        return (f"`{field}[{i}].{key}` must be an object — got {type(v).__name__}")
    return None


def _int_shape_reject(p, fm) -> Optional[str]:
    """Coerce digit-strings in place; return a reject reason when a schema-declared int field holds
    anything else (list/path/prose/bool). None = clean."""
    if not isinstance(fm, dict):
        return None
    for k in _int_fields_for(p):
        v = fm.get(k)
        if v is None or (isinstance(v, int) and not isinstance(v, bool)):
            continue
        if isinstance(v, str) and v.strip().isdigit():
            fm[k] = int(v.strip())                      # unambiguous intent — coerce
            continue
        return (f"field `{k}` must be an integer count (it is machine-computed by a metrics lane) "
                f"— got {type(v).__name__}: {str(v)[:80]!r}. Drop the field; do not hand-author it.")
    return None


def _normalize_refs(fm: dict, list_fields=frozenset()) -> dict:
    """Canonicalize frontmatter values at the single enforced-write chokepoint (so every extension's
    writes are fixed at once). Three coercions:
      - a schema-declared list field written as a scalar string -> a list (okengine#196);
      - a bare `[[x]]` wikilink string -> the plain path `x` (agents write `[[wikilinks]]`, but in a
        frontmatter VALUE that mangles — `field_mapped: [[c/x]]` -> `[[ "c/x" ]]`);
      - a list that mangled into nested lists, or holds `[[..]]` strings, -> a flat list of paths.
    Plain strings and plain lists are left untouched."""
    if not isinstance(fm, dict):
        return fm
    for k, v in list(fm.items()):
        if k in list_fields and isinstance(v, str):
            fm[k] = [s.strip() for s in v.split(",") if s.strip()]   # scalar list-field -> list (#196)
        elif isinstance(v, str):
            fm[k] = _strip_wikilink(v)
        elif _looks_like_ref_list(v):
            fm[k] = _flatten_strip(v)
    return fm


def _coerce_fm(frontmatter_yaml: Union[str, dict, None], page_path=None) -> Optional[dict]:
    """Accept a YAML string OR a dict; return a dict (or None to signal a parse error vs an
    empty/absent value, which returns {}). Frontmatter values are canonicalized via _normalize_refs
    (wikilink -> plain path; scalar -> list for the page's schema-declared list fields). `page_path`
    selects the governing schema's list fields; None falls back to the universal base set."""
    list_fields = _list_fields_for(page_path)
    if frontmatter_yaml is None:
        return {}
    if isinstance(frontmatter_yaml, dict):
        return _normalize_refs(dict(frontmatter_yaml), list_fields)
    try:
        loaded = yaml.safe_load(frontmatter_yaml)
    except Exception:
        return None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        return None
    return _normalize_refs(loaded, list_fields)


def _compose(fm: dict, body: str) -> str:
    """Render frontmatter + body into a page, preserving key order."""
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    body = body or ""
    return f"---\n{fm_text}\n---\n{body}"


def _read_page(p: Path) -> tuple[dict, str]:
    """Split an existing page into (frontmatter dict, body). Empty fm on no match."""
    text = p.read_text(encoding="utf-8", errors="replace")
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    return fm, body


def _review_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review_store() -> Path:
    return _wiki() / "operational" / "reviews"


def _review_record_path(review_id: str) -> Path:
    return _review_store() / f"{_review_digest(review_id)}.yaml"


def _review_page_state(p: Path) -> tuple[dict, str, str, int, str]:
    fm, body = _read_page(p)
    content = p.read_text(encoding="utf-8", errors="replace")
    try:
        version = int(fm.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    subject = _rel(p).removesuffix(".md")
    return fm, body, subject, version, _review_digest(content)


def _structured_review_reasons(fm: dict, body: str, flags=None) -> list[dict]:
    """Convert legacy booleans/write-path strings into durable, explainable reason records."""
    reasons: list[dict] = []
    for flag in (flags or []):
        code = "categorical-confidence" if "categorical" in flag else \
               "changed-after-approval" if "changed after" in flag else \
               "agent-draft" if "degenerate" in flag else "manual"
        reasons.append({"code": code, "detail": str(flag)})
    for conflict in (fm.get("conflicts") or []):
        if isinstance(conflict, dict):
            reasons.append({"code": "conflict", "field": str(conflict.get("field") or ""),
                            "detail": "sources disagree on this field"})
    if re.search(r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)",
                 body or "", re.S | re.I):
        reasons.append({"code": "grounding", "detail": "grounding check flagged an unsupported claim"})
    if not reasons:
        reasons.append({"code": "legacy-unspecified", "detail": "legacy needs_review flag"})
    # Stable de-duplication: the same write-path flag may be surfaced by more than one guard.
    out, seen = [], set()
    for reason in reasons:
        key = (reason.get("code"), reason.get("field"), reason.get("detail"))
        if key not in seen:
            seen.add(key); out.append(reason)
    return out


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
        "version": 1, "review_id": review_id, "subject": subject,
        "subject_version": version, "subject_hash": digest, "state": "open",
        "reasons": reasons, "evidence": [str(v) for v in evidence if str(v).strip()],
        "requested_by": str(requested_by), "requested_at": _now(),
        "assigned_to": None, "history": [], "machine_checks": [],
    }
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(yaml.safe_dump(rec, sort_keys=False, allow_unicode=True), encoding="utf-8")
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


def _assign_review(path: str, reviewer: str, expected_version: int, expected_hash: str,
                   review_id: str | None = None, service: str = "cli") -> dict:
    """Claim the current review request without changing the subject page."""
    candidate = _safe(path)
    if candidate is not None:
        cap = _capability_reject(candidate, "review")
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
    lock = _wiki().parent / ".okengine" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        _, _, subject, version, digest = _review_page_state(p)
        if version != expected_version or not hmac.compare_digest(digest, str(expected_hash)):
            return {"ok": False, "status": 409, "error": "subject changed; refresh before assigning"}
        rec = _ensure_review_request(p)
        if review_id and review_id != rec.get("review_id"):
            return {"ok": False, "status": 409, "error": "review request is stale"}
        if rec.get("state") in {"approved", "rejected", "dismissed"}:
            return {"ok": False, "status": 409, "error": "closed review cannot be assigned"}
        stamp = _now()
        rec["state"] = "in-review"
        rec["assigned_to"] = reviewer
        rec.setdefault("history", []).append({"action": "assign", "state": "in-review",
                                               "assigned_to": reviewer, "at": stamp,
                                               "service": service})
        rec["version"] = int(rec.get("version") or 1) + 1
        rp = _review_record_path(rec["review_id"])
        rp.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=rp.parent,
                                         prefix=".review-record-", delete=False) as f:
            yaml.safe_dump(rec, f, sort_keys=False, allow_unicode=True)
            tmp = Path(f.name)
        try:
            os.replace(tmp, rp)
        finally:
            tmp.unlink(missing_ok=True)
        _append_log(f"- {_today()} review assign {subject} v{version} to {reviewer} via {service}")
        return {"ok": True, "status": 200, "review_id": rec["review_id"],
                "state": "in-review", "assigned_to": reviewer}


def _resolve_review(path: str, decision: str, reviewer: str, note: str,
                    expected_version: int, expected_hash: str,
                    review_id: str | None = None, service: str = "cli") -> dict:
    """Apply one version-locked human decision and its audit record as a single governed action."""
    candidate = _safe(path)
    if candidate is not None:
        cap = _capability_reject(candidate, "review")
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
    lock = _wiki().parent / ".okengine" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        fm, body, subject, version, digest = _review_page_state(p)
        if version != expected_version or not hmac.compare_digest(digest, str(expected_hash)):
            return {"ok": False, "status": 409, "error": "subject changed; refresh before deciding",
                    "current_version": version, "current_hash": digest}
        rec = _ensure_review_request(p)
        if review_id and review_id != rec.get("review_id"):
            return {"ok": False, "status": 409, "error": "review request is stale"}
        if rec.get("state") in {"approved", "dismissed"}:
            return {"ok": False, "status": 409, "error": "review request is already closed"}
        state, remains_flagged = _REVIEW_DECISIONS[decision]
        stamp = _now()
        event = {"decision": decision, "state": state, "decision_by": reviewer,
                 "decision_at": stamp, "decision_note": note or None, "service": service,
                 "subject_version": version, "subject_hash": digest}
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
        old_content = p.read_text(encoding="utf-8", errors="replace")
        old_record = rp.read_text(encoding="utf-8", errors="replace") if rp.exists() else None
        page_tmp = record_tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=p.parent,
                                             prefix=".review-page-", delete=False) as f:
                f.write(new_content); page_tmp = Path(f.name)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=rp.parent,
                                             prefix=".review-record-", delete=False) as f:
                yaml.safe_dump(rec, f, sort_keys=False, allow_unicode=True); record_tmp = Path(f.name)
            os.replace(page_tmp, p); page_tmp = None
            os.replace(record_tmp, rp); record_tmp = None
        except Exception as exc:
            p.write_text(old_content, encoding="utf-8")
            if old_record is None:
                rp.unlink(missing_ok=True)
            else:
                rp.write_text(old_record, encoding="utf-8")
            return {"ok": False, "status": 500, "error": f"atomic review write failed: {exc}"}
        finally:
            if page_tmp: page_tmp.unlink(missing_ok=True)
            if record_tmp: record_tmp.unlink(missing_ok=True)
        _append_log(f"- {_today()} review {decision} {subject} v{version} by {reviewer} via {service}")
        return {"ok": True, "status": 200, "review_id": rec["review_id"], "state": state,
                "subject": subject, "reviewed_version": version, "page_version": version + 1}


def _record_machine_review(path: str, evaluator: str, outcome: str, note: str = "") -> dict:
    """Attach a machine check without clearing or impersonating human approval."""
    if outcome not in {"supported", "unsupported", "unresolved"}:
        return {"ok": False, "status": 400, "error": "invalid machine review outcome"}
    p = _safe(path)
    if p is not None:
        cap = _capability_reject(p, "review")
        if cap:
            return {"ok": False, "status": 403, "error": cap}
    if p is None or not p.is_file():
        return {"ok": False, "status": 404, "error": "subject page not found"}
    lock = _wiki().parent / ".okengine" / "review.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        rec = _ensure_review_request(p)
        check = {"evaluator": str(evaluator or "machine"), "outcome": outcome,
                 "note": str(note or ""), "checked_at": _now()}
        rec.setdefault("machine_checks", []).append(check)
        rec["version"] = int(rec.get("version") or 1) + 1
        rp = _review_record_path(rec["review_id"])
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=rp.parent,
                                         prefix=".review-record-", delete=False) as f:
            yaml.safe_dump(rec, f, sort_keys=False, allow_unicode=True)
            tmp = Path(f.name)
        try:
            os.replace(tmp, rp)
        finally:
            tmp.unlink(missing_ok=True)
        _append_log(f"- {_today()} review-machine {outcome} {rec['subject']} by {check['evaluator']}")
        return {"ok": True, "status": 200, "review_id": rec["review_id"], "state": rec["state"],
                "machine_check": check}


def _frontmatter_error(p: Path) -> Optional[str]:
    """Return a refusal reason when a page appears to have frontmatter but it is
    malformed. Pages with no frontmatter are left to schema validation."""
    text = p.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None
    m = _FM.match(text)
    if not m:
        return "existing page has malformed YAML frontmatter delimiters"
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        return f"existing page has invalid frontmatter YAML: {str(e)[:120]}"
    if not isinstance(fm, dict):
        return "existing page has non-mapping frontmatter"
    return None


def _append_log(line: str) -> None:
    wiki = _wiki()
    wiki.mkdir(parents=True, exist_ok=True)
    log = wiki / "log.md"
    with log.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _rel(p: Path) -> str:
    try:
        return p.relative_to(_wiki()).as_posix()
    except ValueError:
        return p.as_posix()


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
_RESERVED_NAMES = {"log.md", "index.md", "agents.md", "hot.md", "readme.md",
                   "health.md", "bundle.md"}


def _reserved_refuse(p: Path) -> Optional[str]:
    n = p.name.lower()
    # Mirror schema_validator._is_generated_structural EXACTLY: the validator exempts these basenames
    # from conformance, so the write path MUST refuse the same set or the two guards compose into zero
    # protection — an agent forges a conformance-invisible page (invariant-audit M17). That predicate
    # is: any `_`- or `.`-prefixed file, and the whole INDEX family (INDEX.md + INDEX-<anything>, incl.
    # the paginated INDEX-pNN.md). `index-` (lowercased) covers INDEX-glossary etc., not just index-p.
    if (n in _RESERVED_NAMES or n.startswith("index-")
            or p.name.startswith("_") or p.name.startswith(".")):
        return (f"refused: {_rel(p)} is an engine-managed structural/reserved file "
                "— not agent-writable via the MCP write tools (use the file tool only "
                "if a human edit is truly intended)")
    # A pack's schema `reserved_files` is ALSO a write-path refusal — docs/authoring-a-pack.md and
    # okengine-conformance-spec.md define it as "paths the MCP write path refuses", and the
    # validator already EXEMPTS them from conformance (schema_validator reserved_files). Without
    # this the write path never read the key, so declaring a file reserved made it MORE writable
    # (invariant-audit). UNION with the engine set above (never un-protect log/index); basename
    # match, lowercased, mirroring the validator. Degrades to the engine set if the schema is
    # unreadable.
    try:
        reserved = _governing(p).get("reserved_files")
    except Exception:
        reserved = None
    if reserved and n in {str(r).lower() for r in reserved}:
        return (f"refused: {_rel(p)} is a pack-reserved file (schema `reserved_files`) "
                "— not agent-writable via the MCP write tools")
    return None


def _tombstone_refuse(cur_fm: dict, p: Path) -> Optional[str]:
    """Never resurrect a tombstoned page. The converge lane already refuses a write to a tombstoned
    id (id-based); update/patch/append operate by PATH and read a retained tombstone file, so they
    must refuse it here too or an agent silently un-tombstones it (invariant-audit M18). Re-tombstoning
    or recording a successor is the tombstone_entity tool's job, not a plain content write."""
    if str(cur_fm.get("status") or "").strip().lower() == "tombstoned":
        return (f"refused: {_rel(p)} is tombstoned — write to its successor (superseded_by) or use "
                "tombstone_entity; never resurrect a tombstoned page")
    return None


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

def _namespace(p: Path) -> str:
    """Knowledge namespace for a page (e.g. 'predictions', 'entities').

    Sub-domain aware (okengine#173 walk-up multipack): in a co-installed vault a page lives at
    ``wiki/<subdomain>/<namespace>/…`` where the sub-domain dir carries its OWN schema.yaml — the
    namespace is the dir BELOW that container, not the container itself. Reading the container as
    the namespace silently broke the enforced write path for every sub-domain vault: per-namespace
    create/update permissions never matched (the human-only `findings` guard was BYPASSED on
    update/patch/tombstone), and every create was rejected as an 'undeclared namespace'. For a
    flat vault (no nested schema.yaml) the loop never advances, so this is identical to the old
    ``parts[0]`` behavior."""
    try:
        rel = p.relative_to(_wiki())
    except ValueError:
        return ""
    parts = rel.parts
    if not parts:
        return ""
    wiki = _wiki()
    i = 0
    # skip leading sub-domain container dirs (each carries its own schema.yaml); never the filename
    while i < len(parts) - 1 and (wiki.joinpath(*parts[: i + 1]) / "schema.yaml").is_file():
        i += 1
    return parts[i]


def _qualified_namespace(p: Path) -> str:
    """Like _namespace, but KEEPS the sub-domain container prefix: 'acme/entities' for
    wiki/acme/entities/foo (walk-up multipack), 'entities' for a flat vault. This is the namespace
    okf_migrate.write_key / is_partitioned expect — they resolve the governing schema by walking up
    from wiki/<namespace> (so the leaf 'entities' drives the partition config) yet the returned key
    preserves the full prefix, so a sub-domain page shards WITHIN its sub-domain. A flat vault has no
    container prefix, so this equals _namespace / rel.parts[0] exactly (identical to the old path)."""
    try:
        rel = p.relative_to(_wiki())
    except ValueError:
        return ""
    parts = rel.parts
    if not parts:
        return ""
    wiki = _wiki()
    i = 0
    while i < len(parts) - 1 and (wiki.joinpath(*parts[: i + 1]) / "schema.yaml").is_file():
        i += 1
    return "/".join(parts[: i + 1])


def _entities_scope(rel: str) -> str:
    """The sub-domain container prefix of an entities-namespace page rel: '' for a root page
    (entities/s/x), 'acme' for a walk-up page (acme/entities/s/x). Entity dedup is scoped to a single
    sub-domain — a co-installed vault's sub-domains are separate knowledge bases and must not
    cross-merge two same-named entities (invariant-audit #351). The identity index only holds entities
    pages, so every candidate rel carries the 'entities' segment."""
    parts = rel.split("/")
    return "/".join(parts[: parts.index("entities")]) if "entities" in parts else ""


_PERM_KEYS = ("create", "update", "delete")


def _ns_perm(policy: dict, ns: str) -> dict:
    perms = (policy or {}).get("permissions") or {}
    base = dict(perms.get("default") or {})
    nscfg = (perms.get("namespaces") or {}).get(ns) or {}
    # A typo'd permission key (e.g. `creat: false`) was SILENTLY DROPPED by the allowlist, so the
    # namespace fell back to the (usually open) default — a human-authored `findings` ns could go
    # agent-writable with no error (invariant-audit LOW #58). FAIL CLOSED on an unknown key so the
    # typo is caught at the gate instead of quietly opening the namespace.
    unknown = [k for k in nscfg if k not in _PERM_KEYS]
    if unknown:
        raise ValueError(f"namespace '{ns}' permissions has unknown key(s) {unknown} "
                         f"(valid: {list(_PERM_KEYS)}) — a typo would silently default the namespace open")
    base.update({k: v for k, v in nscfg.items() if k in _PERM_KEYS})
    return base


def _policy_reject(p: Path, fm: dict, op: str, prev: dict | None = None) -> Optional[str]:
    """HARD structural check only (namespace create/update rights). None => allowed.
    Review concerns are SOFT — see `_review_flags`, which never blocks a write."""
    policy = governing_policy(str(p))
    if not policy:
        return None
    ns = _namespace(p)
    perm = _ns_perm(policy, ns)
    if op == "create" and perm.get("create") is False:
        return f"namespace '{ns}' is not agent-writable (create denied; human-authored)"
    if op == "update" and perm.get("update") is False:
        return f"namespace '{ns}' is not agent-writable (update denied; human-authored)"
    return None


def _namespace_reject(p: Path) -> Optional[str]:
    """A knowledge page must land in a schema-DECLARED namespace. The write tools take a
    literal agent-supplied path and `_namespace()` is just its top dir, so an agent can drift a
    `type: source` page into a stray `source/` (singular) instead of the schema's `sources/`
    — a fork the dashboards/index/assembler never see (okengine#115, same class as the cwd
    split-brain #110). Reject an undeclared namespace, offering the closest declared one as a
    hint. No-op when the pack declares no namespaces (nothing to enforce against); excluded
    engine-internal dirs (operational/, dashboards/) are allowed."""
    ns = _namespace(p)
    if not ns:
        return None
    try:
        schema = _governing(p)
        declared = schema_lib.knowledge_namespaces(schema)
        allowed = declared | schema_lib.excluded_dirs(schema)
    except Exception:                       # pragma: no cover - schema load is best-effort
        return None
    if not declared or ns in allowed:
        return None
    hint = difflib.get_close_matches(ns, sorted(declared), n=1)
    suggest = f" — did you mean '{hint[0]}/'?" if hint else ""
    return (f"namespace '{ns}/' is not declared in schema.yaml (declared knowledge "
            f"namespaces: {sorted(declared)}){suggest} — a page written here forks into a "
            f"stray tree the dashboards/index never see (okengine#115)")


def _type_namespace_reject(p: Path, fm: dict) -> Optional[str]:
    """A page whose `type` has a canonical HOME namespace must be CREATED there — not drifted into
    another DECLARED namespace. `_namespace_reject` catches an *undeclared* stray (`source/` vs
    `sources/`); this catches a valid type in the *wrong declared* namespace: a `type: source` page
    written under `concepts/` (both declared, but source belongs in `sources/`) — a fork the
    dashboards/index/type-scoped panels never see (okengine#276). Home from the governing schema's
    `type_namespaces` else the engine-core convention (schema_lib.type_home_namespace). No-op when the
    home is unknown or not a declared namespace here (never a vacuous reject); excluded dirs allowed."""
    typ = str((fm or {}).get("type") or "").strip()
    if not typ:
        return None
    ns = _namespace(p)
    if not ns:
        return None
    try:
        schema = _governing(p)
        home = schema_lib.type_home_namespace(schema, typ)
        declared = schema_lib.knowledge_namespaces(schema)
        excluded = schema_lib.excluded_dirs(schema)
    except Exception:                       # pragma: no cover - schema load is best-effort
        return None
    if not home or home not in declared:    # no rule / home isn't a namespace here -> don't enforce
        return None
    if ns == home or ns in excluded:
        return None
    return (f"type '{typ}' belongs in '{home}/', but this page is being created under '{ns}/' — a "
            f"page in the wrong namespace forks the graph (type-scoped panels/indices never see it, "
            f"okengine#276). Write it under '{home}/'.")


def _type_ns_reject_on_change(p: Path, new_fm: dict, cur_fm: dict) -> Optional[str]:
    """`_type_namespace_reject` for the MUTATING lanes (update/patch/converge). The create-time guard
    was CREATE-ONLY, so update/patch/converge could rewrite a page's `type` to one whose home is a
    different namespace, forking the graph exactly as a bad create would (invariant-audit). Only
    reject when the type is actually CHANGING — a legacy page already in the 'wrong' namespace stays
    editable (grandfathered); a NEW drift of the type out of its home namespace is blocked."""
    if str((new_fm or {}).get("type") or "") == str((cur_fm or {}).get("type") or ""):
        return None
    return _type_namespace_reject(p, new_fm)


def _review_flags(p: Path, fm: dict, prev: dict | None = None) -> list[str]:
    """Return review reasons (flag, do NOT block). prev=None => create. A value
    that is unchanged from `prev` never re-flags (so backfills/no-ops don't churn
    the review queue)."""
    policy = governing_policy(str(p))
    if not policy:
        return []
    review = policy.get("review") or {}
    flags: list[str] = []

    cfield = review.get("confidence_field") or "confidence"
    review_vals = {str(v).lower() for v in (review.get("confidence_review_values") or [])}
    if review_vals and cfield in fm:
        val = str(fm.get(cfield) or "").strip().lower()
        prev_val = str((prev or {}).get(cfield) or "").strip().lower() if prev is not None else None
        if val in review_vals and not (prev is not None and prev_val == val):
            flags.append(f"agent asserted categorical `{cfield}: {fm.get(cfield)}`")

    for k in (review.get("review_on_change_fields") or []):
        if fm.get(k) not in (None, "") and fm.get(k) != (prev or {}).get(k):
            flags.append(f"agent set/changed review field `{k}`")
    return flags


# OKF envelope + assembler/bookkeeping keys allowed on any page, so unknown-field flagging
# (okengine#46) targets only genuine domain drift, never the universal scaffolding.
_OKF_ALWAYS = {"type", "name", "title", "tlp", "version", "last_updated", "created", "updated",
               "needs_review", "conflicts", "assembled_from", "aliases", "refs", "tags",
               "status", "id", "raw", "sources", "source"}
_okf_always_cache = None


def _okf_always() -> set:
    """`_OKF_ALWAYS` UNION the base-schema `common_optional` universals (id/description/confidence/
    maintained_by/discovered_by/sensitivity/source_kind/publisher/reliability/credibility/severity/…).
    The drift check must treat every base-schema universal — and the provenance the write path itself
    stamps (maintained_by/discovered_by via _stamp_maintainer) — as KNOWN scaffolding, else
    update_entity flags the engine's own stamped fields as domain drift on every update (spurious
    needs_review + _review-queue noise). Read once from schema_lib.base_schema() with the same
    fallback as _base_list_fields (schema_lib may be absent, or the base may predate the key)."""
    global _okf_always_cache
    if _okf_always_cache is None:
        try:
            base_universal = set(schema_lib.base_schema().get("common_optional") or [])
        except Exception:
            base_universal = set()
        _okf_always_cache = _OKF_ALWAYS | base_universal
    return _okf_always_cache


def _normalize_drift(fm: dict, p: Path) -> tuple[dict, list[str]]:
    """Converge frontmatter on the schema's vocabulary BEFORE write (okengine#46): rename alias
    keys to their canonical name, map aliased values, and surface unknown fields for review.
    Returns (normalized_fm, unknown-field flags). No-op when the pack declares no drift policy."""
    out = dict(fm)
    # Type aliases are a write-boundary migration, not merely a validation
    # exception: accepting `threat_actor` without storing canonical `actor`
    # would keep fragmenting the entity corpus (#245).
    try:
        out["type"] = schema_lib.canonical_type(_governing(p), out.get("type"))
    except Exception:
        pass
    pol = drift_policy(str(p))
    if not pol:
        return out, []
    for alias, canon in (pol.get("field_aliases") or {}).items():   # country -> suspected_origin
        if alias in out:
            v = out.pop(alias)
            if out.get(canon) in (None, "", [], {}):
                out[canon] = v
    for field, vmap in (pol.get("value_aliases") or {}).items():    # CN -> China ; active -> live
        if field in out and isinstance(vmap, dict):
            cur = out[field]
            out[field] = [vmap.get(x, x) for x in cur] if isinstance(cur, list) else vmap.get(cur, cur)
    flags: list[str] = []
    allowed = (pol.get("allowed") or {}).get(str(out.get("type") or ""))
    if isinstance(allowed, list):
        known = _okf_always() | set(allowed) | set((pol.get("field_aliases") or {}).values())
        unknown = sorted(k for k in out if k not in known)
        if unknown:
            flags.append(f"unknown field(s) for type `{out.get('type')}` "
                         f"(not in schema): {', '.join(unknown)}")
    return out, flags


def _append_review_queue_once(p: Path, reason: str) -> bool:
    """Ensure one outstanding queue row per canonical page.

    A model tool call and its runner-owned completion receipt are separate
    transactions.  The call can therefore succeed and be replayed after a
    crash or invalid receipt.  Serialize the read/append boundary and make the
    page path the queue identity; later reasons remain available in log.md.
    """
    wiki = _wiki()
    wiki.mkdir(parents=True, exist_ok=True)
    queue = wiki / "_review-queue.md"
    lock = wiki.parent / ".okengine" / "review-queue.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        if not queue.exists():
            queue.write_text(
                "---\ntitle: Review Queue\n---\n\n"
                "# Review Queue\n\nAgent-flagged pages awaiting human review "
                "(highlight, not a gate — the writes already landed).\n\n",
                encoding="utf-8",
            )
        identity = f"**{_rel(p)}**"
        if identity in queue.read_text(encoding="utf-8"):
            return False
        with queue.open("a", encoding="utf-8") as f:
            f.write(f"- {_today()} {identity} — {reason}\n")
        return True


def _queue_review(p: Path, flags: list[str]) -> str:
    """Append a flagged page to wiki/_review-queue.md + log it. Returns a note to
    append to the tool result (empty if no flags). The write itself already
    succeeded — this only highlights, never blocks."""
    if not flags:
        return ""
    reason = "; ".join(flags)
    created = _append_review_queue_once(p, reason)
    action = "review-flag" if created else "review-flag already-queued"
    _append_log(f"- {_today()} mcp-write {action} {_rel(p)} — {reason}")
    _ensure_review_request(p, flags)
    state = "flagged for review" if created else "already queued for review"
    return f" — {state} ({len(flags)} reason(s))"


# --- plain logic helpers (tested directly) -------------------------------

def _alias_matches(cur_fm: dict, stem: str, incoming_name: str, incoming_aliases: set) -> bool:
    """primary-name<->alias bidirectional match between an existing page and the incoming one."""
    cur_name = id_lib.normalize_key(str(cur_fm.get("name") or cur_fm.get("title") or stem))
    cur_aliases = cur_fm.get("aliases") or []
    if isinstance(cur_aliases, str):
        cur_aliases = [a.strip() for a in cur_aliases.split(",") if a.strip()]
    elif not isinstance(cur_aliases, list):
        cur_aliases = []
    cur_aliases = {id_lib.normalize_key(str(a)) for a in cur_aliases if str(a).strip()}
    return incoming_name in cur_aliases or cur_name in incoming_aliases


def _alias_hits(p: Path, incoming_name: str, incoming_aliases: set) -> "list[tuple[Path, dict]]":
    """LIVE entity pages whose name<->alias matches the incoming page (okengine#324).

    Consults the pre-built id-index name/alias maps (O(#matches)); only the candidate REL PATHS come
    from the index — each hit is still read from disk to confirm the match against the current file
    (the index can lag a concurrent edit) and to hand its frontmatter to the converge/collision logic.
    FALLS BACK to the full entities/ scan (the pre-#324 behavior) when the loaded index is a pre-v2
    artifact with no identity maps, so dedup is never blind in the window before the refresh cron
    rewrites a v2 artifact."""
    self_rel = _rel(p)
    self_scope = _entities_scope(self_rel)   # '' for a root page, 'acme' for acme/entities/x
    reg = _registry()
    if not reg.name_to_rels and not reg.alias_to_rels:   # pre-v2 artifact -> full scan (old behavior)
        # Scan the incoming page's OWN sub-domain entities dir (walk-up multipack): root entities/ or
        # <sub>/entities/. Root-only rglob missed sub-domain entities entirely (invariant-audit #351).
        scope_dir = (_wiki() / self_scope / "entities") if self_scope else (_wiki() / "entities")
        candidates = [(c, c) for c in scope_dir.rglob("*.md")]
    else:
        rels = set(reg.alias_to_rels.get(incoming_name, []))          # incoming name == existing alias
        for a in incoming_aliases:
            rels |= set(reg.name_to_rels.get(a, []))                  # existing name == incoming alias
        rels.discard(self_rel)
        # SAME sub-domain only — a walk-up vault's sub-domains are separate knowledge bases; converging
        # an 'acme' entity into a root (or 'beta') twin sharing a name would be a cross-domain false
        # merge. Root pages ('' scope) match only other root pages (invariant-audit #351).
        rels = {r for r in rels if _entities_scope(r) == self_scope}
        candidates = [(_wiki() / rel, rel) for rel in sorted(rels)]
    hits: list[tuple[Path, dict]] = []
    for candidate, _key in candidates:
        try:
            if candidate.resolve() == p.resolve():
                continue
        except OSError:
            continue
        try:
            cur_fm, _ = _read_page(candidate)
        except OSError:
            continue
        if str(cur_fm.get("status") or "").lower() == "tombstoned":
            continue
        if _alias_matches(cur_fm, candidate.stem, incoming_name, incoming_aliases):
            hits.append((candidate, cur_fm))
    return hits


def _dedup_on_create(path: str, p: Path, fm: dict, body: str) -> Optional[str]:
    """Identity-based dedup for create_entity (okengine#98/#99/#100).

    The duplicate-canonical class is caused by `create_entity` keying on the
    on-disk PATH: every cosmetic variant of the same entity (different shard dir,
    wrong namespace, `Akira` vs `akira`, `vulnerability--cve-x` vs `cve-x`) is a
    new path, so it created a SECOND canonical the assembler never reconciles.
    The fix is to key on IDENTITY, not the filename: derive the page's stable id
    from its CONTENT (authority field, else minted slug) — exactly as converge
    does — and refuse to mint a second canonical for an id that already lives
    elsewhere. The path band-aids in !40/!41 fight this at the wrong layer; here
    the path is irrelevant to identity, which is the design intent (§5a of
    docs/design/composable-okpacks.md).

    Returns a result string when this handled the write (converged into the
    existing canonical, or flagged+refused a slug collision); None to let the
    normal create proceed. Mutates `fm` to stamp the derived `id` so the new page
    is resolvable forever after. No-op (returns None) when the converge/id libs
    are unavailable or no id can be derived."""
    if not _CONVERGE_OK:
        return None
    # Names and aliases are identity evidence too. A source may call an actor by
    # an alias already curated on the canonical page (UNC6240 vs ShinyHunters);
    # minted-id-only dedup misses that and creates a second entity. Match only
    # primary-name↔alias (not alias↔alias), and only when the hit is unique.
    if _namespace(p) == "entities":
        incoming_name = id_lib.normalize_key(str(fm.get("name") or fm.get("title") or p.stem))
        incoming_aliases = fm.get("aliases") or []
        if isinstance(incoming_aliases, str):
            incoming_aliases = [a.strip() for a in incoming_aliases.split(",") if a.strip()]
        elif not isinstance(incoming_aliases, list):
            incoming_aliases = []
        incoming_aliases = {id_lib.normalize_key(str(a)) for a in incoming_aliases if str(a).strip()}
        hits = _alias_hits(p, incoming_name, incoming_aliases)
        if len(hits) == 1:
            existing_path, existing_fm = hits[0]
            existing_id, _ = _page_id_and_kind(
                existing_fm, _governing(existing_path), "entities", existing_path.stem
            )
            fm["id"] = existing_id
            return _converge(path, fm, body, _alias_verified=True)
        if len(hits) > 1:
            rels = ", ".join(_rel(hit[0]) for hit in hits[:5])
            _flag(path, f"ambiguous entity alias on create; matches {rels}")
            return f"refused: entity alias matches multiple canonicals ({rels}) — flagged for review"
    namespace = _namespace(p)
    try:
        schema = _governing(p)      # sub-domain aware (okengine#177); namespace is the bare mint scope
        pid, kind = _page_id_and_kind(fm, schema, namespace, p.stem)
    except Exception:               # pragma: no cover - id derivation is best-effort
        return None
    if not pid:
        return None
    fm["id"] = pid                  # stamp the content-derived id onto the new page
    existing_rel = _registry().resolve(pid)
    if not existing_rel:
        return None                 # genuinely new identity -> create normally
    existing_path = _wiki() / existing_rel
    if not existing_path.exists() or existing_path.resolve() == p.resolve():
        return None                 # stale index entry or the same page -> create normally
    # An existing canonical already owns this id at a different path.
    if kind == "authority":
        # Same real-world entity (authority ids are globally unique to the type)
        # -> converge into the canonical instead of duplicating it.
        return _converge(path, fm, body)
    # Minted-slug collision: two pages, possibly different entities, that slugged
    # the same. Never auto-merge a slug id -> flag for human review and refuse.
    _flag(path, f"slug id collision on create: {pid} already used by {existing_rel}")
    return (f"refused: slug id {pid} already used by {existing_rel} — "
            "flagged for review (slug ids never auto-merge)")


def _prov_pack() -> str:
    """The DEPLOYMENT's pack identity (pack.yaml `name`), injected as OKENGINE_PACK at deploy time.
    Deployment-pinned, never client/agent-supplied, so composition provenance can't be spoofed.
    Empty in a legacy single-pack deploy without the env (then provenance simply isn't stamped)."""
    return os.environ.get("OKENGINE_PACK", "").strip()


def _stamp_maintainer(fm: dict, *, creation: bool) -> None:
    """Composition provenance (okengine#90 P3): union this deployment's pack into `maintained_by`
    (the list of packs that have written the page) and, on CREATION, set `discovered_by` (the first
    attributor). Idempotent; a no-op when OKENGINE_PACK is unset."""
    pack = _prov_pack()
    if not pack:
        return
    prov = fm.get("maintained_by")
    prov = list(prov) if isinstance(prov, (list, tuple)) else ([prov] if prov else [])
    if pack not in prov:
        prov.append(pack)
    fm["maintained_by"] = prov
    if creation:
        fm.setdefault("discovered_by", pack)


# --- future-date guard ------------------------------------------------------
# The envelope's record-keeping dates say when a page WAS written/touched — a future value is
# always fabricated (a weekly-brief lane hallucinated published: <next Sunday> onto an empty
# stub, despite its prompt explicitly forbidding a guessed date; prompts are the unenforced
# half). Enforced HERE — the boundary every writer crosses. Deliberately NARROW: only the
# record-keeping fields — domain dates (a KEV due_date, an event date, a contract end) are
# legitimately future and are never checked. +1 day tolerance absorbs TZ skew (a UTC-thinking
# model just past midnight UTC is "tomorrow" relative to a US-eastern host clock).
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _future_date_reject(fm: dict, fields=_RECORD_DATE_FIELDS) -> Optional[str]:
    try:
        today = datetime.date.fromisoformat(_today())
    except ValueError:          # unparseable test override — never block writes on guard plumbing
        return None
    limit = today + datetime.timedelta(days=1)
    for k in fields:
        v = fm.get(k)
        d = None
        if isinstance(v, datetime.datetime):    # before date: datetime IS a date subclass
            d = v.date()
        elif isinstance(v, datetime.date):
            d = v
        elif isinstance(v, str):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", v.strip())
            if m:
                try:
                    d = datetime.date.fromisoformat(m.group(1))
                except ValueError:
                    d = None
        if d and d > limit:
            return (f"{k}: {d.isoformat()} is in the future (today is {today.isoformat()}) — "
                    f"record-keeping dates must be the ACTUAL write date, never a guessed or "
                    f"future one; use today's date")
    return None


# --- briefing wikilink guard --------------------------------------------------------------
# Briefings are ANALYSIS pages that cite existing knowledge — every [[wikilink]] on one must
# resolve, or the flagship page a human reads daily ships dead links (live incident: the daily
# brief invented slugs from memory — [[entities/q/quimarat]] for the real quimat-rat page — and
# the broken-wikilinks drain's >=3-inbound wake gate treats 1-ref brief links as orphan noise
# forever). Scoped to briefings/ ONLY: source pages legitimately forward-reference entities that
# don't exist yet (the stub-creation drain depends on that), so a vault-wide check would break
# the ingest pattern. Rejection carries did-you-mean suggestions so the lane model can retry
# with the real slug — the same feedback loop schema rejections use.
_WIKILINK = re.compile(r"\[\[([^\]|#\n]+)")
_STRICT_LINK_NS = ("briefings",)
# Namespaces where an unresolvable wikilink is a DEFECT worth a SOFT needs_review flag at write time
# (curated content), vs sources/indicators where a forward-ref to a not-yet-created page is the norm.
# Briefings get the HARD reject (_briefing_link_reject); these get a flag — surface broken links AT
# WRITE so they're attributable and don't just accrue for the drains to chase (link-audit 2026-07-09).
# A cheap per-link existence check, NOT a full-vault scan (that would reintroduce the per-write cost
# the id-index fix removed).
_LINK_REVIEW_NS = ("concepts", "entities")


def _wikilink_resolves(t: str) -> bool:
    wiki = _wiki()
    if (wiki / f"{t}.md").is_file():
        return True                                    # literal path (incl. an already-sharded link)
    parts = t.split("/")
    if len(parts) >= 2 and parts[-1][:1].isalnum():
        ns, base = parts[0], parts[-1]
        b = base[0].lower()
        if (wiki / ns / b / f"{base}.md").is_file():
            return True                                # first-letter shard (entities/qilin -> entities/q/qilin)
        if len(base) > 1 and (wiki / ns / b / base[1].lower() / f"{base}.md").is_file():
            return True                                # second-letter reshard (oversized shard)
    return False


def _unresolvable_link_flags(p: Path, body: Optional[str]) -> list:
    """SOFT review flag (never a reject) for a curated-namespace page that INTRODUCES unresolvable
    wikilinks — a write-time backstop so broken links are attributable, without blocking the organic
    forward-refs that sources/importers rely on (link-audit 2026-07-09)."""
    if _namespace(p) not in _LINK_REVIEW_NS or not body:
        return []
    bad, seen = [], set()
    for m in _WIKILINK.finditer(body):
        t = (m.group(1) or "").strip().strip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if not t or t in seen:
            continue
        seen.add(t)
        if "/" not in t:
            bad.append(f"[[{t}]] (bare name)")
        elif not _wikilink_resolves(t):
            bad.append(f"[[{t}]] (no such page)")
    if not bad:
        return []
    return [f"{len(bad)} unresolvable wikilink(s): " + "; ".join(bad[:5]) + (" …" if len(bad) > 5 else "")]


# A `sources:` entry that uses the SINGULAR `source/` page-path — a structurally-invalid spelling of
# the schema's plural `sources/` namespace. EVERY observed entity-backfill hallucination cited sources
# this way (apt35 → source/mandiant/…, nightshade → source/darkread-…, apt29 → source/cisa/…). Legit
# citations use plural `sources/…`, and a plural forward-ref to a not-yet-created source page is
# TOLERATED (importers write an entity before its source in the same batch — okengine#196's coercion
# test relies on it), so we do NOT touch plural — corpus-audit's dangling-ref detector (#336) covers
# plural fabrications after the fact. Provenance LABELS ("MITRE ATT&CK") carry spaces and never match.
_SINGULAR_SOURCE_REF = re.compile(r"^source/[a-z0-9][a-z0-9._/-]+$")


def _entity_sources(fm: dict) -> list:
    raw = fm.get("sources")
    if raw is None:
        raw = fm.get("source")
    return raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])


def _fabricated_source_reject(p: Path, fm: dict, prev_fm: Optional[dict] = None) -> Optional[str]:
    """HARD-reject an entity whose `sources:` uses the invalid SINGULAR `source/` namespace — the
    entity-backfill hallucination signature (okengine#348 follow-up). The schema's source namespace is
    plural `sources/`; a singular `source/<…>` page-ref is never valid, and every fabricated cohort page
    cited sources exactly this way. Plural `sources/…` refs are left alone (forward-refs are legitimate;
    corpus-audit catches plural dangling refs). On UPDATE, refs ALREADY on the page are grandfathered —
    the lane resends the full list, so block only NEWLY-introduced fabrication, never freeze a page that
    carries a legacy bad ref (e.g. apt29's pre-existing source/cisa/… stays editable)."""
    if _namespace(p) != "entities":
        return None
    grandfathered = {str(s).strip() for s in _entity_sources(prev_fm)} if prev_fm else set()
    bad = []
    for s in _entity_sources(fm):
        if not isinstance(s, str) or s.strip() in grandfathered:
            continue
        t = s.strip().strip("[]").removeprefix("wiki/")
        if t.endswith(".md"):
            t = t[:-3]
        if _SINGULAR_SOURCE_REF.match(t):               # singular `source/` — invalid namespace = fabrication
            bad.append(s)
    if not bad:
        return None
    return ("rejected: sources use the invalid singular `source/` namespace (the schema's source "
            "namespace is plural `sources/`): " + "; ".join(bad[:5]) + (" …" if len(bad) > 5 else "")
            + " — this is the entity-backfill fabrication signature; cite an EXISTING `sources/<…>` "
              "page or a provenance label (e.g. 'MITRE ATT&CK').")


_SLUG_DESIGNATION = re.compile(r"(?:^|[-_])([a-z]{2,16})[-_](\d{3,8})(?:[-_]|$)", re.I)
_VALUE_DESIGNATION = re.compile(r"\b([a-z]{2,16})[-_ ](\d{3,8})\b", re.I)


def _identity_contradiction_flags(p: Path, fm: dict) -> list[str]:
    """Flag a filename designation that contradicts the page's own name/aliases.

    Domain-neutral shape only (``prefix-number``). A disagreement is review evidence, not a hard
    reject: aliases can legitimately document historical designations, but a polished profile must
    be quarantined until a human resolves the identity.
    """
    if _namespace(p) != "entities" or not isinstance(fm, dict):
        return []
    slug_ids = {(prefix.casefold(), number) for prefix, number in _SLUG_DESIGNATION.findall(p.stem)}
    if not slug_ids:
        return []
    values = [fm.get("name"), fm.get("title")]
    aliases = fm.get("aliases") or []
    values.extend(aliases if isinstance(aliases, list) else [aliases])
    declared = {(prefix.casefold(), number) for value in values if value is not None
                for prefix, number in _VALUE_DESIGNATION.findall(str(value))}
    conflicts = sorted({(prefix, slug_num, declared_num)
                        for prefix, slug_num in slug_ids for dprefix, declared_num in declared
                        if prefix == dprefix and slug_num != declared_num})
    if not conflicts:
        return []
    detail = ", ".join(f"{prefix.upper()}-{slug_num} vs {prefix.upper()}-{declared_num}"
                       for prefix, slug_num, declared_num in conflicts[:4])
    return [f"entity identity contradiction between path and declared name/aliases: {detail}"]


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
_DEGEN_FENCE = re.compile(r"```.*?```", re.DOTALL)
_DEGEN_WIKILINK = re.compile(r"\[\[[^\]]*\]\]")
_DEGEN_STOP = re.compile(r"[.!?;:\n,]")
_DEGEN_MAX_RUN = 250


def _degeneration_flags(body: Optional[str]) -> list:
    """SOFT review flag (never a reject) for a DEGENERATE generation — a repetition-loop word-salad
    (comma/wikilink-aware). See the block comment above."""
    if not body:
        return []
    prose = _DEGEN_WIKILINK.sub(" ", _DEGEN_FENCE.sub("\n", body))   # code + wikilink-lists are not prose
    worst = max((len(seg.split()) for seg in _DEGEN_STOP.split(prose)), default=0)
    if worst > _DEGEN_MAX_RUN:
        return [f"degenerate: {worst}-word unpunctuated run (repetition loop)"]
    return []


def _briefing_link_reject(p: Path, body: Optional[str]) -> Optional[str]:
    if _namespace(p) not in _STRICT_LINK_NS or not body:
        return None
    targets = []
    for m in _WIKILINK.finditer(body):
        t = m.group(1).strip().strip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if t:
            targets.append(t)
    if not targets:
        return None
    # one walk builds both the exact rel-path set and the basename->rel-path map
    rels: set[str] = set()
    by_base: dict[str, str] = {}
    for f in WIKI.rglob("*.md"):
        rel = f.relative_to(WIKI).as_posix()[:-3]
        rels.add(rel)
        by_base.setdefault(f.stem, rel)
    broken = []
    n_source = n_knowledge = 0                             # classify resolved links for the cite check
    for t in dict.fromkeys(targets):                      # de-dup, keep order
        rel = t if t in rels else (by_base.get(t) if "/" not in t else None)
        if rel:
            ns = rel.split("/")[0]
            if ns == "sources":
                n_source += 1
            elif ns not in ("dashboards", "operational"):  # a substantive claim, not a nav/meta link
                n_knowledge += 1
            continue
        base = t.split("/")[-1]
        if base in by_base:                               # right page, wrong dir/shard
            broken.append(f"[[{t}]] — did you mean [[{by_base[base]}]]?")
            continue
        near = difflib.get_close_matches(base, list(by_base), n=2, cutoff=0.6)
        hint = " — did you mean " + " or ".join(f"[[{by_base[n]}]]" for n in near) + "?" if near else ""
        broken.append(f"[[{t}]] (no such page){hint}")
    if broken:
        return ("briefing links must resolve to existing pages (cite what you actually read; "
                "do not guess slugs): " + "; ".join(broken))
    # A briefing that makes ENTITY claims must be verifiable: at least one resolvable
    # [[sources/...]] link. Footnotes and code-span paths are not links an analyst can click,
    # and the brief lanes keep omitting real citations despite the prompt (unenforced half).
    # A pure "nothing happened this week" briefing (no knowledge links) is exempt.
    if n_knowledge and not n_source:
        return (f"briefing cites {n_knowledge} entit{'y' if n_knowledge == 1 else 'ies'} but no source — "
                "every development must end with a resolvable [[sources/<path>]] link (a footnote or "
                "a code-span path is not a citation an analyst can verify)")
    return None


def _create(path: str, frontmatter_yaml: Union[str, dict], body: str = "",
            _contract_operation: str = "create") -> str:
    p = _safe(path)
    if p is None:
        return (f"refused: unsafe wiki path (must stay inside wiki/; basename must contain no "
                f"whitespace and entity basenames must be ≤{_MAX_ENTITY_SLUG_LEN} characters)")
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
        return "rejected: frontmatter_yaml is not a valid YAML mapping"
    # Route every partitioned namespace through the shared canonical writer
    # contract before the existence, authorization, and schema gates.  A caller
    # may supply a logical flat key; the physical write must never create a
    # flat-vs-sharded duplicate (#262).
    canonical_p = _partitioned_create_path(p, fm)
    if canonical_p != p:
        p = canonical_p
        _wa = _wauth_refusal(p)
        if _wa:
            return _wa
        if p.exists():
            return f"refused: {_rel(p)} already exists — use update_entity"
    cap = _capability_reject(p, "create", page_type=str(fm.get("type") or ""),
                             changed_fields=fm.keys(),
                             body_change="replace" if body else "none")
    if cap:
        return f"rejected: {cap}"
    # Underscore type spellings are the observed taxonomy-bypass class
    # (`threat_actor` beside canonical `threat-actor`/`actor`). Permit one only
    # when the governing schema explicitly declares it as a type or type_alias.
    ptype = str(fm.get("type") or "").strip()
    if "_" in ptype:
        try:
            schema = _governing(p)
            allowed_types = schema_lib.canonical_types(schema) | set(schema_lib.type_aliases(schema))
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
    fsr = _fabricated_source_reject(p, fm)   # a cited source must exist — no fabricated `source/…` (#348)
    if fsr:
        return fsr
    fdr = _future_date_reject(fm)
    if fdr:
        return f"rejected: {fdr}"
    blr = _briefing_link_reject(p, body)
    if blr:
        return f"rejected: {blr}"
    bir = _body_integrity_reject("", body)
    if bir:
        return f"rejected: {bir}"
    fm, drift = _normalize_drift(fm, p)            # converge on schema vocab (okengine#46)
    # Server stamps version/last_updated if absent, and an IMMUTABLE `created` on first write
    # (the OKF-envelope creation date = when the page was ingested; unlike last_updated it never
    # shifts on later edits, so "recent ingest" / age reporting is accurate).
    if "version" not in fm:
        fm["version"] = 1
    if "created" not in fm:
        fm["created"] = _now()
    if "last_updated" not in fm:
        fm["last_updated"] = _now()
    _stamp_maintainer(fm, creation=True)   # composition provenance (okengine#90 P3)
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
    flags = review_invalidation + drift + _review_flags(p, fm, prev=None) + _identity_contradiction_flags(p, fm) + \
        _unresolvable_link_flags(p, body) + _degeneration_flags(body)
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
    dedup = _dedup_on_create(path, p, fm, body)
    if dedup is not None:
        return dedup
    content = _compose(fm, body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    # Write-synchronous id claim: the new page is now resolvable by id, so a later
    # cosmetic-variant write dedupes against it instead of forking a canonical.
    if _CONVERGE_OK and isinstance(fm.get("id"), str):
        try:
            _registry().by_id[fm["id"]] = _rel(p)
        except Exception:           # pragma: no cover - registry is best-effort
            pass
    # Write-synchronous name/alias claim (okengine#324): keep _alias_hits' index current WITHIN this
    # process so two matching entity pages created back-to-back dedup against each other (the pre-#324
    # rglob saw the just-written page on disk; the index must too). Tombstoned pages excluded.
    if _CONVERGE_OK and _namespace(p) == "entities" \
            and str(fm.get("status") or "").lower() != "tombstoned":
        try:
            _registry()._add_identity(_rel(p), fm)
        except Exception:           # pragma: no cover - registry is best-effort
            pass
    ver = fm.get("version", 1)
    _append_log(f"- {_today()} mcp-write create {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"created {_rel(p)} v{ver}{note}"


# Identity + provenance fields that are IMMUTABLE after creation: `id` never changes (id_lib.py:22),
# and created/created_by/discovered_by are the create-time provenance the converge lane already
# protects (converge._PROVENANCE_KEYS, M19). But _update merged caller frontmatter wholesale
# (new_fm.update(patch)) and _patch re-parsed the edited text wholesale — neither preserved these, so
# update_entity/patch_entity could freely rewrite an id or forge provenance: exactly the class the
# audit closed on converge, left open on the other two mutating lanes (invariant-audit HIGH #3).
# extension_id is handled separately by _apply_extension_provenance; maintained_by is additive.
_IMMUTABLE_KEYS = ("id", "created", "created_by", "discovered_by")


def _preserve_immutable(new_fm: dict, cur_fm: dict) -> list:
    """Force any immutable identity/provenance field that ALREADY EXISTS back to its stored value,
    overriding a caller change — so a read-modify-write that echoes the whole frontmatter is fine,
    but a forged CHANGE is silently reverted. A field the page LACKS may still be set (backfilling an
    id/created onto a legacy or non-compliant page is legitimate — and, since base-schema makes `id`
    required, necessary). The invariant is 'never CHANGES', not 'never appears'. Returns the fields
    whose change was reverted, so the caller can flag the attempt for review."""
    reverted = []
    for key in _IMMUTABLE_KEYS:
        if key in cur_fm:                       # exists -> immutable: restore (no-op if unchanged)
            if new_fm.get(key) != cur_fm[key]:
                reverted.append(key)
            new_fm[key] = cur_fm[key]
    return reverted


def _update(path: str, frontmatter_yaml: Union[str, dict, None] = None,
            body: Optional[str] = None) -> str:
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
    ferr = _frontmatter_error(p)
    if ferr:
        return f"rejected: {ferr} — repair the frontmatter before updating"
    cur_fm, cur_body = _read_page(p)
    tr = _tombstone_refuse(cur_fm, p)   # never resurrect a tombstoned page (invariant-audit M18)
    if tr:
        return tr
    new_fm = dict(cur_fm)
    patch = {}
    if frontmatter_yaml is not None:
        patch = _coerce_fm(frontmatter_yaml, p)
        if patch is None:
            return "rejected: frontmatter_yaml is not a valid YAML mapping"
        # Future-date guard on ONLY the fields this patch supplies: a legacy page that already
        # carries a bad future date must stay fixable by an update that doesn't touch dates.
        fdr = _future_date_reject(patch, fields=tuple(k for k in _RECORD_DATE_FIELDS if k in patch))
        if fdr:
            return f"rejected: {fdr}"
        new_fm.update(patch)
    cap = _capability_reject(
        p, "update", page_type=str(new_fm.get("type") or cur_fm.get("type") or ""),
        changed_fields=patch.keys(), body_change="replace" if body is not None else "none")
    if cap:
        return f"rejected: {cap}"
    # extension_id is server-derived: strip any client forge, keep the create-time stamp (M14).
    _apply_extension_provenance(new_fm, creating=False, existing_ext_id=cur_fm.get("extension_id"))
    # id + created/created_by/discovered_by are immutable — revert any caller change (audit HIGH #3).
    reverted_immutable = _preserve_immutable(new_fm, cur_fm)
    review_invalidation = _apply_review_governance(new_fm, cur_fm)
    new_fm, drift = _normalize_drift(new_fm, p)    # converge on schema vocab (okengine#46)
    new_body = cur_body if body is None else body
    if body is not None:                           # only when this update REWRITES the body
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
    _stamp_maintainer(new_fm, creation=False)   # add this pack as a maintainer (okengine#90 P3)
    _enum_case_coerce(p, new_fm)
    isr = _int_shape_reject(p, new_fm)
    if isr:
        return f"rejected: {isr}"  # existing file left untouched
    itr = _item_shape_reject(p, new_fm)
    if itr:
        return f"rejected: {itr}"  # existing file left untouched
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"  # existing file left untouched
    tnr = _type_ns_reject_on_change(p, new_fm, cur_fm)   # type can't drift out of its home ns (audit)
    if tnr:
        return f"rejected: {tnr}"  # existing file left untouched
    fsr = _fabricated_source_reject(p, new_fm, prev_fm=cur_fm)   # block NEW fabricated source refs (#348)
    if fsr:
        return fsr  # existing file left untouched
    flags = review_invalidation + drift + _review_flags(p, new_fm, prev=cur_fm) + _identity_contradiction_flags(p, new_fm) + \
        (_unresolvable_link_flags(p, new_body) + _degeneration_flags(new_body) if body is not None else []) + \
        ([f"immutable field change reverted: {', '.join(reverted_immutable)}"] if reverted_immutable else [])
    contract_reject = _contract_reject(p, "update", new_fm, new_body, drift)
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, new_body)
    reason = schema_reject_reason(str(p), content)
    if reason:
        return f"rejected: {reason}"  # existing file left untouched
    p.write_text(content, encoding="utf-8")
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
    ferr = _frontmatter_error(p)        # invariant-audit M18: malformed YAML -> refuse, don't wipe it
    if ferr:
        return f"refused: {ferr} — fix the page's frontmatter before tombstoning (would silently wipe it)"
    cur_fm, cur_body = _read_page(p)
    changed = {"status", "tombstone_reason", "last_updated", "version"}
    if superseded_by:
        changed.add("superseded_by")
    cap = _capability_reject(p, "tombstone", page_type=str(cur_fm.get("type") or ""),
                             changed_fields=changed)
    if cap:
        return f"rejected: {cap}"
    # a tombstone IS an update — it must clear the same namespace permission
    # matrix as every other mutation (found via okengine#166: an agent-read-only
    # lookup/ namespace could still be tombstoned through this one path)
    pol = _policy_reject(p, cur_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"  # file left untouched
    new_fm = dict(cur_fm)
    new_fm["status"] = "tombstoned"
    new_fm["tombstone_reason"] = reason
    if superseded_by:
        new_fm["superseded_by"] = superseded_by
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
    p.write_text(content, encoding="utf-8")
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


def _flag(path: str, note: str) -> str:
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    cap = _capability_reject(p, "flag")
    if cap:
        return f"rejected: {cap}"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    if p.is_file():
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
    created = _append_review_queue_once(p, clean_note)
    action = "flag" if created else "flag already-queued"
    _append_log(f"- {_today()} mcp-write {action} {_rel(p)} — {clean_note}")
    if created:
        return f"flagged {_rel(p)} for review — queued in _review-queue.md"
    return f"already flagged {_rel(p)} for review — queue unchanged"


# --- G1.1: body-preserving surgical edits + field-loss guard --------------
# update_entity does whole-body REPLACE (fine for frontmatter-only / wholesale
# rewrites); these add the SURGICAL primitives the drains need without resending
# the whole page: patch_entity (exact one-shot replace, like the Edit tool) and
# append_to_section (append into a `## heading` block). Both re-validate against
# schema, run the review gate, and — unlike the file tool — enforce a hard
# FIELD-LOSS guard: an edit may not drop an existing frontmatter key.

_STAMP_KEYS = {"version", "last_updated", "needs_review"}


def _field_loss(prev_fm: dict, new_fm: dict) -> Optional[str]:
    """Reject an edit that DROPS a frontmatter key present before (server-stamped
    keys excepted). Value changes and additions are fine — only deletions block."""
    dropped = sorted(k for k in (prev_fm or {})
                     if k not in (new_fm or {}) and k not in _STAMP_KEYS)
    if dropped:
        return ("edit would drop existing frontmatter field(s): "
                + ", ".join(dropped) + " — curated fields must be preserved")
    return None


def _stamp(new_fm: dict, cur_fm: dict) -> None:
    try:
        new_fm["version"] = int(new_fm.get("version", cur_fm.get("version", 1))) + 1
    except (TypeError, ValueError):
        new_fm["version"] = 2
    new_fm["last_updated"] = _now()


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
    if not old_string:
        return "rejected: old_string is empty"
    if old_string == new_string:
        return "rejected: old_string and new_string are identical"
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(old_string)
    if n == 0:
        return "rejected: old_string not found in the page (verify with a read first)"
    if n > 1:
        return f"rejected: old_string matches {n} places — add surrounding context to make it unique"
    cur_fm, _cur_body = _read_page(p)
    tr = _tombstone_refuse(cur_fm, p)   # invariant-audit M18
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
    changed_fields = {key for key in set(cur_fm) | set(new_fm)
                      if cur_fm.get(key) != new_fm.get(key)}
    old_match = _FM.match(text)
    old_body = old_match.group(2).lstrip("\n") if old_match else ""
    candidate_body = m.group(2).lstrip("\n")
    cap = _capability_reject(
        p, "patch", page_type=str(new_fm.get("type") or cur_fm.get("type") or ""),
        changed_fields=changed_fields,
        body_change="replace" if candidate_body != old_body else "none")
    if cap:
        return f"rejected: {cap}"
    # patch_entity is a full write chokepoint: apply the SAME shape coercion _create/_update/_converge
    # do (#196 — a schema-declared list field authored as a scalar becomes a list; bare [[wikilink]]
    # values are stripped), so an edit can't land a malformed shape that poisons a downstream lane.
    new_fm = _coerce_fm(new_fm, p)
    fl = _field_loss(cur_fm, new_fm)   # compare in the pre-normalized space (cur_fm is un-normalized)
    if fl:
        return f"rejected: {fl}"
    # patch is a full write chokepoint — converge on the schema vocabulary (okengine#46) exactly like
    # create/update/converge, or an aliased field/value introduced by a surgical edit lands raw and
    # forks the vault silently (invariant-audit). After field-loss so a rename isn't seen as a drop.
    new_fm, drift = _normalize_drift(new_fm, p)
    # extension_id is server-derived: strip any patched-in forge, keep the create-time stamp (M14).
    _apply_extension_provenance(new_fm, creating=False, existing_ext_id=cur_fm.get("extension_id"))
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
    tnr = _type_ns_reject_on_change(p, new_fm, cur_fm)   # type can't drift out of its home ns (audit)
    if tnr:
        return f"rejected: {tnr}"
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    bir = _body_integrity_reject(_cur_body, body)
    if bir:
        return f"rejected: {bir}"
    blr = _briefing_link_reject(p, body)   # briefings must have only resolvable links + a citation
    if blr:
        return f"rejected: {blr}"
    _stamp(new_fm, cur_fm)
    fd = _future_date_reject(new_fm)   # the boundary every writer crosses (invariant-audit)
    if fd:
        return f"rejected: {fd}"        # file left untouched
    # Same review gate as create/update: drift (aliased field/value), degenerate body, and dead
    # wikilinks must be attributable at THIS write, not only via a nightly report-only lint that
    # carries no write attribution (invariant-audit — patch bypassed all three).
    flags = review_invalidation + drift + _review_flags(p, new_fm, prev=cur_fm) + _identity_contradiction_flags(p, new_fm) + \
        _unresolvable_link_flags(p, body) + _degeneration_flags(body) + \
        ([f"immutable field change reverted: {', '.join(reverted_immutable)}"] if reverted_immutable else [])
    contract_reject = _contract_reject(p, "patch", new_fm, body, drift)
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"  # file left untouched
    p.write_text(content, encoding="utf-8")
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write patch {_rel(p)} v{ver}")
    note = _queue_review(p, flags)
    return f"patched {_rel(p)} v{ver}{note}"


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
_MALFORMED_HEADING_RE = re.compile(r"^##[ \t]+##(?:[ \t]+|$)", re.MULTILINE)
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
# These headings are generated by the read MCP from the live backlink graph. Authoring them into a
# canonical page freezes derived state into prose and makes the same panel appear twice to readers.
_DERIVED_PANEL_HEADINGS = {
    "incoming backlinks",
    "outbound references",
    "referenced by",
    "references",
}


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
        return ("body introduces reader-derived panel heading(s): "
                + ", ".join(introduced)
                + " — backlink/reference panels are computed and must not be authored")
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
    idx = level = None
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if m and m.group(2).strip().lower() == want:
            idx, level = i, len(m.group(1)); break
    if idx is None:
        base = body.rstrip("\n")
        prefix = (base + "\n\n") if base else ""
        return f"{prefix}## {heading}\n\n{block}\n", "section created"
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j; break
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
    if not (text or "").strip():
        return "rejected: text is empty"
    ferr = _frontmatter_error(p)        # invariant-audit M18: malformed YAML -> refuse, don't wipe it
    if ferr:
        return f"refused: {ferr} — fix the page's frontmatter before appending (would silently wipe it)"
    cur_fm, cur_body = _read_page(p)
    cap = _capability_reject(p, "append", page_type=str(cur_fm.get("type") or ""),
                             body_change="append")
    if cap:
        return f"rejected: {cap}"
    tr = _tombstone_refuse(cur_fm, p)   # tombstone-guard (a separate concern)
    if tr:
        return tr
    new_body, where = _insert_into_section(cur_body, heading, text)
    bir = _body_integrity_reject(cur_body, new_body)
    if bir:
        return f"rejected: {bir}"
    new_fm = dict(cur_fm)
    blr = _briefing_link_reject(p, new_body)   # append is the hot path for growing a briefing's
    if blr:                                    # `## Recent activity` — apply the same dead-link guard
        return f"rejected: {blr}"
    pol = _policy_reject(p, new_fm, "update", prev=cur_fm)
    if pol:
        return f"rejected: {pol}"
    _stamp(new_fm, cur_fm)
    fd = _future_date_reject(new_fm)   # the boundary every writer crosses (invariant-audit)
    if fd:
        return f"rejected: {fd}"        # file left untouched
    # append is the documented hot path for growing a briefing's `## Recent activity`, so the
    # SAME degenerate-content + dead-link review gate create/update apply must fire here on the
    # newly-appended text — else a degenerate run lands unflagged (invariant-audit).
    flags = _review_flags(p, new_fm, prev=cur_fm) + \
        _unresolvable_link_flags(p, text) + _degeneration_flags(text)
    contract_reject = _contract_reject(p, "append", new_fm, new_body, [])
    if contract_reject:
        return f"rejected: {contract_reject}"
    if flags:
        new_fm["needs_review"] = True
    content = _compose(new_fm, new_body)
    rej = schema_reject_reason(str(p), content)
    if rej:
        return f"rejected: {rej}"
    p.write_text(content, encoding="utf-8")
    ver = new_fm["version"]
    _append_log(f"- {_today()} mcp-write append {_rel(p)} v{ver} ({heading})")
    note = _queue_review(p, flags)
    return f"appended to '{heading}' in {_rel(p)} v{ver} ({where}){note}"


# --- converge-on-write (P2): upsert by id, merge under page+field ownership ---

_registries: dict = {}


def _registry() -> "id_index.IdIndex":
    """Lazy in-process id->path index over the current vault, kept write-synchronous
    (updated on each converge write). Keyed by vault path so tests stay isolated."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    k = str(vault)
    if k not in _registries:
        _registries[k] = id_index.build(vault)
    return _registries[k]


def _governing(p: Path) -> dict:
    """The governing schema for a PAGE — sub-domain aware (okengine#177). Resolve by the page's
    LOCATION (its wiki-relative dir, which merged_schema walks up to the nearest schema.yaml /
    composed artifact), NOT a bare namespace. Passing the bare namespace lost the sub-domain and
    always resolved the ROOT schema, so for a walk-up sub-domain page the id/type-authority/owner
    guards saw root while the permission/shape guards (governing_policy -> _find_schema, a page-path
    walk-up) saw the sub-domain — the split-brain. Now both resolve the same governing schema."""
    vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
    try:
        nsdir = p.parent.relative_to(_wiki()).as_posix()   # 'acme/entities' | 'entities' | 'entities/a'
    except ValueError:
        nsdir = ""                                          # page outside wiki/ — vault-root schema
    if nsdir == ".":
        nsdir = ""
    return schema_lib.merged_schema(vault, nsdir)


def _page_id_and_kind(fm: dict, schema: dict, namespace: str, stem: str) -> tuple[str, str]:
    """(id, kind) for an incoming page: honour an explicit valid `id`, else derive
    from the type's authority binding (authority id) or a minted slug."""
    ptype = str(fm.get("type") or "").strip()
    authority, id_field = schema_lib.type_id_authority(schema, ptype)
    explicit = fm.get("id")
    if isinstance(explicit, str) and id_lib.is_id(explicit.strip()):
        pid = explicit.strip()
        scope = id_lib.parse_id(pid)[0]
        kind = "authority" if (authority and scope == id_lib.normalize_key(authority)) else "slug"
        return pid, kind
    return id_lib.derive_id(authority=authority,
                            local_id=(fm.get(id_field) if authority else None),
                            minted_scope=namespace,
                            slug_source=id_lib.natural_key(fm, stem))


def _converge(path: str, frontmatter_yaml: Union[str, dict], body: str = "",
              pack: str = "", remove: str = "", _alias_verified: bool = False) -> str:
    """Upsert a page by id: merge into a live page that already carries the id
    (page+field ownership), else create + claim. (RFC composable-okpacks §5a.)"""
    pack = pack or _prov_pack()   # deployment-pinned provenance + field ownership (okengine#90 P3)
    if not _CONVERGE_OK:
        return "rejected: converge unavailable (id/schema libs not importable)"
    p = _safe(path)
    if p is None:
        return "refused: path outside the vault wiki/"
    _wa = _wauth_refusal(path)
    if _wa:
        return _wa
    _rr = _reserved_refuse(p)
    if _rr:
        return _rr
    fm = _coerce_fm(frontmatter_yaml, p)
    if fm is None:
        return "rejected: frontmatter_yaml is not a valid YAML mapping"
    cap = _capability_reject(p, "converge", page_type=str(fm.get("type") or ""),
                             changed_fields=fm.keys(),
                             body_change="replace" if body else "none")
    if cap:
        return f"rejected: {cap}"
    namespace = _namespace(p)
    schema = _governing(p)          # sub-domain aware (okengine#177); namespace is the bare mint scope
    pid, kind = _page_id_and_kind(fm, schema, namespace, p.stem)
    if not pid:
        return "rejected: cannot determine page id"
    fm["id"] = pid
    reg = _registry()
    existing_rel = reg.resolve(pid)

    if existing_rel and reg.is_tombstoned(pid):
        return (f"refused: id {pid} is tombstoned — write to its successor, "
                "never resurrect a tombstoned id")

    if existing_rel:
        existing_path = _wiki() / existing_rel
        same = existing_path.exists() and existing_path.resolve() == p.resolve()
        if not same:
            if kind != "authority" and not _alias_verified:
                _flag(path, f"slug id collision: {pid} already used by {existing_rel}")
                return (f"refused: slug id {pid} already used by {existing_rel} — "
                        "flagged for review (slug ids never auto-merge)")
            p = existing_path                       # authority id -> the canonical page
            _wa = _wauth_refusal(p)                 # re-authorize: the redirect can point OUTSIDE
            if _wa:                                 # the caller's declared scope (okengine#178)
                return _wa
            _rr = _reserved_refuse(p)               # re-check reserved: the id-index CAN resolve a
            if _rr:                                 # pack-reserved page (id_index._skip only knows the
                return _rr                          # engine set, not schema reserved_files) — the
                                                    # original _reserved_refuse was on the pre-redirect
                                                    # path, so a converge could land on a reserved page
                                                    # (invariant-audit HIGH — every other mutating lane
                                                    # re-checks reserved on the exact path it writes)
        if p.is_file():
            cur_fm, cur_body = _read_page(p)
            # invariant-audit M14: the registry tombstone check above reads the id-index, which is
            # up to 6h stale (rebuilt on a cron). A page hand-tombstoned on disk (status: tombstoned)
            # since the last index rebuild would slip past it and be RESURRECTED by this merge. Trust
            # the on-disk status too.
            if str(cur_fm.get("status") or "").strip().lower() == "tombstoned":
                return (f"refused: {_rel(p)} is tombstoned on disk (status: tombstoned) — "
                        "write to its successor, never resurrect a tombstoned page")
            ftype = str(cur_fm.get("type") or fm.get("type") or "").strip()
            owner = schema_lib.type_owner(schema, ftype)
            fos = schema_lib.field_owners(schema, ftype)
            rm = [s.strip() for s in (remove or "").split(",") if s.strip()]
            merged, dec = converge.merge_frontmatter(
                cur_fm, fm, owner_pack=owner, caller_pack=(pack or None),
                field_owners=fos, remove=rm)
            merged, drift = _normalize_drift(merged, p)   # okengine#46: converge on schema vocab —
            # extension_id is server-derived: converge.merge treats it as a _SERVER_KEY, but strip any
            # residual forge and re-assert the on-disk stamp so it can never be reassigned (M14).
            _apply_extension_provenance(merged, creating=False, existing_ext_id=cur_fm.get("extension_id"))
            review_invalidation = _apply_review_governance(merged, cur_fm)
            new_body = cur_body if not body else body     # same guard as _create/_update (invariant-audit)
            _stamp(merged, cur_fm)
            if pack:
                merged["last_modified_by"] = pack
            blr = _briefing_link_reject(p, new_body)   # briefings must cite resolvable pages — the same
            if blr:                                    # guard create/update/patch/append enforce (L3)
                return f"rejected: {blr}"
            if body:
                bir = _body_integrity_reject(cur_body, new_body)
                if bir:
                    return f"rejected: {bir}"
            fd = _future_date_reject(merged)   # the boundary every writer crosses (invariant-audit)
            if fd:
                return f"rejected: {fd}"        # file left untouched
            _enum_case_coerce(p, merged)        # case-canonicalize enums (#226) before the guards
            isr = _int_shape_reject(p, merged)  # machine-owned int fields (recent_reports/total_mentions):
            if isr:                             # create/update/patch all reject here; _dedup_on_create
                return f"rejected: {isr}"       # redirects create_entity INTO converge, so this lane must
                                                # enforce it too or the guard is bypassed (invariant-audit)
            itr = _item_shape_reject(p, merged)  # item contracts (#211): same bypass reasoning
            if itr:
                return f"rejected: {itr}"
            # Converge is an agent write into an EXISTING page: apply the same
            # write-governance as update_entity, not a bypass (#21). HARD namespace
            # permission gate first (a human-authored namespace refuses the write,
            # leaving the page untouched)...
            pol = _policy_reject(p, merged, "update", prev=cur_fm)
            if pol:
                return f"rejected: {pol}"
            tnr = _type_ns_reject_on_change(p, merged, cur_fm)   # type can't drift out of home ns (audit)
            if tnr:
                return f"rejected: {tnr}"
            # ...then SOFT review flags (categorical confidence verdict, changed
            # review field) — flag, never block.
            review = review_invalidation + drift + _review_flags(p, merged, prev=cur_fm) + _identity_contradiction_flags(p, merged) + _unresolvable_link_flags(p, new_body) + \
                (_degeneration_flags(new_body) if body else [])   # degenerate body attributable at write (M15)
            contract_reject = _contract_reject(p, "converge", merged, new_body, drift)
            if contract_reject:
                return f"rejected: {contract_reject}"
            if review:
                merged["needs_review"] = True
            content = _compose(merged, new_body)
            rej = schema_reject_reason(str(p), content)
            if rej:
                return f"rejected: {rej}"
            p.write_text(content, encoding="utf-8")
            reg.by_id[pid] = _rel(p)
            ver = merged.get("version")
            _append_log(f"- {_today()} mcp-write converge {_rel(p)} (id {pid}) v{ver}")
            flags = list(review)
            if dec.conflicts:
                flags += [f"field `{k}`: {pack or 'caller'} attempted {a!r}, owner value {c!r} kept"
                          for k, c, a in dec.conflicts]
            note = _queue_review(p, flags) if flags else ""
            return (f"converged into {_rel(p)} (id {pid}) v{ver}: "
                    f"+{len(dec.added)} added, ~{len(dec.updated)} updated, "
                    f"-{len(dec.removed)} removed, {len(dec.conflicts)} conflict(s){note}")

    # new id -> create + claim it in the registry (record the creating pack)
    if pack:
        fm.setdefault("maintained_by", [pack])
        fm.setdefault("discovered_by", pack)
    result = _create(path, fm, body, _contract_operation="converge")
    if result.startswith("created"):
        cp = _safe(path)
        if cp is not None:
            reg.by_id[pid] = _rel(cp)
    return result


# --- FastMCP wrappers (delegate to the plain helpers) --------------------

try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("okengine-write")

    @mcp.tool()
    def create_entity(path: str, frontmatter_yaml: str, body: str = "") -> str:
        """Create a NEW wiki page. Refuses if the page already exists (use
        update_entity). Validates the composed page against the governing
        schema.yaml BEFORE writing; on reject, writes nothing. Stamps
        version:1 + last_updated if absent, then appends a log.md line."""
        return _create(path, frontmatter_yaml, body)

    @mcp.tool()
    def update_entity(path: str, frontmatter_yaml: str = "",
                      body: Optional[str] = None) -> str:
        """Update an EXISTING wiki page. Merges frontmatter keys (if given) and replaces the
        body when `body` is provided. Pass body="" to intentionally CLEAR the body; OMIT body
        (leave it null) to keep the current body. Bumps version, sets last_updated. Validates
        before writing; on reject the existing file is untouched."""
        fm = frontmatter_yaml if frontmatter_yaml else None
        return _update(path, fm, body)          # None -> keep body; "" -> clear; text -> replace

    @mcp.tool()
    def tombstone_entity(path: str, reason: str,
                         superseded_by: str = "") -> str:
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
        return _append_section(path, heading, text)

    @mcp.tool()
    def resolve_review(path: str, decision: str, reviewer: str, expected_version: int,
                       expected_hash: str, note: str = "", review_id: str = "") -> dict:
        """Record a version-locked human review decision. Approval/dismissal may clear the
        review flag; every disposition writes an auditable review record and page projection."""
        return _resolve_review(path, decision, reviewer, note, expected_version, expected_hash,
                               review_id or None, service="mcp")

    @mcp.tool()
    def assign_review(path: str, reviewer: str, expected_version: int,
                      expected_hash: str, review_id: str = "") -> dict:
        """Assign the exact current review request to a human reviewer."""
        return _assign_review(path, reviewer, expected_version, expected_hash,
                              review_id or None, service="mcp")

    @mcp.tool()
    def record_machine_review(path: str, evaluator: str, outcome: str, note: str = "") -> dict:
        """Attach a machine evidence check without clearing human-required review state."""
        return _record_machine_review(path, evaluator, outcome, note)

    @mcp.tool()
    def converge_entity(path: str, frontmatter_yaml: str, body: str = "",
                        pack: str = "", remove: str = "") -> str:
        """Upsert a page by its IDENTITY (not its path). The id is taken from the
        frontmatter `id` or derived (an external-authority id when the type binds
        one, else a minted slug). If a LIVE page already carries this id, MERGE
        into it under page+field ownership: the owning pack may change any field; a
        non-owner may ADD new keys or change only fields it is granted; conflicts
        are flagged, never clobbered. If the id is new, create + claim it.
        Authority ids converge across packs; minted-slug collisions are flagged,
        never auto-merged. A write to a tombstoned id is refused (never resurrect).
        `pack` names the calling pack (for ownership; omit in single-pack use).
        `remove` is a comma-separated list of fields to drop — permitted only for
        fields the caller owns (a non-owner removal is flagged, not applied)."""
        return _converge(path, frontmatter_yaml, body, pack, remove)

except ImportError:  # pragma: no cover - mcp absent (e.g. host test env)
    mcp = None


class _ScopedWriteAuth:
    """ASGI middleware for the networked write surface (okengine#132): resolve
    `Bearer <token>` -> caller, 401 if unknown. The configured admin token
    (OKENGINE_MCP_TOKEN / OKENGINE_WRITE_TOKEN) keeps FULL write; an extension token
    from the vault store is limited to its write scopes. This surface is what lets an
    out-of-process sidecar reach okengine-write at all — stdio cannot."""

    def __init__(self, app, admin_token: str):
        self.app, self.admin_token = app, admin_token

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") != "/healthz":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            token = provided[7:] if provided.startswith("Bearer ") else ""
            caller = None
            if self.admin_token and hmac.compare_digest(token, self.admin_token):
                caller = {"kind": "admin", "actor": "admin",
                          "write_scopes": None, "ext_id": None}
            else:
                rec = _scope.resolve(token)
                if rec is not None:
                    ext_id = rec.get("ext_id")
                    caller = {"kind": "extension", "ext_id": ext_id,
                              "actor": rec.get("actor") or f"extension:{ext_id}",
                              "write_scopes": rec.get("write_scopes") or [],
                              "write_capability": rec.get("write_capability") or {}}
            if caller is None:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            _caller_var.set(caller)
        await self.app(scope, receive, send)


def _review_http_app():
    """Small review-only REST surface for a protected operator UI.

    It deliberately does not expose generic entity mutation. `_ScopedWriteAuth` wraps the whole
    service, while the operation itself enforces version/hash locking and the review state machine.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    review_app = FastAPI(title="OKEngine review write", docs_url=None, redoc_url=None)

    @review_app.get("/healthz")
    def healthz():
        return {"ok": True}

    @review_app.post("/review/resolve")
    async def review_resolve(request: StarletteRequest):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = _resolve_review(
            str(data.get("path") or ""), str(data.get("decision") or ""),
            str(data.get("reviewer") or ""), str(data.get("note") or ""),
            data.get("expected_version"), str(data.get("expected_hash") or ""),
            str(data.get("review_id") or "") or None, service=str(data.get("service") or "cockpit"))
        return JSONResponse(result, status_code=int(result.get("status") or 500))

    @review_app.post("/review/assign")
    async def review_assign(request: StarletteRequest):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = _assign_review(
            str(data.get("path") or ""), str(data.get("reviewer") or ""),
            data.get("expected_version"), str(data.get("expected_hash") or ""),
            str(data.get("review_id") or "") or None, service=str(data.get("service") or "cockpit"))
        return JSONResponse(result, status_code=int(result.get("status") or 500))

    @review_app.post("/review/machine")
    async def review_machine(request: StarletteRequest):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = _record_machine_review(str(data.get("path") or ""),
                                        str(data.get("evaluator") or "machine"),
                                        str(data.get("outcome") or ""), str(data.get("note") or ""))
        return JSONResponse(result, status_code=int(result.get("status") or 500))
    return review_app


# Keep in sync with okengine-mcp/server.py DEFAULT_LOCAL_TOKEN / _LOOPBACK (the read server). The
# well-known local token is PUBLIC (it ships in the source); the enforced WRITE path must never serve
# it beyond loopback (invariant-audit CRITICAL — the read server fails closed on exactly this, but
# write_server only checked the token was non-empty, so a networked write bound off-loopback with the
# seeded compose default served UNAUTHENTICATED full create/update/converge/tombstone access).
DEFAULT_LOCAL_TOKEN = "okengine-local"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _resolve_write_auth(env: dict, host: str) -> str:
    """The admin bearer token for the networked write transport, or raise SystemExit (fail CLOSED).

    - empty (no WRITE_TOKEN and no MCP_TOKEN) → refuse: writes must be authenticated.
    - the built-in DEFAULT token while bound beyond loopback → refuse unless
      OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN=1 (the public token can't guard a networked WRITE surface).
      On loopback the default is painless, same as the read server."""
    admin = (env.get("OKENGINE_WRITE_TOKEN") or env.get("OKENGINE_MCP_TOKEN") or "")
    if not admin:
        raise SystemExit("okengine-write: networked transport requires OKENGINE_WRITE_TOKEN "
                         "(or OKENGINE_MCP_TOKEN) — refusing to serve writes unauthenticated.")
    exposed = host not in _LOOPBACK
    if admin == DEFAULT_LOCAL_TOKEN and exposed and \
            env.get("OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN", "") != "1":
        raise SystemExit(
            f"okengine-write: refusing to bind {host} with the built-in DEFAULT token — it is "
            "public, and this is the ENFORCED WRITE path. Set OKENGINE_WRITE_TOKEN (or "
            "OKENGINE_MCP_TOKEN) to a secret, or OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN=1 to override.")
    return admin


if __name__ == "__main__":  # pragma: no cover
    if mcp is None:
        raise SystemExit("mcp package not installed; cannot run the server")
    transport = os.environ.get("OKENGINE_WRITE_TRANSPORT", "stdio")
    if transport in ("streamable-http", "http"):
        # Networked write surface for out-of-process sidecars. Requires a scoped or admin token;
        # refuses the built-in default off-loopback unless explicitly allowed (mirrors server.py).
        import uvicorn
        host = os.environ.get("OKENGINE_WRITE_HOST", "127.0.0.1")
        admin = _resolve_write_auth(os.environ, host)
        inner = _review_http_app() if os.environ.get("OKENGINE_WRITE_REVIEW_ONLY") == "1" \
            else mcp.streamable_http_app()
        app = _ScopedWriteAuth(inner, admin)
        uvicorn.run(app, host=host, port=int(os.environ.get("OKENGINE_WRITE_PORT", "8731")))
    else:
        mcp.run(transport="stdio")
