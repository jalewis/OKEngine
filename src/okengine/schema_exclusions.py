"""Canonical schema ``exclude`` grammar shared by every engine surface."""

from __future__ import annotations

import re
from pathlib import Path

_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GLOB_META = re.compile(r"[*?[]")

# Engine-generated/operational surfaces are walked under separate contracts.
# They must never be claimed as knowledge namespaces by a pack or extension:
# the corpus index, health, lint, orphan, and statistics lanes deliberately omit
# them even when they are not repeated in a pack's schema ``exclude`` list.
RESERVED_DERIVED_NAMESPACES = frozenset({"dashboards", "operational"})


def excluded_namespace(value: object) -> str:
    """Return one top-level namespace or reject an ambiguous path/pattern."""
    raw = str(value).strip().strip("/")
    if raw.startswith("wiki/"):
        raw = raw[5:]
    name = raw.strip("/")
    invalid = (not name, "/" in name, "\\" in name, not _NAMESPACE.fullmatch(name))
    if any(invalid):
        raise ValueError(
            f"{value!r} is not a namespace exclusion; use a bare namespace or wiki/<namespace>/"
        )
    return name


def excluded_namespaces_from_schema(schema: dict) -> set[str]:
    values = schema.get("exclude") or []
    if not isinstance(values, list):
        raise ValueError("schema exclude must be a list of namespaces")
    namespaces = set()
    for value in values:
        if _GLOB_META.search(str(value)):
            continue
        namespaces.add(excluded_namespace(value))
    return namespaces


def exclusion_globs_from_schema(schema: dict) -> list[str]:
    values = schema.get("exclude") or []
    if not isinstance(values, list):
        raise ValueError("schema exclude must be a list")
    out = []
    for value in values:
        raw = str(value).strip()
        if not _GLOB_META.search(raw):
            excluded_namespace(value)
            continue
        # ``raw`` cannot be empty here: reaching this branch requires glob meta.
        unsafe = ("\\" in raw, ".." in Path(raw).parts)
        if any(unsafe):
            raise ValueError(f"unsafe schema exclude glob: {value!r}")
        out.append(raw)
    return out
