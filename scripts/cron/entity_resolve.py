"""Resolve an incoming entity record to an existing canonical by name/alias match,
requiring stronger-than-single-alias evidence (okengine#39).

Cross-source importers map each source's record onto a shared canonical by matching
names + aliases. Matching on a *single* shared alias OVER-MERGES when an alias token is
reused across sources — i.e. one source's primary name is another source's alias for a
DIFFERENT entity, so the two distinct entities collapse into one. This resolver requires
either a PRIMARY-name match or >= ``min_alias_matches`` distinct shared keys before it
will merge; a lone shared alias is reported as *ambiguous* (the caller mints a new
canonical and/or flags it for review) instead of silently merging. The concrete
motivating case is recorded in okengine#39.

Domain-agnostic: it knows nothing about entity types or the wiki layout. Callers extract
``(slug, primary_name, aliases)`` records from their own pages and pass the incoming
``(name, aliases)``; the resolver returns a slug to merge into (or ``None``).

Future (okengine#39 "seed later"): a curated cross-source co-reference mapping (supplied
by the pack — see okpacks-library#1) can let a *single* shared alias be trusted when the
pair is in the mapping. That relaxation plugs in via ``trusted`` below without changing
the structural default.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


def normalize(s) -> str:
    """Match key: lowercase alphanumerics only, so 'APT 28' == 'APT28'. Mirrors the
    importers' ``norm()`` so the index and the queries agree. Coerces non-string scalars to
    str first — an importer that wrote a numeric alias (e.g. apt35 `aliases: [10768]`,
    okengine#348) must not crash the whole resolve/canonical-assemble lane on one bad record."""
    return re.sub(r"[^a-z0-9]", "", str(s if s is not None else "").lower())


@dataclass
class CanonicalIndex:
    """Lookup over existing canonicals. ``primary`` is the strong identifier (a canonical's
    own name); ``keys`` maps every name/alias token to the set of canonicals carrying it
    (alias tokens are deliberately allowed to be ambiguous across canonicals — detecting
    that ambiguity is the whole point)."""

    primary: dict[str, str] = field(default_factory=dict)
    keys: dict[str, set[str]] = field(default_factory=dict)

    def add(self, slug: str, primary_name: str, aliases=()) -> None:
        slug = (slug or "").lower()
        if not slug:
            return
        pk = normalize(primary_name)
        if pk:
            self.primary.setdefault(pk, slug)   # first writer wins on a primary collision
            self.keys.setdefault(pk, set()).add(slug)
        for a in aliases or ():
            k = normalize(a)
            if k:
                self.keys.setdefault(k, set()).add(slug)


def build_index(records) -> CanonicalIndex:
    """``records``: iterable of ``(slug, primary_name, aliases)``."""
    idx = CanonicalIndex()
    for slug, name, aliases in records:
        idx.add(slug, name, aliases)
    return idx


@dataclass
class AmbiguousMatch:
    """A single-shared-alias near-match the resolver DECLINED to merge into."""

    candidate: str          # the canonical slug we refused to merge into
    shared: list[str]       # the shared normalized key(s) that tempted the merge


@dataclass
class Resolution:
    slug: str | None        # canonical slug to merge into, or None -> mint a new canonical
    evidence: str           # 'primary-name' | 'multi-alias' | 'single-alias' | 'none'
    merged: bool            # True iff evidence was strong enough to merge
    ambiguous: AmbiguousMatch | None = None


def resolve(index: CanonicalIndex, name: str, aliases=(), *,
            min_alias_matches: int = 2,
            trusted: "set[tuple[str, str]] | None" = None) -> Resolution:
    """Resolve ``(name, aliases)`` against ``index``.

    Merge when EITHER the incoming primary name equals an existing canonical's primary
    name, OR at least ``min_alias_matches`` distinct keys are shared with one candidate.
    A single shared alias is NOT enough to merge (the okengine#39 over-merge guard); it
    is returned as ``evidence='single-alias'`` with an ``AmbiguousMatch`` so the caller
    can mint a new canonical and flag the situation for review.

    ``trusted`` (optional) is a set of ``(normalized_alias, slug)`` pairs from a curated
    co-reference mapping; a single shared alias present here IS trusted and merges.
    """
    incoming: list[str] = []
    seen: set[str] = set()
    for s in [name, *(aliases or ())]:
        k = normalize(s)
        if k and k not in seen:
            seen.add(k)
            incoming.append(k)
    if not incoming:
        return Resolution(None, "none", False)

    # Strong signal 1: incoming PRIMARY name == an existing canonical's PRIMARY name.
    primary_slug = index.primary.get(incoming[0])
    if primary_slug:
        return Resolution(primary_slug, "primary-name", True)

    # Tally distinct incoming keys hitting each candidate canonical.
    overlap: Counter = Counter()
    for k in incoming:
        for slug in index.keys.get(k, ()):
            overlap[slug] += 1
    if not overlap:
        return Resolution(None, "none", False)

    # Highest overlap wins; ties break to the lexicographically smaller slug (stable).
    best = min(overlap, key=lambda s: (-overlap[s], s))
    shared = [k for k in incoming if best in index.keys.get(k, set())]

    # Strong signal 2: >= min_alias_matches distinct shared keys with one candidate.
    if overlap[best] >= min_alias_matches:
        return Resolution(best, "multi-alias", True)

    # Trusted co-reference relaxation: a single shared alias the mapping vouches for.
    if trusted and any((k, best) in trusted for k in shared):
        return Resolution(best, "single-alias", True)

    # Weak: only a single shared alias token -> refuse to merge; report the ambiguity.
    return Resolution(None, "single-alias", False, AmbiguousMatch(best, shared))
