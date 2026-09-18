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


def _prepend_queue_row(text: str, row: str) -> str:
    """Insert `row` above the existing rows but BELOW the preamble.

    Byte-zero insertion would put a list item in front of the frontmatter, which stops the
    file being a parseable OKF page at all. So the insertion point is the first existing
    row; failing that, the end of the preamble. Anything that is not a row — frontmatter,
    the heading, the explanatory sentence — is preserved in place.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("- "):
            return "".join(lines[:i]) + row + "".join(lines[i:])
    body = text if text.endswith("\n") or not text else text + "\n"
    if body and not body.endswith("\n\n"):
        body += "\n"
    return body + row


def _append_review_queue_once(p: Path, reason: str) -> bool:
    """Ensure one outstanding queue row per canonical page.

    A model tool call and its runner-owned completion receipt are separate
    transactions.  The call can therefore succeed and be replayed after a
    crash or invalid receipt.  Serialize the read/append boundary and make the
    page path the queue identity; later reasons remain available in log.md.

    Rows go at the TOP.  This is a worklist, not a changelog: the reader opens it to see
    what arrived most recently, and appending buried each new row under every row that
    came before it.  Prepending costs a rewrite of the file per new row, which is
    affordable here only because the queue is bounded — one row per canonical page, and
    a resolved page's row is removed.  log.md is NOT bounded (30k lines / 805K on one
    live vault, written on every single write) and is deliberately still appended.
    """
    wiki = _wiki()
    wiki.mkdir(parents=True, exist_ok=True)
    queue = wiki / "_review-queue.md"
    lock = wiki.parent / ".okengine" / "review-queue.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        if not queue.exists():
            _corpus_touch(queue)
            _atomic_write_text(queue, QUEUE_HEADER)
        identity = f"**{_rel(p)}**"
        text = queue.read_text(encoding="utf-8")
        if identity in text:
            return False
        row = f"- {_today()} {identity} — {reason}\n"
        _corpus_touch(queue)
        _atomic_write_text(queue, _prepend_queue_row(text, row))
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
    self_scope = _entities_scope(self_rel)  # '' for a root page, 'acme' for acme/entities/x
    reg = _registry()
    if (
        not reg.name_to_rels and not reg.alias_to_rels
    ):  # pre-v2 artifact -> full scan (old behavior)
        # Scan the incoming page's OWN sub-domain entities dir (walk-up multipack): root entities/ or
        # <sub>/entities/. Root-only rglob missed sub-domain entities entirely (invariant-audit #351).
        scope_dir = (_wiki() / self_scope / "entities") if self_scope else (_wiki() / "entities")
        candidates = [(c, c) for c in scope_dir.rglob("*.md")]
    else:
        rels = set(reg.alias_to_rels.get(incoming_name, []))  # incoming name == existing alias
        for a in incoming_aliases:
            rels |= set(reg.name_to_rels.get(a, []))  # existing name == incoming alias
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


def _strict_slug_hits(p: Path) -> list[Path]:
    """Live pages in this qualified namespace with the same human slug identity (#592).

    The persisted v3 id-index makes this O(number of matches). During a rolling upgrade,
    a pre-v3 artifact has no strict map, so scan only the incoming qualified namespace;
    enforcement must not go blind until the next index refresh.
    """
    qualified = _qualified_namespace(p)
    identity = id_lib.slug_identity(p.stem)
    if not qualified or not identity:
        return []
    reg = _registry()
    if getattr(reg, "has_slug_identity_index", False):
        rels = reg.slug_identity_hits(qualified, p.stem)
        candidates = [_wiki() / rel for rel in rels]
    else:
        candidates = list((_wiki() / qualified).rglob("*.md"))

    hits: list[Path] = []
    for candidate in candidates:
        try:
            if (
                candidate.resolve() == p.resolve()
                or not candidate.is_file()
                or id_index._skip(candidate)
            ):
                continue
            if id_lib.slug_identity(candidate.stem) != identity:
                continue
            cur_fm, _ = _read_page(candidate)
        except (OSError, ValueError):
            continue
        if str(cur_fm.get("status") or "").strip().lower() == "tombstoned":
            continue
        hits.append(candidate)
    return sorted(hits, key=lambda item: _rel(item))


def _norm_url(value: object) -> str:
    """A source URL reduced to a comparable form, or "" if it is not a real URL (#515/#516).

    Deliberately minimal normalization: whitespace and ONE trailing slash. No case folding
    and no query/fragment surgery — a URL path is case-significant on many hosts, and two
    genuinely different pages must never be fused by an over-eager normalizer.

    The scheme check is NOT cosmetic. 1,495 source pages on one deployment carry a `url`
    that is a PLACEHOLDER rather than an address — `UNKNOWN`, literal `null`, blanks. Those
    compare EQUAL to each other, so treating any non-empty string as identity would converge
    every `UNKNOWN` page into one, fusing hundreds of unrelated documents. A placeholder is
    the ABSENCE of identity and must behave exactly like a missing url: undecidable, refuse.
    """
    text = str(value or "").strip()
    if not text or not _REAL_URL.match(text):
        return ""
    return text[:-1] if text.endswith("/") and len(text) > 1 else text


def _is_url_derived_source_id(page_id: object) -> bool:
    """Whether an id is the URL-derived source form `sources:url-<sha256[:20]>`.

    Matched by SHAPE, not by prefix alone, so a hand-written `sources:url-something`
    cannot masquerade as authority-grade identity and win an automatic merge.
    """
    return bool(_URL_SOURCE_ID.match(str(page_id or "").strip()))


def source_url_id(url: object) -> str:
    """The URL-derived source id: `sources:url-<sha256(url)[:20]>`.

    Must stay byte-identical to what `converge_source` mints, or the two writers would
    disagree about the same document's identity. 105 pages already carry this shape.
    """
    text = _norm_url(url)
    if not text:
        return ""
    return f"sources:url-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]}"


def _source_identity_url_id(namespace: str, fm: dict) -> str:
    """Return URL authority only inside the source namespace.

    Keep this boundary in one place: applying the URL identity rule independently to incoming
    and existing pages makes either half look mutation-insensitive even though the pair is the
    security boundary that prevents non-source URL fields from authorizing convergence.
    """
    return source_url_id(fm.get("url")) if namespace == "sources" else ""


def _same_source_url(fm: dict, existing_path: Path) -> bool:
    """Whether an incoming page and an existing one are the same source document.

    True only when BOTH carry a non-empty `url` and they match after minimal
    normalization. Absence is never equality: a page with no URL, or an unreadable
    existing page, is undecidable and must fall through to the refusal — guessing here
    would fuse records.
    """
    incoming = _norm_url(fm.get("url"))
    if not incoming:
        return False
    try:
        existing_fm, _ = _read_page(existing_path)
    except (OSError, ValueError):
        return False
    return _norm_url((existing_fm or {}).get("url")) == incoming


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
    existing canonical, or resolved a weak slug collision to its owner); None to let the
    normal create proceed. Mutates `fm` to stamp the derived `id` so the new page
    is resolvable forever after. No-op (returns None) when the converge/id libs
    are unavailable or no id can be derived."""
    if not _CONVERGE_OK:
        return None
    strict_hits = _strict_slug_hits(p)
    differing_hits: list[Path] = []
    if strict_hits:
        incoming_url_id = ""
        try:
            incoming_namespace = _namespace(p)
            incoming_pid, _ = _page_id_and_kind(
                fm, _governing(p), incoming_namespace, p.stem
            )
            incoming_url_id = _source_identity_url_id(incoming_namespace, fm)
            incoming_ids = {value for value in (incoming_pid, incoming_url_id) if value}
        except Exception:
            incoming_ids = set()
        for hit in strict_hits:
            try:
                existing_fm, _ = _read_page(hit)
                existing_namespace = _namespace(hit)
                existing_pid, _ = _page_id_and_kind(
                    existing_fm, _governing(hit), existing_namespace, hit.stem
                )
                existing_url_id = _source_identity_url_id(existing_namespace, existing_fm)
                existing_ids = {
                    value for value in (
                        existing_pid,
                        existing_url_id,
                    ) if value
                }
            except Exception:
                # The strict hit was already proved live and separator-equivalent. If its
                # content identity cannot be evaluated, fail closed instead of assuming sameness.
                differing_hits.append(hit)
                continue
            if incoming_url_id and incoming_url_id == existing_url_id:
                # A legacy source can still carry a title-derived id, so the normal id-index
                # lookup below cannot find it by URL. The strict path hit has already located
                # the page; converge through that exact canonical path using the shared URL as
                # authority-grade evidence rather than either refusing or creating a duplicate.
                return _converge(_rel(hit), fm, body)
            if not incoming_ids or incoming_ids.isdisjoint(existing_ids):
                differing_hits.append(hit)
    if differing_hits:
        rels = ", ".join(_rel(hit) for hit in differing_hits[:5])
        # The attempted page is deliberately not created, so queueing `path` would produce an
        # unopenable phantom row (#539). Queue the existing canonical instead: that is the page a
        # reviewer can inspect and amend while deciding whether this weak spelling match is real.
        _flag(
            _rel(differing_hits[0]),
            f"slug id collision (separator-insensitive) on create from {path}; matches {rels}",
        )
        return (
            f"refused: slug id spelling collides with existing canonical(s) ({rels}) — "
            "flagged for review; weak spelling identity never auto-merges"
        )
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
        incoming_aliases = {
            id_lib.normalize_key(str(a)) for a in incoming_aliases if str(a).strip()
        }
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
            return (
                f"refused: entity alias matches multiple canonicals ({rels}) — flagged for review"
            )
    namespace = _namespace(p)
    try:
        schema = _governing(p)  # sub-domain aware (okengine#177); namespace is the bare mint scope
        pid, kind = _page_id_and_kind(fm, schema, namespace, p.stem)
    except Exception:  # pragma: no cover - id derivation is best-effort
        return None
    if not pid:
        return None

    # okengine#515: a source page carrying a `url` is identified BY that url. The
    # URL-derived id is strong and convergent; a title slug is neither — 44,480 source
    # pages carried title slugs against 105 with the strong form, which is why the same
    # document arriving on a second path collided instead of converging.
    #
    # TWO-KEY lookup, deliberately. Minting the url id and checking only that would MISS
    # a legacy holder still carrying the title slug, and would then create a duplicate
    # rather than refuse one — strictly worse than the bug being fixed. So both candidate
    # ids are resolved before deciding, and the strong id is stamped only on a page that
    # is genuinely new. This makes the change safe to ship BEFORE the legacy re-id
    # backfill, rather than depending on ordering.
    url_id = source_url_id(fm.get("url")) if namespace == "sources" else ""
    candidates = [c for c in dict.fromkeys((url_id, pid)) if c]
    fm["id"] = url_id or pid  # stamp the strongest available content-derived id

    collision_rel: Optional[str] = None
    collision_path: Optional[Path] = None
    matched = ""
    for candidate in candidates:
        rel = _registry().resolve(candidate)
        if not rel:
            continue
        holder = _wiki() / rel
        if not holder.exists() or holder.resolve() == p.resolve():
            continue  # stale index entry or the same page
        collision_rel, collision_path, matched = rel, holder, candidate
        break
    if collision_rel is None:
        return None  # genuinely new identity -> create normally
    # A live page at a different path already owns one of this page's candidate ids.
    if matched == url_id:
        # Same URL => same document, by a strong key. Converge, never duplicate.
        return _converge(path, fm, body)
    if kind == "authority":
        # Same real-world entity (authority ids are globally unique to the type)
        # -> converge into the canonical instead of duplicating it.
        return _converge(path, fm, body)
    # A minted-slug id is a WEAK key, and it was deciding BOTH directions wrongly for
    # sources. Measured on one live deployment, 83 collisions:
    #
    #   74 (89%)  pages that AGREED on `url` — ONE document arriving on two paths (the
    #             vault carries five date-partition spellings, so a path-based existence
    #             check misses across them and the title slug is what catches it). These
    #             were refused, so the second capture was simply lost.
    #    9 (11%)  genuinely DIFFERENT documents that slugged alike (two unrelated Show HN
    #             posts sharing a title). These were ALSO refused — a real, distinct
    #             document rejected because of a title collision. A false refusal, not a
    #             save.
    #
    # URL identity fixes both: same url converges here, different urls now derive
    # different ids and never reach this branch at all (okengine#515).
    #
    # The unresolved case below still MUST NOT auto-merge: a weak slug alone is insufficient
    # evidence to fuse two records. Sources are documents, so absent URL identity remains a hard
    # refusal: inviting an explicit slug-only converge would undo #516's evidence boundary. It is
    # still deterministic operational routing rather than human review, however, so record it in
    # the ledger without creating a phantom queue row for a path that was never written.
    if collision_path is not None and _same_source_url(fm, collision_path):
        return _converge(path, fm, body)
    if namespace == "sources":
        _append_log(
            f"- {_today()} mcp-write collision-refused {_rel(p)} -> {collision_rel} (id {pid})"
        )
        return (
            f"refused: weak source slug id {pid} already used by {collision_rel} — "
            "a URL identity is required to converge; no review item created"
        )
    # For non-source identities the caller can explicitly converge into the known canonical under
    # the normal authorization, ownership, schema, and field-conflict gates. Return that canonical
    # rather than enqueueing the nonexistent attempted path.
    _append_log(
        f"- {_today()} mcp-write collision-resolved {_rel(p)} -> {collision_rel} (id {pid})"
    )
    return (
        f"resolved existing canonical {collision_rel} (id {pid}) — no page created; "
        f"use converge_entity on {collision_rel} to merge explicitly"
    )


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
