#!/usr/bin/env python3
"""id_lib — the engine-owned, versioned page-identity normalizer + grammar.

Composable okpacks need a *stable, type-independent* page identity so that
independent packs writing the same real-world thing converge on one page
(RFC docs/design/composable-okpacks.md §5a). This module is the SINGLE source of
that normalization — it MUST be byte-identical across packs and engine versions,
or convergence silently breaks. Changing the algorithm requires bumping
``NORM_VERSION`` and re-deriving (a migration), never an in-place tweak.

Identity grammar: an id is ``<scope>:<key>`` (both parts normalized, so neither
contains the ``:`` delimiter). Two *kinds* of id, distinguished by the scope:

  - **Authority id** — when the owning type declares an external authority
    (``id_authority``), the scope is the authority and the key is its local id:
    ``mitre:t1059``, ``cve:cve-2024-12345``. This is what makes packs converge.
  - **Minted slug** — otherwise, a slug derived from the page's natural key and
    **stamped once at creation, never recomputed**. The scope is chosen by the
    caller (the id-mint step) and frozen on the page. Slug ids are best-effort:
    collisions are flagged, never auto-merged (§5a).

`id` is immutable and **type-independent** — a page's `type` may be reclassified;
its id never changes.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

#: Bump when the normalization algorithm changes (forces a re-derivation/migration).
NORM_VERSION = 1

_MAX_KEY_LEN = 80          # cap a key; longer keys are truncated + hash-disambiguated
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MULTI_DASH = re.compile(r"-{2,}")


def _short_hash(raw: str, n: int) -> str:
    """A deterministic short hex digest of the ORIGINAL input — used to keep
    empty/truncated keys unique and collision-resistant."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def normalize_key(raw: str) -> str:
    """Normalize an arbitrary string into a stable, url/path-safe key.

    Deterministic and locale-independent: NFKD ascii-fold → lowercase →
    hyphenate non-alphanumeric runs → trim. Empty results (e.g. all-CJK input)
    and over-length results fall back to a hash of the *original* so the key is
    never empty and truncation can't silently collide. Never contains ``:``.
    """
    s = (raw or "").strip()
    # ascii-fold: decompose, drop combining marks, drop non-ascii.
    ascii_s = (unicodedata.normalize("NFKD", s)
               .encode("ascii", "ignore").decode("ascii").lower())
    slug = _MULTI_DASH.sub("-", _NON_ALNUM.sub("-", ascii_s)).strip("-")
    if not slug:
        return "x-" + _short_hash(s, 8)
    if len(slug) > _MAX_KEY_LEN:
        slug = slug[:_MAX_KEY_LEN].rstrip("-") + "-" + _short_hash(s, 6)
    return slug


def slug_identity(raw: str) -> str:
    """Return a separator-insensitive human identity for a filename slug.

    This is deliberately stricter than :func:`normalize_key`: ``agent-tesla``,
    ``agent_tesla``, and ``agenttesla`` are spelling variants of one subject.
    An all-digit result is excluded because punctuation can carry the identity
    of numeric indicators. This key detects collisions; it never authorizes an
    automatic merge.
    """
    folded = (
        unicodedata.normalize("NFKD", str(raw or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    identity = re.sub(r"[^a-z0-9]", "", folded)
    return "" if not identity or identity.isdigit() else identity


def qualified_namespace(wiki: Path, page: Path) -> str:
    """Return the schema-qualified namespace for a page below ``wiki``.

    A nested directory carrying ``schema.yaml`` is a pack/subdomain container,
    so it remains in the qualified key while the following directory is the
    governed namespace. Flat vaults retain their first path component.
    """
    try:
        parts = page.resolve().relative_to(wiki.resolve()).parts
    except (OSError, ValueError):
        return ""
    if not parts:
        return ""
    index = 0
    while (
        index < len(parts) - 1
        and (wiki.joinpath(*parts[: index + 1]) / "schema.yaml").is_file()
    ):
        index += 1
    return "/".join(parts[: index + 1])


def make_id(scope: str, key: str) -> str:
    """Build a normalized ``<scope>:<key>`` id (both parts normalized so the
    delimiter is unambiguous)."""
    return f"{normalize_key(scope)}:{normalize_key(key)}"


def authority_id(authority: str, local_id: str) -> str:
    """A convergent id from an external authority + its local id (e.g.
    ``authority_id('mitre', 'T1059')`` → ``mitre:t1059``)."""
    return make_id(authority, local_id)


def parse_id(page_id: str) -> tuple[str, str]:
    """Split an id into ``(scope, key)`` on the first ``:``. Returns
    ``("", page_id)`` if there is no delimiter."""
    s = str(page_id or "")
    scope, sep, key = s.partition(":")
    return (scope, key) if sep else ("", s)


def is_id(value: object) -> bool:
    """True iff `value` is a well-formed ``<scope>:<key>`` id (non-empty scope and
    key, normalized form — i.e. exactly what `make_id` would produce)."""
    if not isinstance(value, str) or ":" not in value:
        return False
    scope, key = parse_id(value)
    if not scope or not key:
        return False
    return make_id(scope, key) == value


def natural_key(fm: dict, fallback: str = "") -> str:
    """The string a minted-slug id is derived from: the page's human name
    (`title`, else `name`), else `fallback` (typically the filename stem)."""
    for f in ("title", "name"):
        v = fm.get(f) if isinstance(fm, dict) else None
        if isinstance(v, str) and v.strip():
            return v
    return fallback


def derive_id(*, authority: str | None = None, local_id: object = None,
             minted_scope: str, slug_source: str) -> tuple[str, str]:
    """Derive a page's id and its kind.

    If an `authority` and a non-empty `local_id` are supplied, return the
    convergent **authority** id (``mitre:t1059``). Otherwise **mint a slug** id
    scoped to `minted_scope` (the page's creation namespace, frozen) from
    `slug_source`. Returns ``(id, kind)`` with kind in ``{"authority", "slug"}``.

    The caller resolves `authority`/`local_id` from the governing type schema
    (`schema_lib.type_id_authority`) and reads the page's frontmatter; this stays
    a pure function of its inputs so it is trivially test-vectored.
    """
    if authority and local_id is not None and str(local_id).strip():
        return authority_id(authority, str(local_id)), "authority"
    return make_id(minted_scope, slug_source), "slug"
