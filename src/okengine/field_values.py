"""Type-strict frontmatter value comparison, shared by every write-path "unchanged" check.

Python equality is the wrong question for an "is this field unchanged?" guard: ``0 == False``
and ``1 == True``, so a write that stores ``actor_identity_validated: 0`` over ``false`` compared
as "unchanged" and passed a protected-field check (okengine#983 QA), although every consumer that
tests ``is False`` now reads a different value. Two values are the same here only when their
canonical YAML forms are identical: ``0`` and ``false`` differ, ``"true"`` and ``true`` differ, and
a verbatim read-modify-write echo (same types, same values, any mapping order) is still equal.
"""
from __future__ import annotations

from typing import Any

import yaml


def canonical(value: Any) -> str:
    """The value's canonical YAML text: key-sorted, so mapping order never counts as a change."""
    try:
        # Formatting options would apply identically to both sides, so none are set: only the
        # key order is normalized.
        return yaml.safe_dump(value, sort_keys=True)
    except yaml.YAMLError:
        # Not representable as YAML (never produced by safe_load): compare type and repr instead.
        return f"!{type(value).__qualname__} {value!r}"


def same_value(left: Any, right: Any) -> bool:
    """True when the two frontmatter values are the same value of the same type."""
    return canonical(left) == canonical(right)
