#!/usr/bin/env python3
"""importer_guard.py — the write-path guards for no_agent DIRECT WRITERS (okengine#237).

`no_agent` importers and deterministic mutation lanes write vault pages via direct
``path.write_text`` (the established convention: the enforced-MCP rule governs AGENT writes
only) — so NONE of the write path's guards fire on their output. Measured consequence
(2026-07-15): hours after a vault-wide tlp backfill, one NVD import run re-minted 3,898
lowercase pages; minutes after a traction backfill reached zero, a lane minted a fresh
out-of-enum value. Backfills are Sisyphean while producers bypass the boundary.

This module is the documented convention for every direct writer: call :func:`guard` on the
frontmatter dict before rendering the page. It applies the same semantics the enforced write
path applies to agent writes —

  - enum case-canonicalization (``tlp: clear`` -> ``CLEAR``; schema_validator, okengine#226)
  - strict-enum violation REPORTING (the write path rejects; an importer must not silently
    drop records, so violations come back as strings for the lane's reject log/dashboard)
  - list-shape coercion (scalar written for a schema list field -> one-element list, #196)
  - int-shape coercion/reporting (digit-string -> int; junk reported, #196)
  - item-contract enforcement for list-of-dict fields (evidence[].direction etc., #211/#217):
    strict object/required-key contracts, enum case coercion, scalar and container shapes

Enum/shape sources are the page's GOVERNING composed schema (base ⊕ pack ⊕ extensions), the
same resolution the write path uses. Fail-open like the runtime gate: a broken/missing schema
returns the fm untouched with no reports — an importer is never bricked by schema infra.

The item-check semantics here are a deliberate thin twin of
``write_server._item_shape_reject`` (baked, not importable from the cron surface without an
image dependency); the cross-implementation contract test
(tests/cron/test_importer_guard.py) pins the two to identical verdicts on shared fixtures —
the #218 pattern, so they cannot drift apart silently.

Usage (the convention)::

    from importer_guard import guard
    problems = guard(fm, vault=VAULT, namespace="entities")   # mutates fm in place
    if problems:
        reject_log.extend(f"{slug}: {p}" for p in problems)   # surface, never silently drop
        # lane decides: skip the record, or land it flagged for review

Env: none (pure library). Staged with the cron fleet; imports the baked schema_validator
(/opt/hermes/tools) exactly like schema_drift_lint does.
"""
from __future__ import annotations

import datetime as _dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema_lib  # noqa: E402

# tools.schema_validator ships in the Hermes image at /opt/hermes/tools/ (repo: tools/).
for _p in ("/opt/hermes", str(Path(__file__).resolve().parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from tools.schema_validator import canonicalize_enum_case, _enum_reject_reason  # noqa: E402

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_AUTHORITY_APPROVAL_FIELDS = frozenset({
    "review_state", "reviewed_by", "reviewed_at", "review_method", "review_policy",
    "authority", "authority_source_url", "authority_verified_fields",
})


def _revoke_invalid_authority_approval(fm: dict, problems: list[str]) -> None:
    """Never leave an importer-guard failure carrying contradictory policy approval metadata."""
    if not problems or not any(field in fm for field in _AUTHORITY_APPROVAL_FIELDS):
        return
    for field in _AUTHORITY_APPROVAL_FIELDS:
        fm.pop(field, None)
    fm["needs_review"] = True


def _coerce_shapes(fm: dict, schema: dict) -> list[str]:
    """List/int shape coercion with the write path's semantics; returns problems."""
    problems: list[str] = []
    for f in schema_lib.list_fields(schema):
        v = fm.get(f)
        if isinstance(v, str) and v.strip():
            fm[f] = [s.strip() for s in v.split(",")] if "," in v else [v.strip()]
    for f in schema_lib.int_fields(schema):
        v = fm.get(f)
        if v is None or (isinstance(v, int) and not isinstance(v, bool)):
            continue
        if isinstance(v, str) and v.strip().isdigit():
            fm[f] = int(v.strip())
            continue
        problems.append(f"field `{f}` must be an integer count — got {type(v).__name__}: {str(v)[:60]!r}")
    return problems


def _guard_items(fm: dict, schema: dict) -> list[str]:
    """Item contracts (#211) with the write path's semantics: case-variant of exactly one
    sanctioned value coerces; numeric strings coerce; junk is reported. Thin twin of
    write_server._item_shape_reject — pinned by the cross-implementation contract test."""
    problems: list[str] = []
    for field, keyrules in schema_lib.item_rules(schema).items():
        items = fm.get(field)
        if not isinstance(items, list):
            continue
        item_spec = keyrules.get("_item") or {}
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                if item_spec.get("shape") == "dict":
                    problems.append(f"`{field}[{i}]` must be an object — got {type(item).__name__}")
                continue
            missing = [key for key in sorted(item_spec.get("required") or set())
                       if key not in item or item[key] is None
                       or (isinstance(item[key], str) and not item[key].strip())]
            if missing:
                problems.append(f"`{field}[{i}]` is missing required item field(s): {', '.join(missing)}")
            for key, rule in keyrules.items():
                if key == "_item":
                    continue
                v = item.get(key)
                if v is None:
                    continue
                allowed = rule.get("enum")
                if allowed is not None:
                    if isinstance(v, str) and v not in allowed:
                        ci = [a for a in allowed if a.casefold() == v.casefold()]
                        if len(ci) == 1:
                            item[key] = ci[0]
                            continue
                    if not isinstance(v, str) or item.get(key) not in allowed:
                        problems.append(
                            f"`{field}[{i}].{key}` = {str(v)[:60]!r} is not in the sanctioned "
                            f"vocabulary ({', '.join(sorted(allowed))})")
                    continue
                shape = rule.get("shape")
                if shape == "number":
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        try:
                            item[key] = float(str(v).strip())
                        except (ValueError, TypeError):
                            problems.append(
                                f"`{field}[{i}].{key}` must be a number — got "
                                f"{type(v).__name__}: {str(v)[:60]!r}")
                elif shape == "date":
                    if isinstance(v, (_dt.date, _dt.datetime)):
                        continue
                    if not (isinstance(v, str) and _ISO_DATE_RE.match(v.strip())):
                        problems.append(
                            f"`{field}[{i}].{key}` must be an ISO date (YYYY-MM-DD) — got "
                            f"{str(v)[:60]!r}")
                elif shape == "str":
                    if not isinstance(v, str):
                        problems.append(
                            f"`{field}[{i}].{key}` must be a string — got {type(v).__name__}")
                elif shape == "bool" and not isinstance(v, bool):
                    problems.append(
                        f"`{field}[{i}].{key}` must be a boolean — got {type(v).__name__}")
                elif shape == "list" and not isinstance(v, list):
                    problems.append(
                        f"`{field}[{i}].{key}` must be a list — got {type(v).__name__}")
                elif shape == "dict" and not isinstance(v, dict):
                    problems.append(
                        f"`{field}[{i}].{key}` must be an object — got {type(v).__name__}")
    return problems


def guard(fm: dict, *, vault: Path, namespace: str = "") -> list[str]:
    """Apply the write-path guards to a direct-writer's frontmatter IN PLACE.

    Coerces everything unambiguous (enum casing, list/int shapes, item numbers/casing) and
    returns a list of human-readable problems it could NOT fix — strict-enum violations,
    junk shapes. An empty list = the page is boundary-clean. The caller surfaces problems
    (reject log, dashboard, needs_review flag) — never silently drops them.

    Fail-open: schema resolution errors return [] with fm untouched (the runtime gate's
    philosophy — importer infrastructure must not be bricked by a schema problem)."""
    if not isinstance(fm, dict):
        return []
    try:
        schema = schema_lib.merged_schema(Path(vault), namespace)
    except Exception:
        return []
    problems: list[str] = []
    try:
        raw_typ = str(fm.get("type") or "").strip()
        typ = schema_lib.canonical_type(schema, raw_typ)
        if typ != raw_typ:
            fm["type"] = typ
        declared = schema_lib.canonical_types(schema)
        if schema.get("strict_types") and typ not in declared:
            problems.append(f"unknown type '{raw_typ}' — not in schema.yaml taxonomy")
            _revoke_invalid_authority_approval(fm, problems)
            return problems
        canonicalize_enum_case(schema, typ, fm)
        problems += _coerce_shapes(fm, schema)
        problems += _guard_items(fm, schema)
        enum_reason = _enum_reject_reason(schema, typ, fm)
        if enum_reason:
            problems.append(enum_reason)
    except Exception:
        pass
    _revoke_invalid_authority_approval(fm, problems)
    return problems
