"""Which page types are ACTORS, resolved from the composed schema (okengine#1020).

Every actor guard, detector and panel used to test ``type == "actor"``. A pack that names its
adversaries differently (one live pack keeps them under two STIX-style types) got no grounding at
all, and the corpus audit reported 0 ungrounded actors: a vacuous pass. The engine cannot learn
those names, because a type name is domain vocabulary. So a pack DECLARES the role on the type:

    types:
      intrusion-set: {required: [type], role: actor}

The declaration travels with the type definition wherever composition carries it (co-install,
bundle, extension fragment), so a composed schema needs no separate list kept in sync. ``actor``
stays an actor type by default: it is the engine's own actor contract (``actor_type``,
``actor_identity_validated``). A ``type_aliases`` key that maps onto an actor type is one too.

One resolver for every caller: the enforced write path, the deterministic importers' guard, the
corpus audit, the index generators and the cockpit panels. Everything here is pure; callers hand
in the schema they already resolved.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable

#: The value of ``types.<t>.role`` that marks an actor type.
ACTOR_ROLE = "actor"
#: The engine's own actor type. Always an actor type, declared or not.
DEFAULT_ACTOR_TYPE = "actor"

#: Frontmatter fields whose page references point AT an actor. ``actor`` is the engine's word for
#: the role and ``attributed_to`` the grounding predicate's own attribution phrase, not domain
#: terms; a pack that relates campaigns or malware to their operators this way shows which of its
#: types hold actors, declared or not.
ACTOR_REFERENCE_FIELDS = ("actor", "actors", "attributed_to")
#: A type is actor-like when it receives at least this many actor references ...
UNDECLARED_MIN_REFERENCES = 3
#: ... and at least this share of all resolved actor references. A stray link to a malware page
#: is noise; a type that receives a fifth of the corpus's actor references is where actors live.
UNDECLARED_MIN_SHARE = 0.2


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def actor_types(schema: Any) -> frozenset[str]:
    """The casefolded actor types of ``schema``: ``actor``, every type declaring ``role: actor``,
    and every ``type_aliases`` key that maps onto one of those."""
    found = {DEFAULT_ACTOR_TYPE}
    if not isinstance(schema, Mapping):
        return frozenset(found)
    types = schema.get("types")
    if isinstance(types, Mapping):
        found.update(_fold(name) for name, spec in types.items()
                     if isinstance(spec, Mapping) and _fold(spec.get("role")) == ACTOR_ROLE)
    aliases = schema.get("type_aliases")
    if isinstance(aliases, Mapping):
        found.update(_fold(alias) for alias, target in aliases.items() if _fold(target) in found)
    return frozenset(found)


def is_actor_type(schema: Any, value: Any) -> bool:
    """True when a page of type ``value`` is an actor under ``schema``."""
    return bool(_fold(value)) and _fold(value) in actor_types(schema)


def is_default_actor_type(schema: Any, value: Any) -> bool:
    """True for the engine's own ``actor`` type (or an alias of it).

    The write path has re-judged ``actor`` pages on every update since okengine#846. A type that
    a pack declares later is judged on create, retype and rename only, so the declaration cannot
    start refusing updates to pages that were stored before it existed.
    """
    typ = _fold(value)
    if typ == DEFAULT_ACTOR_TYPE:
        return True
    aliases = schema.get("type_aliases") if isinstance(schema, Mapping) else None
    if not isinstance(aliases, Mapping):
        return False
    return any(_fold(alias) == typ and _fold(target) == DEFAULT_ACTOR_TYPE
               for alias, target in aliases.items())


def undeclared_actor_types(schema: Any, referenced: Mapping[str, int]) -> dict[str, int]:
    """The types that hold actors without being declared actor types.

    ``referenced`` counts the resolved targets of the corpus's actor references
    (:data:`ACTOR_REFERENCE_FIELDS`) by the target page's type. A type that is not an actor type
    but receives at least :data:`UNDECLARED_MIN_REFERENCES` of them, and at least
    :data:`UNDECLARED_MIN_SHARE` of the total, is reported: grounding cannot see its pages, so a
    count of zero there would be a vacuous pass.
    """
    total = sum(referenced.values())
    return {typ: count for typ, count in sorted(referenced.items())
            if count >= UNDECLARED_MIN_REFERENCES and count >= UNDECLARED_MIN_SHARE * total
            and not is_actor_type(schema, typ)}


def reference_targets(fm: Mapping[str, Any]) -> list[str]:
    """The page references in a page's actor fields, normalized to ``ns/.../slug``."""
    found: list[str] = []
    for field in ACTOR_REFERENCE_FIELDS:
        raw = fm.get(field)
        values: Iterable[Any] = raw if isinstance(raw, list) else [raw]
        for value in values:
            if not isinstance(value, str):
                continue
            text = value.strip().strip("[]").partition("|")[0].strip()
            if "/" not in text:
                continue  # a bare name is prose, not a page reference
            found.append(text.strip("/").removeprefix("wiki/").removesuffix(".md"))
    return found
