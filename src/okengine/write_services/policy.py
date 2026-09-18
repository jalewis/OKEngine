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
    _corpus_touch(log)
    with log.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _rel(p: Path) -> str:
    try:
        return p.relative_to(_wiki()).as_posix()
    except ValueError:
        return p.as_posix()


def _reserved_refuse(p: Path) -> Optional[str]:
    n = p.name.lower()
    # Mirror schema_validator._is_generated_structural EXACTLY: the validator exempts these basenames
    # from conformance, so the write path MUST refuse the same set or the two guards compose into zero
    # protection — an agent forges a conformance-invisible page (invariant-audit M17). That predicate
    # is: any `_`- or `.`-prefixed file, and the whole INDEX family (INDEX.md + INDEX-<anything>, incl.
    # the paginated INDEX-pNN.md). `index-` (lowercased) covers INDEX-glossary etc., not just index-p.
    if (
        n in _RESERVED_NAMES
        or n.startswith("index-")
        or p.name.startswith("_")
        or p.name.startswith(".")
    ):
        return (
            f"refused: {_rel(p)} is an engine-managed structural/reserved file "
            "— not agent-writable via the MCP write tools (use the file tool only "
            "if a human edit is truly intended)"
        )
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
        return (
            f"refused: {_rel(p)} is a pack-reserved file (schema `reserved_files`) "
            "— not agent-writable via the MCP write tools"
        )
    return None


def _tombstone_refuse(cur_fm: dict, p: Path) -> Optional[str]:
    """Never resurrect a tombstoned page. The converge lane already refuses a write to a tombstoned
    id (id-based); update/patch/append operate by PATH and read a retained tombstone file, so they
    must refuse it here too or an agent silently un-tombstones it (invariant-audit M18). Re-tombstoning
    or recording a successor is the tombstone_entity tool's job, not a plain content write."""
    if str(cur_fm.get("status") or "").strip().lower() == "tombstoned":
        return (
            f"refused: {_rel(p)} is tombstoned — write to its successor (superseded_by) or use "
            "tombstone_entity; never resurrect a tombstoned page"
        )
    return None


def _source_receipt_refuse(fm: dict, p: Path) -> Optional[str]:
    """A processing receipt is operational state, never canonical source intelligence."""
    if (
        str(fm.get("type") or "").strip().lower() == "source"
        and str(fm.get("status") or "").strip().lower() == "duplicate-receipt"
    ):
        return (
            f"rejected: {_rel(p)} cannot store status duplicate-receipt as a source page — "
            "append the duplicate raw provenance to the canonical source instead"
        )
    return None


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
    return id_lib.qualified_namespace(_wiki(), p)


def _entities_scope(rel: str) -> str:
    """The sub-domain container prefix of an entities-namespace page rel: '' for a root page
    (entities/s/x), 'acme' for a walk-up page (acme/entities/s/x). Entity dedup is scoped to a single
    sub-domain — a co-installed vault's sub-domains are separate knowledge bases and must not
    cross-merge two same-named entities (invariant-audit #351). The identity index only holds entities
    pages, so every candidate rel carries the 'entities' segment."""
    parts = rel.split("/")
    return "/".join(parts[: parts.index("entities")]) if "entities" in parts else ""


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
        raise ValueError(
            f"namespace '{ns}' permissions has unknown key(s) {unknown} "
            f"(valid: {list(_PERM_KEYS)}) — a typo would silently default the namespace open"
        )
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
    except Exception:  # pragma: no cover - schema load is best-effort
        return None
    if not declared or ns in allowed:
        return None
    hint = difflib.get_close_matches(ns, sorted(declared), n=1)
    suggest = f" — did you mean '{hint[0]}/'?" if hint else ""
    return (
        f"namespace '{ns}/' is not declared in schema.yaml (declared knowledge "
        f"namespaces: {sorted(declared)}){suggest} — a page written here forks into a "
        f"stray tree the dashboards/index never see (okengine#115)"
    )


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
    except Exception:  # pragma: no cover - schema load is best-effort
        return None
    if not home or home not in declared:  # no rule / home isn't a namespace here -> don't enforce
        return None
    if ns == home or ns in excluded:
        return None
    return (
        f"type '{typ}' belongs in '{home}/', but this page is being created under '{ns}/' — a "
        f"page in the wrong namespace forks the graph (type-scoped panels/indices never see it, "
        f"okengine#276). Write it under '{home}/'."
    )


def _type_ns_reject_on_change(p: Path, new_fm: dict, cur_fm: dict) -> Optional[str]:
    """`_type_namespace_reject` for the MUTATING lanes (update/patch/converge). The create-time guard
    was CREATE-ONLY, so update/patch/converge could rewrite a page's `type` to one whose home is a
    different namespace, forking the graph exactly as a bad create would (invariant-audit). Only
    reject when the type is actually CHANGING — a legacy page already in the 'wrong' namespace stays
    editable (grandfathered); a NEW drift of the type out of its home namespace is blocked."""
    if str((new_fm or {}).get("type") or "") == str((cur_fm or {}).get("type") or ""):
        return None
    return _type_namespace_reject(p, new_fm)


def _ordered_enum(policy: dict, field: str) -> list[str]:
    """The pack-DECLARED ordered vocabulary for a field, most-certain first, or [].

    Resolves the same indirection every other consumer uses: field_enums[field].enum names an entry
    in the schema's `enums:` map (or is an inline list). The engine carries no vocabulary of its own
    — a CTI pack declares [confirmed, high, moderate, low, suspected, unverified], a vendor-risk pack
    declares something else entirely, and both work here.
    """
    spec = ((policy or {}).get("field_enums") or {}).get(field)
    ref = spec.get("enum") if isinstance(spec, dict) else spec
    if isinstance(ref, str):
        ref = ((policy or {}).get("enums") or {}).get(ref)
    return [str(v) for v in ref] if isinstance(ref, list) and ref else []


def _enum_norm(policy: dict, field: str, value) -> str:
    """Lower-cased value with the pack's declared value_aliases applied (okcti maps medium ->
    moderate), so an alias spelling is not mistaken for a different level."""
    v = str(value or "").strip().lower()
    aliases = ((policy or {}).get("value_aliases") or {}).get(field) or {}
    return str(aliases.get(v, v)).strip().lower() if isinstance(aliases, dict) else v


def _cites_evidence(fm: dict) -> bool:
    for key in ("sources", "source"):
        v = (fm or {}).get(key)
        if isinstance(v, list) and any(str(x).strip() for x in v):
            return True
        if isinstance(v, str) and v.strip():
            return True
    return False


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

    for k in review.get("review_on_change_fields") or []:
        if fm.get(k) in (None, "") or fm.get(k) == (prev or {}).get(k):
            continue
        # A field with a DECLARED ORDERED ENUM is a scale, and a scale is self-documenting: a page
        # marked `suspected` is already telling the reader the claim is weakly held, and confidence
        # moving up and down as evidence arrives is the normal lifecycle, not an exception worth a
        # human's attention (okengine#546). Measured on okcti-test: 233 of 264 queue rows for this
        # field were hedges or mid-scale values, and 120 of them were DOWNGRADES — the flag fired
        # when an agent became MORE cautious, which is backwards for a guard that exists to catch a
        # claim being laundered upward.
        #
        # What is worth catching is the TOP of the scale asserted without evidence. So flag exactly
        # that, and nothing else. Fields with no declared ordering keep flag-on-any-change.
        # The ENUM lives in the full governing schema, not the policy subset governing_policy
        # returns (that carries `review`/`permissions` only). _governing resolves it sub-domain
        # aware, the same way every other shape guard does.
        schema = _governing(p)
        levels = _ordered_enum(schema, k)
        if levels:
            if _enum_norm(schema, k, fm.get(k)) != _enum_norm(schema, k, levels[0]):
                continue  # not the top of the scale — a hedge, never a flag
            if _cites_evidence(fm):
                continue  # top of the scale, and the page shows its evidence
            if str(fm.get("type") or "") == "source":
                continue  # a `source` page IS the primary document — it cites
                #                                nothing by construction and its authority is its own
                #                                `publisher`. Demanding a citation from it is the same
                #                                category error okengine#549 fixed in review_autoverify;
                #                                measured, it was all 23 of the remaining flags.
            flags.append(
                f"asserted `{k}: {fm.get(k)}` (top of the declared scale) with no citation"
            )
            continue
        flags.append(f"agent set/changed review field `{k}`")
    return flags


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
    # `drift_policy` intentionally exposes the nearest pack's raw policy. Universal value aliases
    # live in the engine base schema, so fold the COMPOSED aliases in before normalizing. Pack-only
    # field aliases/allowed lists retain their existing behavior; for a closed base field,
    # schema_lib has already made the engine's alias meaning authoritative.
    try:
        composed_aliases = (_governing(p).get("value_aliases") or {})
    except Exception:
        composed_aliases = {}
    if isinstance(composed_aliases, dict):
        pack_aliases = pol.get("value_aliases") if isinstance(pol.get("value_aliases"), dict) else {}
        pol = {**pol, "value_aliases": {**pack_aliases, **composed_aliases}}
    if not pol:
        return out, []
    for alias, canon in (pol.get("field_aliases") or {}).items():  # country -> suspected_origin
        if alias in out:
            v = out.pop(alias)
            if out.get(canon) in (None, "", [], {}):
                out[canon] = v
    for field, vmap in (pol.get("value_aliases") or {}).items():  # CN -> China ; active -> live
        if field in out and isinstance(vmap, dict):
            cur = out[field]
            # Exact match first (preserves case-sensitive legacy maps), then a conservative textual
            # spelling match. This lets `Probable` and `roughly even odds` converge without adding
            # every capitalization to the schema; it does not tokenize or infer unknown prose.
            folded = {str(k).strip().casefold(): v for k, v in vmap.items()}
            resolve = lambda value: vmap.get(  # noqa: E731 - small local rule used for scalar/list
                value, folded.get(str(value).strip().casefold(), value)
            )
            out[field] = (
                [resolve(x) for x in cur] if isinstance(cur, list) else resolve(cur)
            )
    flags: list[str] = []
    allowed = (pol.get("allowed") or {}).get(str(out.get("type") or ""))
    if isinstance(allowed, list):
        known = _okf_always() | set(allowed) | set((pol.get("field_aliases") or {}).values())
        unknown = sorted(k for k in out if k not in known)
        if unknown:
            flags.append(
                f"unknown field(s) for type `{out.get('type')}` "
                f"(not in schema): {', '.join(unknown)}"
            )
    return out, flags
