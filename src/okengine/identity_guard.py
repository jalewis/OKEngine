"""Identity-field guard for writes to EXISTING pages (okengine#1028).

Pack ownership answers "may this pack write here", not "is this identity change correct". A model
lane that owns a page still must not retype it, rename it, or strip its curated fields on its own
authority: a replay of the logged conflicts showed lanes routinely attempting exactly that (an
actor retyped to `identity`, `xloader` retitled "Formbook", an actor's aliases, country and
description removed). Such a change is HELD: the stored value stays, the change is queued for
review, and the rest of the write applies.

Identity changes, judged on an existing page (``before``) against what the write would store
(``after``):

* ``type`` changed (a retype out of a type as much as into one);
* ``title`` or ``name`` changed;
* an alias dropped from ``aliases`` (adding one is curation; dropping one re-routes every later
  identity match, so it is identity for every type, not only actors);
* any other curated field removed or emptied (engine-managed keys excepted).

A field the page did not carry is never an identity change: backfilling a missing ``type`` or
``title`` is enrichment.

A lane may apply identity changes directly only with an explicit grant, ``identity_edit`` in its
server-bound output contract (see ``permission``). The operator principal (the actor-less admin
caller) holds every grant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from okengine.list_scalars import split_list_scalar

#: The grantable identity fields. `citations` covers dropping a citation; `remove` covers removing
#: or emptying any other curated field.
FIELDS = frozenset({"type", "title", "name", "aliases", "citations", "remove"})
#: In `retype_from`, the governing schema's `classify_catchall` types.
CATCHALL = "@classify_catchall"
#: The identity scalars, each with the kind of change a new value makes.
_SCALARS = {"type": "retype", "title": "rename", "name": "rename"}
#: The display keys, in the order the identity index reads them (`name`, else `title`).
_DISPLAY = ("name", "title")
_ALIASES = "aliases"
#: Citation lists (base-schema `sources`, and `basis` on inference pages): a dropped entry loses
#: provenance (grant `citations`, held by the deterministic citation-repair lanes). Converge never
#: drops one at all.
CITATIONS = ("sources", "basis")
#: List fields judged entry by entry: field -> the kind of change a dropped entry makes.
_LISTS = {_ALIASES: "alias-removal", **{field: "citation-removal" for field in CITATIONS}}
_JUDGED_APART = frozenset({*_SCALARS, *_LISTS})
_REMOVALS = frozenset({"removal"})
_CITATION_REMOVALS = frozenset({"citation-removal"})
_RETYPES = frozenset({"retype"})
_LIST_REMOVALS = frozenset({"alias-removal", "citation-removal"})
_BODY_KINDS = frozenset({"retype", "rename"})
_ANY_REMOVAL = frozenset({"removal", "alias-removal", "citation-removal"})


@dataclass(frozen=True)
class Change:
    field: str
    kind: str            # retype | rename | alias-removal | citation-removal | removal
    before: Any
    after: Any = None

    @property
    def grant(self) -> str:
        if self.kind in _CITATION_REMOVALS:
            return "citations"
        return "remove" if self.kind in _REMOVALS else self.field


@dataclass(frozen=True)
class Permission:
    fields: frozenset
    retype_from: frozenset | None = None


#: The operator principal's grant: every identity change applies directly.
ALL = Permission(FIELDS)
NONE = Permission(frozenset())


def permission(grant: object) -> Permission:
    """The `identity_edit` grant of a lane contract or extension capability; NONE when absent or
    malformed (fail closed: a grant the engine cannot read grants nothing)."""
    if not isinstance(grant, Mapping):
        return NONE
    fields = grant.get("fields")
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        return NONE
    retype_from = grant.get("retype_from")
    if retype_from is not None and not (
            isinstance(retype_from, list) and all(isinstance(t, str) for t in retype_from)):
        return NONE
    return Permission(frozenset(fields) & FIELDS,
                      None if retype_from is None else frozenset(retype_from))


def _present(value: Any) -> bool:
    """Whether `value` holds anything. Whitespace, and a list or mapping whose every entry is blank,
    hold nothing: setting a curated field to `" "` or `[None]` empties it as surely as removing it."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple)):
        return any(_present(item) for item in value)
    if isinstance(value, dict):
        return any(_present(item) for item in value.values())
    return value is not None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _alias_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return [v for v in value if _present(v)]
    return [value] if _present(value) else []


def _indexed_aliases(value: Any) -> list:
    """The aliases exactly as the identity index records them (`id_index._add_identity`): a
    scalar string is split the way the write path coerces it (`split_list_scalar`: a comma inside
    brackets, a date or a number is not a delimiter, okengine#1042 [19]), a list entry is taken
    whole, anything else holds none. A comma-joined LIST entry is one (unroutable) alias, so
    dropping its parts is a real drop."""
    if isinstance(value, str):
        return split_list_scalar(value)
    return _alias_list(value) if isinstance(value, (list, tuple)) else []


def _alias_key(value: Any) -> str:
    return _text(value).casefold()


def _citation_key(value: Any) -> str:
    """A citation's target, whichever spelling holds it (`[[sources/x|label]]`, `sources/x.md`)."""
    text = _text(value)
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    return text.split("|")[0].split("#")[0].strip().removesuffix(".md")


#: How a list field's entries are read and compared: aliases as the identity index reads them, by
#: case-folded spelling; a citation by its target.
_ENTRY_RULES = {_ALIASES: (_alias_key, _indexed_aliases)}


def _effective_name(before: Mapping, stem: str) -> Any:
    """The page's name as the identity index reads it: `name`, else `title`, else the stem."""
    for field in _DISPLAY:
        if _present(before.get(field)):
            return before.get(field)
    return stem


def identity_changes(before: Mapping, after: Mapping, exempt: Iterable[str] = (), *,
                     stem: str = "", name_key: Callable[[Any], str] | None = None) -> list[Change]:
    """Every identity change `after` makes to the stored page `before`.

    `title` and `name` are ONE identity (okengine v0.15.11 audit #5): the identity index keys a
    page by `name`, else `title`, else its `stem`. Changing a key the page carries is a rename.
    So is ADDING the other key with a value that is not the page's effective name under
    `name_key` (the index's normalization): a page stored with only `title: X` that gains
    `name: Y` is re-keyed under Y. Adding a key that names the page what it is already called,
    a nameless page included (its stem), is enrichment."""
    exempt = set(exempt)
    key = name_key or _alias_key
    out: list[Change] = []
    if _present(before.get("type")) and _text(before.get("type")) != _text(after.get("type")):
        out.append(Change("type", "retype", before.get("type"), after.get("type")))
    effective = _effective_name(before, stem)
    for field in ("title", "name"):
        stored, proposed = before.get(field), after.get(field)
        if _present(stored):
            if _text(stored) != _text(proposed):
                out.append(Change(field, "rename", stored, proposed))
        elif _present(proposed) and _present(effective) and key(proposed) != key(effective):
            out.append(Change(field, "rename", effective, proposed))
    for field, kind in _LISTS.items():
        key, entries = _ENTRY_RULES.get(field, (_citation_key, _alias_list))
        kept = {key(v) for v in entries(after.get(field))}
        dropped = [v for v in entries(before.get(field)) if key(v) not in kept]
        if dropped:
            out.append(Change(field, kind, dropped))
    for key, value in before.items():
        if key in _JUDGED_APART or key in exempt:
            continue
        if _present(value) and not _present(after.get(key)):
            out.append(Change(str(key), "removal", value, after.get(key)))
    return out


def permits(grant: Permission, change: Change, before_type: Any,
            catchall: Iterable[str] = ()) -> bool:
    """Whether `grant` lets `change` apply directly."""
    if change.grant not in grant.fields:
        return False
    if change.kind not in _RETYPES or grant.retype_from is None:
        return True
    allowed = set(grant.retype_from) - {CATCHALL}
    if CATCHALL in grant.retype_from:
        allowed |= {str(t) for t in catchall}
    return _text(before_type) in allowed


def hold(after: dict, before: Mapping, held: Iterable[Change]) -> None:
    """Restore every held change in `after` to the stored value, in place."""
    for change in held:
        if change.kind in _LIST_REMOVALS:
            after[change.field] = _alias_list(after.get(change.field)) + list(change.before)
        elif _present(before.get(change.field)):
            after[change.field] = before[change.field]
        else:                      # a display key the page never carried: the addition is held
            after.pop(change.field, None)


def holds_body(held: Iterable[Change]) -> bool:
    """A held retype or rename also holds a body replacement in the same write: the new body
    describes the identity the page was refused."""
    return any(change.kind in _BODY_KINDS for change in held)


def flag(change: Change) -> str:
    """The review reason recorded for a held change."""
    if change.kind in _ANY_REMOVAL:
        detail = f"removal of {change.before!r}"
    else:
        detail = f"{change.before!r} -> {change.after!r}"
    return (f"identity change held for review (okengine#1028): field `{change.field}`: {detail}; "
            "the stored value is kept")
