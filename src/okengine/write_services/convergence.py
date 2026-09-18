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
from okengine.actor_identity import actor_identity_error, has_positive_actor_identity
import output_contract_enforce as _output_contract

import id_lib, schema_lib, id_index, converge, okf_migrate
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")
_FRONTMATTER_PARSE_ERRORS = (yaml.YAMLError, TypeError, AttributeError)


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
        nsdir = p.parent.relative_to(
            _wiki()
        ).as_posix()  # 'acme/entities' | 'entities' | 'entities/a'
    except ValueError:
        nsdir = ""  # page outside wiki/ — vault-root schema
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
        # A URL-derived source id is AUTHORITY-grade, not a slug (okengine#515): its key is
        # sha256(url), so two pages carrying it are the same document by construction. Classing
        # it as a slug made `_converge` refuse to merge into the canonical — the very
        # duplicate-refusal this identity scheme exists to remove. The type declares no
        # `id_authority` because the authority is the URL itself, not an external registry.
        if _is_url_derived_source_id(pid):
            kind = "authority"
        return pid, kind
    return id_lib.derive_id(
        authority=authority,
        local_id=(fm.get(id_field) if authority else None),
        minted_scope=namespace,
        slug_source=id_lib.natural_key(fm, stem),
    )


def _converge(
    path: str,
    frontmatter_yaml: Union[str, dict],
    body: str = "",
    pack: str = "",
    remove: str = "",
    expected_sha256: str = "",
    _alias_verified: bool = False,
) -> str:
    """Upsert a page by id: merge into a live page that already carries the id
    (page+field ownership), else create + claim. (RFC composable-okpacks §5a.)"""
    # Ownership arbitration keys on the DEPLOYMENT's pack identity (OKENGINE_PACK, injected by
    # compose), never on the tool argument: `pack or _prov_pack()` let a caller name the owner and
    # clobber another pack's owned fields with "0 conflicts" (okengine#662). A differing argument is
    # refused loudly rather than ignored, so a mis-prompted lane surfaces instead of silently
    # degrading to non-owner writes. Without the env (a legacy single-pack deploy) the argument is
    # the only identity there is and keeps working.
    deployment = _prov_pack()
    if deployment and pack and pack != deployment:
        return (
            f"refused: pack identity is deployment-pinned (OKENGINE_PACK={deployment}); "
            f"a caller cannot act as {pack!r} — omit `pack`"
        )
    pack = deployment or pack  # deployment-pinned provenance + field ownership (okengine#90 P3)
    caller = _caller()
    extension_id = str(caller.get("ext_id") or "").strip()
    authenticated_extension_principal = (
        f"ext:{extension_id}"
        if caller.get("kind") == "extension" and extension_id
        else None
    )
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
    receipt_reject = _source_receipt_refuse(fm, p)
    if receipt_reject:
        return receipt_reject
    # Resolve a logical flat entity key to its canonical physical partition
    # before deciding whether this is a create or a merge. _create performs the
    # same routing, but doing it only after converge's identity lookup misses an
    # existing authority-id page and yields the unusable "use update_entity"
    # refusal to an actor that intentionally has only converge_entity.
    canonical_p = _partitioned_create_path(p, fm)
    if canonical_p != p:
        p = canonical_p
        _wa = _wauth_refusal(p)
        if _wa:
            return _wa
        _rr = _reserved_refuse(p)
        if _rr:
            return _rr
    namespace = _namespace(p)
    schema = _governing(p)  # sub-domain aware (okengine#177); namespace is the bare mint scope
    pid, kind = _page_id_and_kind(fm, schema, namespace, p.stem)
    if not pid:
        return "rejected: cannot determine page id"
    fm["id"] = pid
    reg = _registry()
    existing_rel = reg.resolve(pid)
    # A caller may already know the canonical path while supplying a weaker
    # identity than the page's authority-backed id (for example title "APT3"
    # beside the existing MITRE id G0022).  Converge is the actor's only upsert
    # primitive, so an exact live-path hit must take the governed merge path
    # instead of falling through to _create and endlessly returning "use
    # update_entity".  Immutable identity fields are preserved by
    # merge_frontmatter below; path authorization, tombstones, policy, schema,
    # ownership, and output-contract checks still apply.
    exact_path_fallback = not existing_rel and p.is_file()
    if exact_path_fallback:
        existing_rel = _rel(p)

    if existing_rel and reg.is_tombstoned(pid):
        return (
            f"refused: id {pid} is tombstoned — write to its successor, "
            "never resurrect a tombstoned id"
        )

    if existing_rel:
        existing_path = _wiki() / existing_rel
        same = existing_path.exists() and existing_path.resolve() == p.resolve()
        if not same:
            # Unlike create_entity, converge_entity is an explicit request to merge identity.
            # Resolve both authority and minted-slug hits to the canonical holder, then apply the
            # exact same redirect authorization, ownership, schema, and conflict gates below.
            # A field conflict may still queue the REAL canonical page; the identity collision
            # itself is deterministic routing, not human-review work.
            p = existing_path
            _wa = _wauth_refusal(p)  # re-authorize: the redirect can point OUTSIDE
            if _wa:  # the caller's declared scope (okengine#178)
                return _wa
            _rr = _reserved_refuse(p)  # re-check reserved: the id-index CAN resolve a
            if _rr:  # pack-reserved page (id_index._skip only knows the
                return _rr  # engine set, not schema reserved_files) — the
                # original _reserved_refuse was on the pre-redirect
                # path, so a converge could land on a reserved page
                # (invariant-audit HIGH — every other mutating lane
                # re-checks reserved on the exact path it writes)
        if p.is_file():
            utf8 = _utf8_refusal(p)
            if utf8:
                return utf8
            precondition = _write_precondition(p, expected_sha256)
            if precondition:
                return precondition
            cur_fm, cur_body = _read_page(p)
            if exact_path_fallback and cur_fm.get("id"):
                # The exact-path fallback is intentionally weaker than an
                # identity-index hit. Never let the title-derived candidate id
                # replace the canonical on-disk authority id.
                fm["id"] = cur_fm["id"]
            # invariant-audit M14: the registry tombstone check above reads the id-index, which is
            # up to 6h stale (rebuilt on a cron). A page hand-tombstoned on disk (status: tombstoned)
            # since the last index rebuild would slip past it and be RESURRECTED by this merge. Trust
            # the on-disk status too.
            if str(cur_fm.get("status") or "").strip().lower() == "tombstoned":
                return (
                    f"refused: {_rel(p)} is tombstoned on disk (status: tombstoned) — "
                    "write to its successor, never resurrect a tombstoned page"
                )
            ftype = str(cur_fm.get("type") or fm.get("type") or "").strip()
            owner = schema_lib.type_owner(schema, ftype)
            fos = schema_lib.field_owners(schema, ftype)
            rm = [s.strip() for s in (remove or "").split(",") if s.strip()]
            ownership_principal = authenticated_extension_principal or (pack or None)
            # In-gateway extension operations use a server-bound cron identity,
            # not a network token.  The generated actor is exactly
            # ``cron:<extension-id>`` (or ``cron:<extension-id>:<operation>``).
            # Treat it as the composed extension owner only when the page's
            # authoritative owner map agrees; a similarly named pack job cannot
            # claim some other extension's page.
            actor = str(caller.get("actor") or "")
            if caller.get("kind") == "job" and isinstance(owner, str) and owner.startswith("ext:"):
                extension_actor = f"cron:{owner[4:]}"
                if actor == extension_actor or actor.startswith(extension_actor + ":"):
                    ownership_principal = owner
            merged, dec = converge.merge_frontmatter(
                cur_fm,
                fm,
                owner_pack=owner,
                # Extension ownership comes from the authenticated scoped token,
                # never from the deployment pack or a client-supplied argument.
                caller_pack=ownership_principal,
                field_owners=fos,
                remove=rm,
            )
            # Capability checks for an existing page must describe the page and mutation that
            # will actually be committed.  Checking the caller's proposed type before reading the
            # target let an entity-only lane rewrite a malware page; omitting ``remove`` let it
            # delete protected fields.  Redirects and exact-path fallback share this late gate.
            # Normalize before authorization so aliases cannot disguise a protected canonical
            # field. Authorization covers both the existing target type and the resulting type:
            # a vendor-scoped lane may not turn that page into malware during convergence.
            merged, drift = _normalize_drift(merged, p)
            changed_fields = set(rm)
            changed_fields.update(
                key for key in set(cur_fm) | set(merged)
                if cur_fm.get(key) != merged.get(key)
            )
            cap = _capability_reject(
                p,
                "converge",
                page_type=str(cur_fm.get("type") or ""),
                changed_fields=changed_fields,
                body_change="replace" if body else "none",
            )
            if cap:
                return f"rejected: {cap}"
            resulting_type = str(merged.get("type") or "")
            if resulting_type != str(cur_fm.get("type") or ""):
                cap = _capability_reject(
                    p,
                    "converge",
                    page_type=resulting_type,
                    changed_fields=changed_fields,
                    body_change="replace" if body else "none",
                )
                if cap:
                    return f"rejected: {cap}"
            # extension_id is server-derived: converge.merge treats it as a _SERVER_KEY, but strip any
            # residual forge and re-assert the on-disk stamp so it can never be reassigned (M14).
            _apply_extension_provenance(
                merged,
                creating=False,
                existing_ext_id=cur_fm.get("extension_id"),
                existing_producer_lane=cur_fm.get("producer_lane"),
            )
            review_invalidation = _apply_review_governance(merged, cur_fm)
            new_body = (
                cur_body if not body else body
            )  # same guard as _create/_update (invariant-audit)
            _stamp(merged, cur_fm)
            if pack:
                merged["last_modified_by"] = pack
            blr = _briefing_link_reject(
                p, new_body
            )  # briefings must cite resolvable pages — the same
            if blr:  # guard create/update/patch/append enforce (L3)
                return f"rejected: {blr}"
            if body:
                bir = _body_integrity_reject(cur_body, new_body)
                if bir:
                    return f"rejected: {bir}"
            fd = _future_date_reject(merged)  # the boundary every writer crosses (invariant-audit)
            if fd:
                return f"rejected: {fd}"  # file left untouched
            _enum_case_coerce(p, merged)  # case-canonicalize enums (#226) before the guards
            isr = _int_shape_reject(
                p, merged
            )  # machine-owned int fields (recent_reports/total_mentions):
            if isr:  # create/update/patch all reject here; _dedup_on_create
                return (
                    f"rejected: {isr}"  # redirects create_entity INTO converge, so this lane must
                )
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
            tnr = _type_ns_reject_on_change(
                p, merged, cur_fm
            )  # type can't drift out of home ns (audit)
            if tnr:
                return f"rejected: {tnr}"
            fsr = _fabricated_source_reject(p, merged, prev_fm=cur_fm)
            if fsr:
                return fsr
            msr = _missing_source_reject(p, merged, prev_fm=cur_fm)
            if msr:
                return msr
            # ...then SOFT review flags (categorical confidence verdict, changed
            # review field) — flag, never block.
            review = (
                review_invalidation
                + drift
                + _review_flags(p, merged, prev=cur_fm)
                + _identity_contradiction_flags(p, merged)
                + _unresolvable_link_flags(p, new_body)
                + (_degeneration_flags(new_body) if body else [])
            )  # degenerate body attributable at write (M15)
            contract_reject = _contract_reject(p, "converge", merged, new_body, drift)
            if contract_reject:
                return f"rejected: {contract_reject}"
            if review:
                merged["needs_review"] = True
            content = _compose(merged, new_body)
            rej = schema_reject_reason(str(p), content)
            if rej:
                return f"rejected: {rej}"
            precondition = _write_precondition(p, expected_sha256)
            if precondition:
                return precondition
            _corpus_touch(p)
            _atomic_write_text(p, content)
            reg.by_id[pid] = _rel(p)
            ver = merged.get("version")
            _append_log(f"- {_today()} mcp-write converge {_rel(p)} (id {pid}) v{ver}")
            flags = list(review)
            if dec.conflicts:
                flags += [
                    f"field `{k}`: {ownership_principal or 'caller'} attempted {a!r}, "
                    f"owner value {c!r} kept"
                    for k, c, a in dec.conflicts
                ]
            note = _queue_review(p, flags) if flags else ""
            return (
                f"converged into {_rel(p)} (id {pid}) v{ver}: "
                f"+{len(dec.added)} added, ~{len(dec.updated)} updated, "
                f"-{len(dec.removed)} removed, {len(dec.conflicts)} conflict(s){note}"
            )

    # New id: authorize the proposed page before create + registry claim. Existing pages take the
    # target-aware gate above instead, after canonical resolution and the governed merge.
    cap = _capability_reject(
        p,
        "converge",
        page_type=str(fm.get("type") or ""),
        changed_fields=fm.keys(),
        body_change="replace" if body else "none",
    )
    if cap:
        return f"rejected: {cap}"
    if pack:
        fm.setdefault("maintained_by", [pack])
        fm.setdefault("discovered_by", pack)
    result = _create(path, fm, body, _contract_operation="converge")
    if result.startswith("created"):
        cp = _safe(path)
        if cp is not None:
            reg.by_id[pid] = _rel(cp)
    return result


def _is_class_description(generic: str) -> bool:
    """True when a title DESCRIBES an adversary instead of NAMING one.

    Two shapes, both measured against all 1,018 actor titles on a live vault before being
    written down -- 9 matches, every one of them junk, no legitimate name touched:

    * The last word is the category itself. A name does not end in the word for its own
      kind: `Iranian-Aligned Threat Actor`, `Unnamed Actor`, `Some Generic Actor`. Bounded
      at four words so a longer real name that happens to end in "Actor" is left alone.
    * A vagueness word appears: `Placeholder`, `[Unnamed group]`. These are template text
      the model failed to replace, not adversaries.

    This generalises where the exact-match list cannot -- but it does not close the class.
    A generative writer will always reach a phrasing neither shape covers; the structural
    answer is okengine#592.
    """
    words = generic.split()
    if not words:
        return False
    return (len(words) <= 4 and words[-1] in {"actor", "actors"}) or bool(
        _VAGUE_WORDS.intersection(words)
    )


_GENERIC_ACTOR_LABELS = frozenset(
    """
actor|actors|adversary|ai agent|ai agents|ai attacker|ai attackers|attack group|attacker|attackers
autonomous llm agent|criminal group|cyber criminals|cybercriminal|cybercriminal group|cybercriminals
hacker|hacker group|hackers|initial access broker|initial access brokers|intruder|intruders|llm agent
llm agents|malware campaign|outsider|placeholder|ransomware campaign|ransomware gang|ransomware gangs
ransomware group|threat actor|threat actor name|threat actors|threat group|unknown|unknown actor
unknown threat actor|unsafe
""".strip().replace("\n", "|").split("|")
)

_NON_ACTOR_DEFINITION = re.compile(
    r"\b(?:is|was|dubbed|described as|documented)\b.{0,100}\b"
    r"(?:backdoor|malware|implant|loader|ransomware|stealer|trojan|"
    r"phishing[- ]as[- ]a[- ]service|phishing (?:service|toolkit)|toolkit)\b",
    re.I | re.S,
)


def _actor_reserved_titles(page_path: Any = None) -> frozenset[str]:
    """Return schema-declared exact titles that cannot identify an actor.

    Packs own domain vocabulary, so the engine deliberately knows no country or
    geopolitical names. A mapping allows a pack to reuse its code-to-label
    assessment vocabulary through a YAML anchor instead of copying names.
    """
    if page_path is None:
        return frozenset()
    try:
        if not isinstance(page_path, Path):
            page_path = _safe(str(page_path))
        configured = (
            (_governing(page_path).get("identity_admission") or {})
            .get("actor", {})
            .get("excluded_exact_titles", ())
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return frozenset()
    values = configured.values() if isinstance(configured, Mapping) else configured
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        return frozenset()
    return frozenset(
        re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        for value in values
        if str(value).strip()
    )


def _actor_admission_reject(
    fm: Mapping[str, Any], body: str = "", page_path: Any = None
) -> str | None:
    """Reject an obvious class label or software subject presented as an actor.

    This is deliberately high precision. It is a hard admission boundary, not a general entity
    classifier: uncertain identities pass to normal review, while deterministic errors never land.
    """
    if str(fm.get("type") or "").strip() != "actor":
        return None
    title = str(fm.get("title") or fm.get("name") or "").strip()
    generic = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    if generic in _actor_reserved_titles(page_path):
        return (
            "rejected: configured geopolitical entities are attribution values, not named "
            "threat actors; create an actor-country-linkage assessment for a discrete actor"
        )
    if generic in _GENERIC_ACTOR_LABELS or _is_class_description(generic):
        return (
            "rejected: generic class labels are not named threat actors; "
            "use skipped or select a supported named adversary"
        )
    plain = re.sub(r"[`*_#]+", " ", body[:4000])
    plain = re.sub(r"\s+", " ", plain).strip()
    contradictory_kind = actor_identity_error(title, body)
    if contradictory_kind:
        return (
            "rejected: page evidence defines the subject as a "
            f"{contradictory_kind}, not a threat actor; classify it using a supported "
            "non-actor type or skip/defer it"
        )
    named = re.escape(title)
    software_kind = (
        r"(?:backdoor|malware|implant|loader|ransomware|stealer|trojan|"
        r"phishing[- ]as[- ]a[- ]service|phishing (?:service|toolkit)|toolkit)"
    )
    direct_definition = re.search(
        rf"\b{named}\b\s+(?:is|was)\s+(?P<prefix>[^.;:]{{0,80}}?)\b{software_kind}\b",
        plain,
        re.I,
    )
    defines_software = False
    if direct_definition:
        prefix_words = re.findall(r"[a-z]+", direct_definition.group("prefix").lower())
        suffix_words = re.findall(
            r"[a-z]+", plain[direct_definition.end():direct_definition.end() + 70].lower()
        )[:6]
        defines_software = set(prefix_words) <= {
            "a", "an", "the", "commercial", "custom", "malicious", "modular", "new",
            "newly", "previously", "python", "remote", "sophisticated", "unreported", "windows",
        } and not {
            "actor", "actors", "affiliate", "affiliates", "cartel", "cluster", "gang", "group",
            "operation", "operator", "operators", "team", "threat",
        }.intersection(suffix_words)
    defines_software = defines_software or bool(
        re.search(
            rf"\b{software_kind}\b\s*,?\s*(?:dubbed|named|called)\s+{named}\b",
            plain,
            re.I,
        )
    )
    title_words = set(generic.split())
    actor_categories = {"actor", "cartel", "crew", "gang", "group", "operation", "operator", "team"}
    if defines_software or (
        re.search(r"\b(?:backdoor|malware|ransomware|stealer|trojan|rat|toolkit)\b", title, re.I)
        and not actor_categories.intersection(title_words)
        and re.search(r"\b(?:this|the) ransomware\b", body, re.I)
    ):
        return (
            "rejected: page evidence defines the subject as software/tooling, not a threat actor; "
            "classify it as malware or tool"
        )
    return None


def _actor_payload_reject(
    frontmatter_yaml: str, body: str = "", page_path: Any = None
) -> str | None:
    """Apply actor admission when a generic MCP create receives serialized frontmatter."""
    try:
        candidate = yaml.safe_load(frontmatter_yaml) or {}
    except yaml.YAMLError:
        return None  # the normal schema parser returns the detailed YAML rejection
    return (
        _actor_admission_reject(candidate, body, page_path)
        if isinstance(candidate, dict)
        else None
    )


def _entity_backfill_frontmatter(
    path: str, frontmatter_yaml: str, body: str = ""
) -> tuple[str | None, str | None]:
    """Repair deterministic source-evidence leakage in an entity payload."""
    try:
        actor_fm = yaml.safe_load(frontmatter_yaml) or {}
    except yaml.YAMLError as exc:
        return None, f"rejected: invalid frontmatter YAML: {exc}"
    if not isinstance(actor_fm, dict):
        return None, "rejected: frontmatter_yaml must decode to a mapping"
    # Embedded evidence and nearby wikilinks can leak another page family's
    # storage identity into the proposed entity. Those are never entity
    # authority IDs; discard them and mint identity from the entity itself.
    explicit_id = str(actor_fm.get("id") or "")
    if explicit_id.split(":", 1)[0] in {
        "sources",
        "concepts",
        "predictions",
        "briefings",
        "reports",
        "findings",
        "reviews",
        "operational",
    }:
        actor_fm.pop("id", None)
    target = _safe(path)
    schema = _governing(target) if target is not None else {}
    allowed = schema_lib.canonical_types(schema)
    ptype = str(actor_fm.get("type") or "").strip()
    wrong_namespace = bool(
        target is not None and ptype and _type_namespace_reject(target, actor_fm)
    )
    if not ptype or ptype not in allowed or wrong_namespace:
        replacement = next(
            (
                candidate
                for candidate in ("publisher", "lab", "vendor", "identity")
                if candidate in allowed
            ),
            None,
        )
        if replacement:
            actor_fm["type"] = replacement
            ptype = replacement
    if ptype == "actor":
        actor_type = str(actor_fm.get("actor_type") or "").strip()
        allowed_actor_types = {
            "nation-state",
            "cybercriminal",
            "hacktivist",
            "insider",
            "commercial-surveillance",
            "unknown",
        }
        title = str(actor_fm.get("title") or actor_fm.get("name") or "").strip()
        generic = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        # The list is the guard. It previously held only the two examples the entity-backfill
        # PROMPT happens to name ("threat actor", "AI agents") plus a couple of neighbours, so the
        # rest of the family walked straight through a live check: `Attacker`, `Malware Campaign`
        # and `Ransomware Gang` were all created on a real vault AFTER this guard shipped.
        #
        # Their cost is not a bad row on a board. A generic name matches ordinary prose, so the
        # news lane counts it against the whole firehose, and `actor-assessment-priority` records
        # inherit that count -- `Threat Actor` scored 58 matches on 1 cited source and was
        # scheduled for analysis ahead of every real adversary (okengine#591).
        #
        # Singular AND plural: the write path normalises punctuation, not grammar, so "Attackers"
        # is a different string from "Attacker" and would otherwise be the next page created.
        #
        # NOTE: okpack-threat-actors' `actor_news_activity._GENERIC_PHRASE` keeps a near-twin of
        # this list for a different question (what may MATCH, not what may be CREATED). Two lists
        # in two repos will drift; unifying them needs a shared declaration neither side has today.
        #
        # The list is a FLOOR, not a ceiling, and it is worth being honest about that: while this
        # very guard was in review the lane produced `Initial Access Brokers` on a live vault, and
        # a scan then surfaced `AI Attackers`, `Unnamed Actor`, `Placeholder` and `Threat actor
        # name`. Enumerating loses to a generative writer. `_is_class_description` covers the two
        # shapes that generalise; the durable answer is structural (okengine#592).
        admission_error = _actor_admission_reject(actor_fm, body, path)
        if admission_error:
            return None, admission_error
        if actor_type not in allowed_actor_types:
            return None, (
                "rejected: entity-backfill type actor requires explicit actor_type "
                "(nation-state, cybercriminal, hacktivist, insider, "
                "commercial-surveillance, or unknown); use publisher/identity for "
                "companies and skipped for generic subjects"
            )
        if not has_positive_actor_identity(title, body):
            return None, (
                "rejected: entity-backfill type actor requires source-grounded evidence that "
                "the named subject is an adversarial group, intrusion set, criminal "
                "organization, operation, or operator; source grade, news count, and actor_type "
                "do not establish entity class — use a supported non-actor type or skip/defer"
            )
        actor_fm["actor_identity_validated"] = True
    if (
        ptype in {"publisher", "lab", "vendor", "identity"}
        and not str(actor_fm.get("name") or "").strip()
    ):
        derived = str(
            actor_fm.get("title")
            or actor_fm.get("publisher")
            or Path(path).stem.replace("-", " ").title()
        ).strip()
        if derived:
            actor_fm["name"] = derived
    return yaml.safe_dump(actor_fm, sort_keys=False, allow_unicode=True), None


def _raw_backfill_frontmatter(
    frontmatter_yaml: Any,
    parse_errors: tuple[type[BaseException], ...] = _FRONTMATTER_PARSE_ERRORS,
) -> tuple[dict | None, str | None]:
    """Normalize deterministic raw-path leakage before source validation."""
    if isinstance(frontmatter_yaml, dict):
        fm = dict(frontmatter_yaml)
    else:
        try:
            fm = yaml.safe_load(frontmatter_yaml) or {}
        except parse_errors as exc:
            return None, f"rejected: invalid frontmatter YAML: {exc}"
    if not isinstance(fm, dict):
        return None, "rejected: frontmatter_yaml must decode to a mapping"
    fm["type"] = "source"
    # Local models commonly infer a kind from the raw directory name
    # (`qualification`, `clippings`, etc.). Those storage labels are not source
    # taxonomy values. A captured document with no more specific valid kind is
    # conservatively a report.
    if str(fm.get("source_kind") or "").strip() not in _SOURCE_KINDS:
        fm["source_kind"] = "report"
    return fm, None


def _selected_raw_ref(selected: Any) -> str:
    """The single selected, existing raw artifact as a vault-relative path."""
    if not isinstance(selected, list) or len(selected) != 1:
        return ""
    (item,) = selected
    if not isinstance(item, str):
        return ""
    rel = item.strip().lstrip("/")
    if not rel.startswith("raw/"):
        return ""
    vault = Path(os.environ.get("WIKI_PATH", "/opt/vault"))
    candidate = vault / rel
    try:
        candidate.resolve().relative_to(vault.resolve())
    except (OSError, ValueError):
        return ""
    return rel if candidate.is_file() else ""


def _raw_manifest_selection() -> tuple[list[Any], str]:
    """Load a selection manifest and validate its single raw capture."""
    manifest_path = Path(
        os.environ.get(
            "OKENGINE_SELECTION_MANIFEST",
            "/opt/data/cron-plus/selections/raw-backfill.json",
        )
    )
    try:
        manifest = json.loads(manifest_path.read_text())
        selected = manifest.get("selected") or [] if isinstance(manifest, dict) else []
    except (OSError, ValueError, TypeError):
        selected = []
    return selected, _selected_raw_ref(selected)


def _selected_raw_url(selected: list[Any]) -> str:
    """Recover the authoritative URL from the single selected raw artifact.

    The raw selector already bounded and embedded this document.  Local models
    sometimes omit ``url`` from otherwise valid structured tool arguments; do
    not turn that serialization lapse into a permanently rejected corpus item
    when the selected evidence contains an unambiguous URL.
    """
    rel = _selected_raw_ref(selected)
    if not rel:
        return ""
    try:
        text = (Path(os.environ.get("WIKI_PATH", "/opt/vault")) / rel).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""
    match = re.search(r"(?im)^\s*(?:url|source_url)\s*:\s*(https?://\S+)\s*$", text)
    return match.group(1).strip() if match else ""
