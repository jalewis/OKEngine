#!/usr/bin/env python3
"""converge — the page+field-scoped merge arbitration for converge-on-write.

When two packs write the same (authority-id) page, the engine merges instead of
duplicating (RFC docs/design/composable-okpacks.md §5a). Ownership is **page +
field scoped**, NOT declared-field scoped (the original "owner-wins declared /
last-writer extras" rule inverted its own promise):

  - the **owner** of the page's type may set/change ANY field;
  - a **non-owner** may **add new keys** (attributed) and may **change only fields
    it owns** via an explicit `field_owners` grant (e.g. a hunt pack owns
    `detection` on an `attack-pattern` owned by an attack pack);
  - a non-owner changing a field it does NOT own is a **conflict** — left
    unchanged and surfaced for review, never a silent clobber.

`merge_frontmatter` is a pure function of its inputs (no I/O), so it is trivially
test-vectored. Owner-authorized field *removal* is a separate operation (not a
merge of provided fields) and lives in the write path, not here. Tombstoned-id
handling is the caller's job (never resurrect — §5a).
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field

# The one normalizer for a composed `cron:<job>@<pack>` write actor (okengine#961). It lives with the
# write path's always-installed state, not here: the facade must fence tools even without this lib.
from okengine.write_services.state import lane_actor
from okengine.list_scalars import split_list_scalar

# Server-managed keys the merge never treats as a pack-authored conflict — the
# write path stamps/owns these regardless of caller. `extension_id` is included so a
# converge merge never takes a caller-supplied value for it — it is server-derived
# provenance the write path re-applies from the scoped token / on-disk stamp
# (invariant-audit: forgeable extension_id → mis-attributed purge deletion).
_SERVER_KEYS = {"id", "version", "updated", "last_modified_by",
                "maintained_by", "discovered_by", "created", "created_by", "extension_id"}

# The subset of server keys that are PROVENANCE — set once at create and never re-stamped by the
# write path. A converge merge must PRESERVE these (never take an incoming value), or a caller forges
# them (invariant-audit M19). The other server keys (id/version/updated/last_modified_by) ARE
# re-stamped after the merge, so passing them through here is harmless and back-compatible.
_PROVENANCE_KEYS = {"created", "created_by", "discovered_by", "extension_id"}

# Append-only backlinks: the capture(s) a page was compiled from. The write path computes `raw`
# from the runner's selection, never from caller content, so page ownership does not govern it.
# Any writer may ADD a capture and none may drop one by merging. `sources` pages are engine-owned,
# so under ownership a pack lane's second capture was a conflict, the page kept one `raw`, and the
# selector re-offered the other capture every run: one page converged 39 times in a day while
# 214 other captures waited (okengine#886).
_APPEND_ONLY_KEYS = {"raw"}

# Citation lists are append-only through a merge as well (okengine#1028): a lane converging a shorter
# `sources` replaced the page's list and dropped its existing citations (252 logged segments on one
# deployment). A merge keeps every stored citation and adds the new ones, judged by target so
# `[[sources/x]]` and `sources/x` are one citation. Dropping one is an explicit `remove=`, which the
# write path's identity guard holds for review. Mirrors okengine.identity_guard.CITATIONS.
CITATION_KEYS = frozenset({"sources", "basis"})


def _citation_target(item) -> str:
    text = str(item).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    return text.split("|")[0].split("#")[0].strip().removesuffix(".md")


def _citation_additions(prev, new) -> list:
    """Each incoming citation whose target the stored list (and this list) does not yet hold."""
    seen = {_citation_target(item) for item in _as_list(prev)}
    added = []
    for item in _as_list(new):
        if _citation_target(item) not in seen:
            seen.add(_citation_target(item))
            added.append(item)
    return added


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _append_only_union(prev, new):
    """`prev` plus every item of `new` it lacks, in order. A single item stays scalar; with no
    items at all `prev` is returned as it was (an empty `raw` never becomes `[]`)."""
    items = _as_list(prev)
    items += [item for item in _as_list(new) if item not in items]
    if len(items) > 1:
        return items
    return items[0] if items else prev


# Lanes whose job is to CREATE missing pages. When identity resolution lands such a lane on a page
# that already exists, it may only ADD to it: the existing body stays (unless the page has none,
# enforced by the caller), list fields are unioned, existing values are kept, and `remove` is
# ignored. Live on okcti (2026-09-30): entity-backfill, handed a 2018 source mentioning Lazarus
# Group, "created" the actor over its canonical page -- the MITRE description became a 105-char
# one-liner, which withdrew the Lazarus -> North Korea attribution downstream; on 11 other pages
# its `sources` list replaced the page's own. Lanes that recompile whole pages through converge
# (page-quality-enrich, the model-write repair drain) keep replace semantics: this is scoped by the
# server-bound write actor, never by a model argument.
# concept-backfill has the same create-missing role: a title-minted id can resolve to an existing
# concept at another path (re-verify of okengine!1210). Matched by `lane_actor`, so a composed
# instance of either lane is additive too.
ADDITIVE_ACTORS = frozenset({"cron:entity-backfill", "cron:concept-backfill"})



# Keys that decide what a page IS called or how it is classified. An additive merge never adds them
# to an existing page (re-verify round 5): a create redirect added `title: Hidden Cobra`, which every
# surface prefers over `name`, and `link_stub: true`, which demotes the page to a redirect. A page
# with neither `title` nor `name` may still gain one.
_NO_ADD_KEYS = frozenset({"link_stub", "redirect_to", "tombstoned", "status", "superseded_by",
                          "rejected_aliases", "canonical"})
_DISPLAY_KEYS = frozenset({"title", "name"})


# A write that asked to CREATE and was redirected by dedup (alias, URL, authority id) onto an
# existing page -- or a raw capture of an already-compiled URL -- is additive whoever the caller
# is: a creator never intends to replace (okengine#962). Only a genuine create is marked; an
# explicit converge that falls through _create keeps its own semantics.
_CREATE_REDIRECT = contextvars.ContextVar("okengine_create_redirect", default=False)


def create_redirect(enabled: bool = True):
    """Context manager marking the enclosed converge as a create-intent write (see additive_gate).
    `enabled=False` is a no-op, so a call site can pass whether its caller really asked to create."""
    if not enabled:
        return contextlib.nullcontext()

    @contextlib.contextmanager
    def _marked():
        token = _CREATE_REDIRECT.set(True)
        try:
            yield
        finally:
            _CREATE_REDIRECT.reset(token)
    return _marked()


def record_identity(registry, rel: str, merged_fm: dict) -> None:
    """Index a merged page's name and aliases in the in-process id-index, as _create does for a
    new page (best-effort). Without it, an alias one merge added was invisible to the next merge's
    hijack check, so two pages could gain the same alias in one process (#962)."""
    try:
        if str(merged_fm.get("status") or "").lower() != "tombstoned":
            registry._add_identity(rel, merged_fm)
    except Exception:  # the registry is best-effort; a failure never fails the write
        pass


def stub_upgrade(current_body: str, new_body: str, current_fm: dict | None = None) -> bool:
    """True when an additive merge may write `new_body` over `current_body`.

    Any page: only when it has no body (whitespace only) and `new_body` has content. A short body
    on an entity or concept is curated content and is never replaced.

    A `source` page (a compiled capture of one URL): the FULLER capture wins -- `new_body` replaces
    the body when it is at least as long (non-whitespace characters). A snippet or paywalled
    re-capture never overwrites a full extract (#962), while a fuller re-extraction can still
    correct a bad one and fills a stub. User decision, 2026-10-01. The caller withholds the body
    first on an identity conflict, so another document's body never lands either way."""
    meaningful = lambda text: len("".join((text or "").split()))  # noqa: E731
    if not meaningful(current_body):
        return meaningful(new_body) > 0
    if str((current_fm or {}).get("type") or "") != "source":
        return False
    return meaningful(new_body) >= meaningful(current_body)


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _same_source_url(prev_fm: dict, incoming_fm: dict, url_identity) -> bool:
    """Both sides are the same SOURCE document by URL identity (never by a placeholder)."""
    ident = url_identity(prev_fm.get("url")) if url_identity is not None else ""
    return (str(prev_fm.get("type") or "") == "source" and bool(ident)
            and ident == url_identity(incoming_fm.get("url")))


def additive_payload(prev_fm: dict, incoming_fm: dict, dropped: list | None = None, fold=None,
                     alias_ok=None, url_identity=None) -> dict:
    """`incoming_fm` restricted to additions against `prev_fm`: new (or empty) keys pass, a list
    field the page already holds as a list becomes the union (current order first), and any other
    key the page already has a value for is dropped so the merge keeps the current value. When
    `dropped` is given, each held value that DISAGREES with the incoming one is appended to it as a
    `(key, current, incoming)` conflict, so the caller's identity-conflict guard (okengine#901) still
    sees a write that describes a different document. `id` never passes (an alias redirect swapped
    it, or stamped a title-minted id on an id-less page); `type` passes only when the page has none
    (no retyping). Pure."""
    out = {}
    # On a SOURCE page the URL is the document's identity: a retitled or paraphrased capture of the
    # same URL is not a "different document" by its title (#962 -- the capture's backlink was
    # otherwise dropped and the capture re-queued forever). Compared through the write path's
    # URL-identity rule (`url_identity`): a placeholder (`UNKNOWN`) or bare endpoint carries no
    # identity and never matches. Not on other types -- two entities can cite one report's URL.
    same_url = _same_source_url(prev_fm, incoming_fm, url_identity)
    held_names = [prev_fm[key] for key in sorted(_DISPLAY_KEYS) if not _empty(prev_fm.get(key))]
    # Names are compared the way the alias dedup that routed the write matches them (the write
    # path passes id_lib.normalize_key), so a dedup-verified alias is never a "different document"
    # over punctuation, spacing or diacritics (#962).
    fold = fold or (lambda v: str(v).strip().casefold())
    # The page's own aliases are agreement, not a different document: a write that reached
    # ShinyHunters through its verified alias UNC6240 is the same entity (okengine#324). A legacy
    # comma-string alias list is split as _alias_matches splits it: split_list_scalar, the write
    # path's own coercion (okengine#1042 [19]).
    raw_aliases = prev_fm.get("aliases")
    held_aliases = raw_aliases if isinstance(raw_aliases, list) else (
        split_list_scalar(str(raw_aliases)) if not _empty(raw_aliases) else [])
    known = {fold(n) for n in [*held_names, *held_aliases]}
    for key, value in incoming_fm.items():
        current = prev_fm.get(key)
        if key == "id" or key in _NO_ADD_KEYS:
            continue  # the page is already resolved; an incoming id never replaces or stamps it
        if key == "aliases":
            # Alias accrual (okengine#324) is kept, but an alias another live page already holds as
            # its name or alias would re-route that entity's later writes onto this page (APT38
            # onto Lazarus). `alias_ok(alias)` is the write path's id-index check; without it no
            # alias is added (fail closed).
            incoming = value if isinstance(value, list) else split_list_scalar(str(value or ""))
            new = [a for a in incoming if str(a).strip() and fold(a) not in known
                   and alias_ok is not None and alias_ok(a)]
            if new:
                # A legacy comma-string is stored as the parts the identity index reads from it
                # (okengine#1028): kept verbatim as one LIST entry, the index stopped routing them.
                kept = raw_aliases if isinstance(raw_aliases, list) else held_aliases
                out[key] = list(kept) + list(dict.fromkeys(new))
            continue
        if key in _DISPLAY_KEYS and held_names:
            # Compared against WHICHEVER display key the page holds: `title: Andariel` sent to a
            # page holding only `name: Lazarus Group` is the same disagreement.
            if dropped is not None and not same_url and not _empty(value) and fold(value) not in known:
                dropped.append((key, current if not _empty(current) else held_names[0], value))
            continue
        if key in _APPEND_ONLY_KEYS or key not in prev_fm or _empty(current):
            # `raw` is append-only: merge_frontmatter unions it, so pass it through.
            out[key] = value
        elif isinstance(value, list) and isinstance(current, list):
            # Only an existing LIST is unioned. A list sent for a field the page holds as a scalar
            # is dropped: unioning it turned `suspected_origin: North Korea` into a second
            # attribution (re-verify round 4). Dropping loses an addition; it destroys nothing.
            added = [item for item in value if item not in current]
            out[key] = current + added
            if added and dropped is not None:
                # Reported too: harmless for ordinary lists (only identity fields are acted on),
                # but a list-valued identity field (`url: [a]` vs `[b]`) must be seen as a
                # disagreement, not silently unioned (#962).
                dropped.append((key, current, value))
        elif dropped is not None and current != value:
            dropped.append((key, current, value))
    return out


def additive_gate(actor: str, prev_fm: dict, incoming_fm: dict, remove: str = "", normalize=None,
                  identity=None, fold=None, alias_ok=None, url_identity=None):
    """`(payload, remove_list, additive, dropped)` for a converge into an existing page.

    For an ADDITIVE_ACTORS caller (any composed `@<pack>` suffix removed), or any caller inside
    `create_redirect()` (a genuine create redirected onto an existing page, #962), the payload is
    `additive_payload(...)`, nothing is removed, and `dropped` lists the held values the write
    disagreed with as `(key, current, incoming)` conflicts for the identity guard. For any other
    caller the payload is unchanged, `remove` is parsed as the comma-separated list, `dropped` is
    empty, and `normalize` is never called.

    `normalize` maps frontmatter onto the schema's canonical keys (the write path's drift
    normalizer). Both sides are compared canonically, so a value held under one alias (`origin`)
    blocks the lane writing another alias (`country`) of the same field. If it raises, the gate fails
    CLOSED: nothing is added, and disagreements are still reported on the raw keys.

    `identity(dropped)` returns the identity fields among the disagreements (the write path's
    okengine#901 check, which knows the pack's `identity_fields`). If any, the write describes a
    DIFFERENT document: nothing is added at all, so another entity's attributes never land."""
    if lane_actor(actor) not in ADDITIVE_ACTORS and not _CREATE_REDIRECT.get():
        return incoming_fm, [s.strip() for s in (remove or "").split(",") if s.strip()], False, []
    dropped: list = []
    if normalize is not None:
        try:
            prev_n, incoming_n = {**prev_fm, **normalize(prev_fm)}, normalize(incoming_fm)
        except Exception:
            additive_payload(prev_fm, incoming_fm, dropped, fold, alias_ok, url_identity)
            return {}, [], True, dropped
        prev_fm, incoming_fm = prev_n, incoming_n
    payload = additive_payload(prev_fm, incoming_fm, dropped, fold, alias_ok, url_identity)
    if identity is not None and identity(dropped):
        # A different document adds nothing -- except its append-only capture backlink (`raw`),
        # so a recorded capture is never re-queued forever (#962).
        return {k: v for k, v in payload.items() if k in _APPEND_ONLY_KEYS}, [], True, dropped
    # "same-url": the write is POSITIVELY the same source document, so the fuller-capture body rule
    # may apply. Any other additive write may only fill an empty body (#962 re-verify: a write
    # carrying only a copied page id has nothing to disagree on, which is not agreement).
    return payload, [], ("same-url" if _same_source_url(prev_fm, incoming_fm, url_identity) else True), dropped


@dataclass
class MergeDecision:
    added: list[str] = field(default_factory=list)        # new keys the caller contributed
    updated: list[str] = field(default_factory=list)      # existing keys the caller (legally) changed
    removed: list[str] = field(default_factory=list)      # existing keys the (authorized) caller dropped
    conflicts: list[tuple[str, object, object]] = field(default_factory=list)  # (key, current, attempted)
    unchanged: list[str] = field(default_factory=list)    # same key, same value

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def _owns_field(key: str, *, owner_pack, caller_pack, field_owners: dict) -> bool:
    """True iff the caller may mutate an existing `key`. With no declared owner
    (single-pack / no composition) there is no enforcement — changes apply
    (back-compat). Otherwise the caller must own the page (== owner_pack) or hold
    an explicit per-field grant for this key."""
    if owner_pack is None:
        return True
    if caller_pack is not None and caller_pack == owner_pack:
        return True
    return caller_pack is not None and field_owners.get(key) == caller_pack


def merge_frontmatter(prev_fm: dict, incoming_fm: dict, *,
                      owner_pack=None, caller_pack=None,
                      field_owners: dict | None = None,
                      remove: "list[str] | None" = None) -> tuple[dict, MergeDecision]:
    """Merge `incoming_fm` into `prev_fm` under page+field ownership.

    Returns ``(merged_fm, decision)``. New keys are added; same-value keys are
    no-ops; an existing key with a different value is applied only when the caller
    owns it (page owner, or `field_owners[key] == caller_pack`), otherwise it is a
    conflict (current value kept). `remove` lists fields the caller wants dropped —
    permitted ONLY for a field the caller owns (a non-owner removal is a conflict),
    and never for server-managed keys. Append-only keys (`raw`) take the union of both sides
    whoever writes: a capture can be added, never dropped, by a merge. `maintained_by` unions the caller. Pure:
    never raises, never writes.
    """
    field_owners = field_owners or {}
    merged = dict(prev_fm)
    dec = MergeDecision()

    for key, new_val in incoming_fm.items():
        if key in _SERVER_KEYS:
            # Server-managed; never a pack conflict. PRESERVE provenance keys (created/created_by/
            # discovered_by) — a caller must not forge them, and the write path does NOT re-stamp them
            # (invariant-audit M19). `merged` already carries prev_fm's value, so skip. The rest
            # (id/version/updated/last_modified_by) are re-stamped after merge → pass through as before.
            if key not in _PROVENANCE_KEYS:
                merged[key] = new_val
            continue
        if key in CITATION_KEYS and isinstance(prev_fm.get(key), list):
            added = _citation_additions(prev_fm[key], new_val)
            if added:
                merged[key] = list(prev_fm[key]) + added
                dec.updated.append(key)
            else:
                dec.unchanged.append(key)
            continue
        if key in _APPEND_ONLY_KEYS and key in prev_fm:
            union = _append_only_union(prev_fm[key], new_val)
            if union == prev_fm[key]:
                dec.unchanged.append(key)
            else:
                merged[key] = union
                dec.updated.append(key)
            continue
        if key not in prev_fm:
            merged[key] = new_val
            dec.added.append(key)
        elif prev_fm[key] == new_val:
            dec.unchanged.append(key)
        elif _owns_field(key, owner_pack=owner_pack, caller_pack=caller_pack,
                         field_owners=field_owners):
            merged[key] = new_val
            dec.updated.append(key)
        else:
            dec.conflicts.append((key, prev_fm[key], new_val))  # keep current

    # Owner-authorized removal (exempt from the field-loss guard): only the owner
    # of a field may drop it; a non-owner removal is a conflict; server keys never.
    for key in (remove or []):
        if key in _SERVER_KEYS or key not in merged:
            continue
        if _owns_field(key, owner_pack=owner_pack, caller_pack=caller_pack,
                       field_owners=field_owners):
            del merged[key]
            dec.removed.append(key)
        else:
            dec.conflicts.append((key, merged[key], "<remove>"))

    if caller_pack is not None:
        prov = merged.get("maintained_by")
        prov = list(prov) if isinstance(prov, (list, tuple)) else ([prov] if prov else [])
        if caller_pack not in prov:
            prov.append(caller_pack)
        merged["maintained_by"] = prov

    return merged, dec
