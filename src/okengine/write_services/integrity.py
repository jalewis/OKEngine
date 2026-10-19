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
from okengine.list_scalars import split_list_scalar
import output_contract_enforce as _output_contract

import id_lib, schema_lib, id_index, converge, okf_migrate
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _atomic_write_text(path: Path, content: str) -> None:
    """Durably publish UTF-8 text without exposing a partial destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    tmp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            tmp = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _utf8_refusal(path: Path) -> str:
    """Return an explicit refusal and queue corrupt bytes for human repair."""
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        reason = (
            f"invalid UTF-8 at byte {exc.start}; write refused to preserve the original bytes"
        )
        _append_review_queue_once(path, reason)
        _append_log(f"- {_today()} mcp-write integrity-refusal {_rel(path)} — {reason}")
        return f"refused: {_rel(path)} contains {reason}"
    return ""


def _future_date_reject(fm: dict, fields=_RECORD_DATE_FIELDS) -> Optional[str]:
    try:
        today = datetime.date.fromisoformat(_today())
    except ValueError:  # unparseable test override — never block writes on guard plumbing
        return None
    limit = today + datetime.timedelta(days=1)
    for k in fields:
        v = fm.get(k)
        d = None
        if isinstance(v, datetime.datetime):  # before date: datetime IS a date subclass
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
            return (
                f"{k}: {d.isoformat()} is in the future (today is {today.isoformat()}) — "
                f"record-keeping dates must be the ACTUAL write date, never a guessed or "
                f"future one; use today's date"
            )
    return None


def _wikilink_resolves(t: str) -> bool:
    wiki = _wiki()
    if (wiki / f"{t}.md").is_file():
        return True  # literal path (incl. an already-sharded link)
    parts = t.split("/")
    if len(parts) >= 2 and parts[-1][:1].isalnum():
        ns, base = parts[0], parts[-1]
        b = base[0].lower()
        if (wiki / ns / b / f"{base}.md").is_file():
            return True  # first-letter shard (entities/qilin -> entities/q/qilin)
        if len(base) > 1 and (wiki / ns / b / base[1].lower() / f"{base}.md").is_file():
            return True  # second-letter reshard (oversized shard)
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
    return [
        f"{len(bad)} unresolvable wikilink(s): "
        + "; ".join(bad[:5])
        + (" …" if len(bad) > 5 else "")
    ]


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
        if _SINGULAR_SOURCE_REF.match(t):  # singular `source/` — invalid namespace = fabrication
            bad.append(s)
    if not bad:
        return None
    return (
        "rejected: sources use the invalid singular `source/` namespace (the schema's source "
        "namespace is plural `sources/`): "
        + "; ".join(bad[:5])
        + (" …" if len(bad) > 5 else "")
        + " — this is the entity-backfill fabrication signature; cite an EXISTING `sources/<…>` "
        "page or a provenance label (e.g. 'MITRE ATT&CK')."
    )


def _alias_shape_reject(p: Path, fm: dict, prev_fm: Optional[dict] = None) -> Optional[str]:
    """Reject a NEW `aliases` entry that is not a single name (okengine#845): a URL, prose, or a
    comma-joined list. Aliases already on the page are grandfathered, exactly as legacy source refs
    are, so a page carrying old junk stays editable while no writer can add more."""
    raw = fm.get("aliases")
    values = raw if isinstance(raw, list) else ([raw] if raw not in (None, "") else [])
    prev_raw = (prev_fm or {}).get("aliases")
    grandfathered = {str(v).strip() for v in (prev_raw if isinstance(prev_raw, list)
                                                else [prev_raw] if prev_raw else [])}
    bad = [(v, why) for v in values
           if str(v).strip() not in grandfathered and (why := schema_lib.alias_value_problem(v))]
    # A name a review removed from THIS page stays out until someone deliberately re-admits it by
    # deleting it from `rejected_aliases` -- in the same call is fine. Read from the RESULTING page
    # so that re-admission works in one update.
    banned = schema_lib.rejected_alias_keys(fm)
    prev_keys = {schema_lib.alias_key(v) for v in grandfathered}
    rejected = [v for v in values
                if (k := schema_lib.alias_key(v)) in banned and k not in prev_keys]
    if rejected:
        return (
            "rejected: "
            + ", ".join(repr(str(v)[:60]) for v in rejected[:5])
            + (" …" if len(rejected) > 5 else "")
            + " was removed from this page's aliases by review and is listed in `rejected_aliases`"
            " — to re-admit a name, remove it from `rejected_aliases` in the same update."
        )
    if not bad:
        return None
    return (
        "rejected: an alias must be a single name: "
        + "; ".join(f"{str(v)[:60]!r} is {why}" for v, why in bad[:5])
        + (" …" if len(bad) > 5 else "")
        + " — put references in `sources`, descriptions in the body, and each alias in its own "
        "list entry."
    )
def _record_alias_removals(fm: dict, prev_fm: dict) -> list:
    """Append to `rejected_aliases` every well-formed alias this edit dropped, so an importer's
    additive merge cannot put it back. Returns what was recorded.
    A name already listed is not repeated; one the edit re-spelled (same key) is not a removal."""
    dropped = schema_lib.removed_aliases((prev_fm or {}).get("aliases"), fm.get("aliases"))
    if not dropped:
        return []
    field = schema_lib.REJECTED_ALIASES_FIELD
    current = fm.get(field)
    listed = list(current) if isinstance(current, list) else ([current] if current else [])
    known = {schema_lib.alias_key(v) for v in listed}
    added = [v for v in dropped if schema_lib.alias_key(v) not in known]
    if added:
        fm[field] = listed + added
    return added


def _tombstone_successor_reject(p: Path, cur_fm: dict, reason: str,
                                superseded_by: Optional[str]) -> Optional[str]:
    """Refuse an entity tombstone whose reason or successor cannot be right (okengine#101).

    Entities only: source-dedup lanes legitimately retire sources onto winners, and nothing here
    has been measured against them."""
    if _namespace(p) != "entities":
        return None
    kind = schema_lib.tombstone_reason_kind(reason)
    if kind == "distinct":
        return ("rejected: this reason says the subjects are DIFFERENT, and a tombstone retires a "
                "page that is the SAME identity as another — remove the conflating alias with "
                "update_entity and leave both pages live")
    target = str(superseded_by or "").strip().strip("[]").strip()
    if not target:
        if kind == "duplicate":
            return ("rejected: a duplicate is retired ONTO its canonical page — pass superseded_by "
                    "with the live page it duplicates")
        return None
    rel = target.removeprefix("wiki/").removesuffix(".md").strip("/")
    succ = _safe(rel)
    if not (succ and succ.is_file()):
        namespace, _, rest = rel.partition("/")
        succ = okf_migrate.find_page(_wiki().parent, namespace, rest.rsplit("/", 1)[-1]) \
            if rest else None
    if succ is None or not succ.is_file():
        return f"rejected: superseded_by {target!r} does not resolve to an existing page"
    if succ.resolve() == p.resolve():
        return "rejected: a page cannot supersede itself"
    succ_fm, _ = _read_page(succ)
    if str(succ_fm.get("status") or "").strip().lower() == "tombstoned":
        return (f"rejected: superseded_by {_rel(succ)} is itself tombstoned — point at the live "
                "canonical page")
    schema = _governing(p)
    mine = schema_lib.canonical_type(schema, str(cur_fm.get("type") or ""))
    theirs = schema_lib.canonical_type(schema, str(succ_fm.get("type") or ""))
    if mine and theirs and mine != theirs:
        return (f"rejected: superseded_by {_rel(succ)} is type {theirs!r}, not {mine!r} — a page is "
                "only ever superseded by the same kind of thing")
    return None


def _missing_source_reject(p: Path, fm: dict, prev_fm: Optional[dict] = None) -> Optional[str]:
    """Reject NEW entity citations to nonexistent canonical source pages.

    Source pages may forward-reference entities, but the reverse is evidence provenance: an entity
    cannot cite a source that has not been compiled. Existing broken references are grandfathered
    so repair updates remain possible; only newly introduced values are blocked.
    """
    if _namespace(p) != "entities":
        return None
    grandfathered = {str(s).strip() for s in _entity_sources(prev_fm)} if prev_fm else set()
    bad = []
    for source in _entity_sources(fm):
        if not isinstance(source, str) or source.strip() in grandfathered:
            continue
        target = source.strip().strip("[]").strip("/").removeprefix("wiki/").removesuffix(".md")
        if target.startswith("sources/") and not (_wiki() / f"{target}.md").is_file():
            bad.append(target)
    if not bad:
        return None
    return (
        "rejected: entity `sources:` must cite existing canonical source pages; unresolved: "
        + "; ".join(bad[:5])
        + (" …" if len(bad) > 5 else "")
        + " — read and use the exact canonical source path; never invent or forward-reference evidence."
    )


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
    declared = {
        (prefix.casefold(), number)
        for value in values
        if value is not None
        for prefix, number in _VALUE_DESIGNATION.findall(str(value))
    }
    conflicts = sorted(
        {
            (prefix, slug_num, declared_num)
            for prefix, slug_num in slug_ids
            for dprefix, declared_num in declared
            if prefix == dprefix and slug_num != declared_num
        }
    )
    if not conflicts:
        return []
    detail = ", ".join(
        f"{prefix.upper()}-{slug_num} vs {prefix.upper()}-{declared_num}"
        for prefix, slug_num, declared_num in conflicts[:4]
    )
    return [f"entity identity contradiction between path and declared name/aliases: {detail}"]


def _degeneration_flags(body: Optional[str]) -> list:
    """SOFT review flag (never a reject) for a DEGENERATE generation — a repetition-loop word-salad
    (comma/wikilink-aware). See the block comment above."""
    if not body:
        return []
    prose = _DEGEN_WIKILINK.sub(
        " ", _DEGEN_FENCE.sub("\n", body)
    )  # code + wikilink-lists are not prose
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
    n_source = n_knowledge = 0  # classify resolved links for the cite check
    for t in dict.fromkeys(targets):  # de-dup, keep order
        target_rel = t if t in rels else (by_base.get(t) if "/" not in t else None)
        if target_rel:
            ns = target_rel.split("/")[0]
            if ns == "sources":
                n_source += 1
            elif ns not in (
                "dashboards",
                "operational",
            ):  # a substantive claim, not a nav/meta link
                n_knowledge += 1
            continue
        base = t.split("/")[-1]
        if base in by_base:  # right page, wrong dir/shard
            broken.append(f"[[{t}]] — did you mean [[{by_base[base]}]]?")
            continue
        near = difflib.get_close_matches(base, list(by_base), n=2, cutoff=0.6)
        hint = (
            " — did you mean " + " or ".join(f"[[{by_base[n]}]]" for n in near) + "?"
            if near
            else ""
        )
        broken.append(f"[[{t}]] (no such page){hint}")
    if broken:
        return (
            "briefing links must resolve to existing pages (cite what you actually read; "
            "do not guess slugs): " + "; ".join(broken)
        )
    # A briefing that makes ENTITY claims must be verifiable: at least one resolvable
    # [[sources/...]] link. Footnotes and code-span paths are not links an analyst can click,
    # and the brief lanes keep omitting real citations despite the prompt (unenforced half).
    # A pure "nothing happened this week" briefing (no knowledge links) is exempt.
    if n_knowledge and not n_source:
        return (
            f"briefing cites {n_knowledge} entit{'y' if n_knowledge == 1 else 'ies'} but no source — "
            "every development must end with a resolvable [[sources/<path>]] link (a footnote or "
            "a code-span path is not a citation an analyst can verify)"
        )
    return None


def _converge_alias_rule(merged: dict, cur_fm: dict, dec) -> None:
    """Aliases are additive under converge (okengine#864). Removing a name is a review decision,
    recorded in `rejected_aliases` so an importer's additive merge cannot restore it -- exactly as
    update/patch do. Converge used to REPLACE the owned list, so a shorter incoming list (an upsert
    that did not know every name) or `remove=aliases` dropped names silently and the next import
    put them back. Decided on the merge's own verdict, so field ownership stands: a non-owner's
    change or removal was already a conflict that kept the current value. Malformed or
    review-rejected additions are still refused by _alias_shape_reject, not filtered here."""
    if "aliases" in dec.removed:
        _record_alias_removals(merged, cur_fm)
    elif "aliases" in dec.updated:
        merged["aliases"] = _converge_aliases(cur_fm.get("aliases"), merged.get("aliases"))


def _converge_aliases(existing: object, incoming: object) -> list:
    """Existing aliases kept exactly, incoming ones appended when no spelling of them is present
    (okengine#864). Shape and rejected-alias checks are _alias_shape_reject's job, so nothing is
    filtered here: a malformed addition must be refused, not quietly dropped."""
    def _as_list(raw: object) -> list:
        # A legacy scalar is read as the identity index reads it, split as the write path coerces
        # it (okengine#1028, #1042 [19]): kept whole, `A, B` became one list entry the index
        # never routes.
        if isinstance(raw, str):
            return split_list_scalar(raw)
        return list(raw) if isinstance(raw, list) else ([] if raw is None else [raw])
    kept = _as_list(existing)
    seen = {schema_lib.alias_key(v) for v in kept}
    for value in _as_list(incoming):
        key = schema_lib.alias_key(value)
        if key and key not in seen:  # a value with no identity is not a name (admissible_aliases)
            seen.add(key)
            kept.append(value)
    return kept


def _identity_field_conflicts(schema: dict, page_type: str, conflicts: list) -> list[str]:
    """The identity-defining fields among a merge's conflicts, in conflict order (okengine#901).

    A conflict on one of them means the incoming write disagrees with the page about WHICH
    document it is: strong evidence of two different records under one id, not an update. The
    caller then keeps the page's body; frontmatter conflicts already keep the owner's values.
    The engine-generic set is `state._ENGINE_IDENTITY_FIELDS`; a pack adds its own per type.
    """
    tdef = (schema.get("types") or {}).get(page_type)
    declared = tdef.get("identity_fields") if isinstance(tdef, dict) else None
    fields = set(_ENGINE_IDENTITY_FIELDS)
    if isinstance(declared, list):
        fields.update(str(f) for f in declared if isinstance(f, str) and f.strip())
    return [key for key, _current, _attempted in conflicts if key in fields]


def _identity_body_kept(fields: list[str], flags: list[str]) -> str:
    """Record that a converge withheld its body over an identity conflict (okengine#901).

    Appends the review-queue reason to `flags` and returns the note for the tool result.
    """
    withheld = ", ".join(f"`{k}`" for k in fields)
    flags.append(
        f"identity field conflict ({withheld}): the write may describe a different "
        "document — incoming body NOT applied, existing body kept"
    )
    return f"; body kept (identity conflict on {withheld})"


def _actor_source_page(ref: str):
    """The canonical source page a `sources:` entry names, or None (okengine#1012).

    A path resolves directly. An id-shaped entry (``sources:url-…``) resolves through the id index,
    the lookup converge already trusts for identity. Anything outside ``sources/`` is not evidence.
    """
    try:
        page = _safe(ref)
        if page is None or not page.is_file():
            rel = _registry().resolve(ref) if ":" in ref else None
            page = _safe(rel) if rel else None
        if page is not None and _namespace(page) == "sources" and page.is_file():
            return page
    except (OSError, ValueError):  # an unreadable index or path is not evidence: fail closed
        return None
    return None


def _actor_source_documents(fm: Mapping[str, Any]) -> tuple[list, list[str], list[str]]:
    """Resolve an actor's cited sources: (documents, provenance labels, unresolved refs)."""
    from okengine import actor_grounding

    def read(ref: str):
        page = _actor_source_page(ref)
        if page is None:
            return None
        try:
            source_fm, body = _read_page(page)
        except (OSError, ValueError):  # unreadable or not UTF-8: unresolved, so fail closed
            return None
        return actor_grounding.source_document(_rel(page), source_fm, body)

    return actor_grounding.resolve_sources(fm, read)


def _actor_source_identity_error(fm: Mapping[str, Any]) -> str | None:
    """Inspect existing canonical source pages, leaving missing-source policy unchanged."""
    from okengine.actor_identity import actor_source_identity_error

    title = str(fm.get("title") or fm.get("name") or "")
    documents, _labels, _unresolved = _actor_source_documents(fm)
    return actor_source_identity_error(title, [document.body for document in documents])


def _actor_schema(page_path: Any = None) -> Mapping[str, Any]:
    """The schema governing ``page_path``, or ``{}`` when there is no resolvable page."""
    if page_path is None:
        return {}
    try:
        if not isinstance(page_path, Path):
            page_path = _safe(str(page_path))
        schema = _governing(page_path)
    except Exception:  # an unresolvable schema leaves the engine's own `actor` type guarded
        return {}
    return schema if isinstance(schema, Mapping) else {}


def _actor_reserved_titles(page_path: Any = None) -> frozenset[str]:
    """Return schema-declared exact titles that cannot identify an actor.

    Packs own domain vocabulary, so the engine deliberately knows no country or
    geopolitical names. A mapping allows a pack to reuse its code-to-label
    assessment vocabulary through a YAML anchor instead of copying names.
    """
    try:
        configured = (
            (_actor_schema(page_path).get("identity_admission") or {})
            .get("actor", {})
            .get("excluded_exact_titles", ())
        )
    except AttributeError:
        return frozenset()
    values = configured.values() if isinstance(configured, Mapping) else configured
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        return frozenset()
    return frozenset(
        re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        for value in values
        if str(value).strip()
    )


def _actor_namesakes(p: Path, names: list[str], schema: Mapping[str, Any]) -> list[str]:
    """The page's names the deployment holds as something other than an actor (okengine#1014):
    the pack's geopolitical titles, and a name a live non-actor entity page already carries (a
    `malware` page called RedLine). Grounding reads no agent verb as evidence for those."""
    from okengine import actor_grounding, actor_types

    held = list(actor_grounding.reserved_titles(schema))
    own = _rel(p)
    for name in names:
        for rel in _registry().name_to_rels.get(id_lib.normalize_key(name), []):
            try:
                other, _ = _read_page(_wiki() / rel) if rel != own else ({}, "")
            except (OSError, ValueError):
                continue
            kind = schema_lib.canonical_type(schema, other.get("type"))
            if kind and kind != "source" and not actor_types.is_actor_type(schema, kind):
                held.append(name)
    return held


def _actor_grounding_reject(p: Path, fm: Mapping[str, Any],
                            prev_fm: Optional[Mapping[str, Any]] = None) -> str | None:
    """Refuse a model write that creates an actor, or retypes a page to one, without evidence.

    okengine#1012: the entity-backfill lane minted a sandbox vendor, a hosting provider, a
    package registry and a nation-nexus adjective as actors. Each passed because the identity
    checks read the profile the model had just written. Here the evidence is the CITED SOURCES:
    one must name the subject and, in its own words, affirm it as an adversary (a curated record
    may vouch by naming it). A provenance label does not count; a model can type any label.

    Only a create, a retype or a rename is judged (okengine#1014: retitling a stored actor
    "ANY.RUN" repurposes it). Any other update to an actor keeps its path: re-litigating every
    stored page on every edit would freeze legitimate enrichment.
    """
    from okengine import actor_grounding, actor_types

    # Every actor type the composed schema declares, not the literal `actor` (okengine#1020), and
    # case-insensitive: a non-strict pack stores `Actor` as written (QA of !1288).
    schema = _governing(p)
    if not actor_types.is_actor_type(schema, fm.get("type")):
        return None
    was_actor = prev_fm is not None and actor_types.is_actor_type(schema, prev_fm.get("type"))
    if was_actor and not actor_grounding.renamed(prev_fm, fm, p.stem):
        return None
    documents, labels, unresolved = _actor_source_documents(fm)
    names = actor_grounding.actor_names(fm, p.stem)
    aliases = actor_grounding.actor_aliases(fm)
    if was_actor:
        # A rename judges only the NEW names (#1014). A new name the deployment already holds as a
        # non-actor (a vendor, a malware family) is always judged: no source tie can make the page
        # that entity (QA of !1297). Any other new name a cited source binds to a kept name is the
        # same actor (a vendor's "X (aka Y)"); the rest must ground on their own.
        new, kept = actor_grounding.rename_split(prev_fm, fm, p.stem)
        held = _actor_namesakes(p, new, schema)
        held_keys = {actor_grounding.name_key(value) for value in held}
        namesakes = [name for name in new if actor_grounding.name_key(name) in held_keys]
        names, aliases = namesakes or actor_grounding.untied_names(new, kept, documents), []
        if not names:
            return None
    else:
        held = _actor_namesakes(p, names, schema)
    failure = actor_grounding.ground_actor_identity(
        names, documents, publisher=fm.get("publisher"),
        labels=labels, unresolved=unresolved, aliases=aliases, non_actor_names=held,
    )
    if failure is None:
        return None
    if was_actor:
        # A stored page no resolvable source ever grounded is unverifiable, not contradicted, so a
        # rename of it is refused only when the deployment already holds the new name as something
        # else (a vendor, a malware family): that repurposes the page (QA of !1297).
        unverifiable = (actor_grounding.UNSOURCED, actor_grounding.NO_RESOLVABLE_SOURCE)
        if failure.code in unverifiable and not namesakes:
            return None
        action = f"renaming this actor to {names[0]!r}"
    elif prev_fm is not None:
        action = (f"retyping this {str(prev_fm.get('type') or 'untyped')!r} page to actor type "
                  f"{str(fm.get('type'))!r}")
    else:
        action = "creating an actor"
    return (
        f"rejected: {action} needs a cited source that names the subject and affirms it as an "
        f"adversary ({failure}). The page's own prose is not evidence and a provenance label is "
        "not a source: cite the canonical `sources/` page whose text says this subject attacked, "
        "claimed, targeted or operates as a threat group, or classify it with a non-actor type "
        "(publisher, vendor, identity, malware, tool, campaign) or skip it"
    )
