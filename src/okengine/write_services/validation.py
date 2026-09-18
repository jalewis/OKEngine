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
import output_contract_enforce as _output_contract

import id_lib, schema_lib, id_index, converge, okf_migrate
_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")


def _partitioned_create_path(p: Path, fm: dict) -> Path:
    """Return the schema-canonical destination for a newly created page.

    ``_safe`` historically normalized only ``entities``.  That left the same
    enforced MCP boundary able to mint flat ``concepts/<slug>`` pages and
    year-only ``sources/YYYY/<slug>`` pages beside their canonical shards.  Use
    the exact helper shared by importers, reshelve, and collision cleanup so all
    namespaces follow one partition contract.  Existing pages remain where
    they are (``write_key`` deliberately converges on them); migration owns
    moving stale layouts.
    """
    if not _CONVERGE_OK:
        return p
    try:
        # SUB-DOMAIN AWARE (walk-up multipack #173): a page at wiki/<subdomain>/entities/<slug> lives
        # in namespace 'entities', not '<subdomain>'. Using rel.parts[0] read the CONTAINER as the
        # namespace, so okf_migrate.is_partitioned('<subdomain>') found no partition config and the
        # page was written FLAT — while the reshelve drain (reshelve.py walks every sub-domain's
        # schema and reshards <subdomain>/entities) then sharded it, re-opening the #54 duplicate-
        # canonical ping-pong for every co-installed vault (invariant-audit #351). write_key preserves
        # the full prefix, so the page shards WITHIN its sub-domain. A flat vault has no container, so
        # _qualified_namespace == rel.parts[0] — byte-identical for every single-pack deployment.
        namespace = _qualified_namespace(p)
        vault = Path(os.environ.get("WIKI_PATH") or str(VAULT))
        if not namespace or not okf_migrate.is_partitioned(vault, namespace):
            return p
        key = okf_migrate.write_key(vault, namespace, p.stem, fm)
        return (_wiki() / f"{key}.md").resolve()
    except (OSError, ValueError, IndexError):
        return p


def _strip_wikilink(s):
    """'[[concepts/x]]' -> 'concepts/x' (drops any #anchor / |display); a non-wikilink value is
    returned unchanged."""
    if isinstance(s, str):
        m = _WIKILINK_FULL.match(s.strip())
        if m:
            return m.group(1).strip()
    return s


def _looks_like_ref_list(v) -> bool:
    """A list that needs canonicalizing: it either contains nested lists (the shape YAML produces
    when a bare `[[x]]` wikilink is used as a value — `[[x]]` parses as a nested flow sequence) or
    holds `[[..]]` wikilink strings."""
    return isinstance(v, list) and (
        any(isinstance(x, list) for x in v)
        or any(isinstance(x, str) and x.strip().startswith("[[") for x in v)
    )


def _flatten_strip(v) -> list:
    """Flatten arbitrarily-nested lists (the `[[..]]` YAML mangling) into a flat list of plain
    wiki-relative path strings, wikilink-stripped, dropping blanks + dups (order-preserving)."""
    out: list = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
            return
        s = _strip_wikilink(x)
        if isinstance(s, str):
            s = s.strip()
        if s and s not in out:
            out.append(s)

    walk(v)
    return out


def _base_list_fields() -> frozenset:
    """The universal list fields, read once from base-schema `field_shapes` (schema_lib may be
    absent, or the base may predate field_shapes — fall back to the known set either way)."""
    global _base_list_fields_cache
    if _base_list_fields_cache is None:
        try:
            lf = schema_lib.list_fields(schema_lib.base_schema())
        except Exception:
            lf = set()
        _base_list_fields_cache = frozenset(lf) or _FALLBACK_LIST_FIELDS
    return _base_list_fields_cache


def _list_fields_for(page_path) -> set:
    """List fields governing a page: the universal base set ∪ any the page's COMPOSED schema declares
    (so a pack's domain list field is honoured too). Base-only fallback if the schema can't load."""
    lf = set(_base_list_fields())
    if page_path is not None:
        try:
            lf |= schema_lib.list_fields(_governing(page_path))
        except Exception:
            pass
    return lf


def _base_int_fields() -> frozenset:
    global _base_int_fields_cache
    if _base_int_fields_cache is None:
        try:
            _base_int_fields_cache = frozenset(schema_lib.int_fields(schema_lib.base_schema()))
        except Exception:
            _base_int_fields_cache = frozenset()
    return _base_int_fields_cache


def _int_fields_for(page_path) -> set:
    fields = set(_base_int_fields())
    if page_path is not None:
        try:
            fields |= schema_lib.int_fields(_governing(page_path))
        except Exception:
            pass
    return fields


def _enum_case_coerce(p, fm) -> None:
    """Case-canonicalize enum values IN PLACE before validation (okengine#226): `tlp: clear`
    lands as `CLEAR` instead of rejecting — a case-insensitive match to exactly one allowed
    value is unambiguous intent (same philosophy as the digit-string int coercion). Genuinely
    unknown values still reject downstream (schema_reject_reason/_enum_reject_reason).
    Never raises: a broken schema must not brick a write (the runtime gate is fail-open)."""
    if not isinstance(fm, dict):
        return
    try:
        canonicalize_enum_case(_governing(p), str(fm.get("type") or ""), fm)
    except Exception:
        pass


def _base_item_rules() -> dict:
    global _base_item_rules_cache
    if _base_item_rules_cache is None:
        try:
            _base_item_rules_cache = schema_lib.item_rules(schema_lib.base_schema())
        except Exception:
            _base_item_rules_cache = {}
    return _base_item_rules_cache


def _item_rules_for(page_path) -> dict:
    rules = dict(_base_item_rules())
    if page_path is not None:
        try:
            rules.update(schema_lib.item_rules(_governing(page_path)))
        except Exception:
            pass
    return rules


def _item_shape_reject(p, fm) -> Optional[str]:
    """Validate schema-declared ITEM contracts on list-of-dict fields; None = clean.
    Coerces an unambiguous numeric string in place; rejects out-of-enum / wrong-shape values with
    the exact location named. Non-dict items (legacy prose strings) and absent keys pass — item
    requiredness is not this guard's job, vocabulary/shape integrity is."""
    if not isinstance(fm, dict):
        return None
    for field, keyrules in _item_rules_for(p).items():
        items = fm.get(field)
        if not isinstance(items, list):
            continue
        item_spec = keyrules.get("_item") or {}
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                if item_spec.get("shape") == "dict":
                    return (
                        f"`{field}[{i}]` must be an object — got "
                        f"{type(item).__name__}: {str(item)[:60]!r}"
                    )
                continue
            missing = [
                key
                for key in sorted(item_spec.get("required") or set())
                if key not in item
                or item[key] is None
                or (isinstance(item[key], str) and not item[key].strip())
            ]
            if missing:
                return f"`{field}[{i}]` is missing required item field(s): {', '.join(missing)}"
            for key, rule in keyrules.items():
                if key == "_item":
                    continue
                v = item.get(key)
                if v is None:
                    continue
                allowed = rule.get("enum")
                if allowed is not None:
                    if isinstance(v, str) and v not in allowed:
                        # case-variant of exactly one allowed value -> coerce (#226)
                        ci = [a for a in allowed if a.casefold() == v.casefold()]
                        if len(ci) == 1:
                            item[key] = ci[0]
                            continue
                    if not isinstance(v, str) or v not in allowed:
                        return (
                            f"`{field}[{i}].{key}` = {str(v)[:60]!r} is not in the sanctioned "
                            f"vocabulary ({', '.join(sorted(allowed))}). Resubmit the complete "
                            f"list using only those values."
                        )
                    continue
                shape = rule.get("shape")
                if shape == "number":
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        coerced = None
                        if isinstance(v, str):
                            try:
                                coerced = float(v.strip())
                            except ValueError:
                                pass
                        if coerced is None:
                            return (
                                f"`{field}[{i}].{key}` must be a number — got "
                                f"{type(v).__name__}: {str(v)[:60]!r}"
                            )
                        item[key] = coerced  # unambiguous intent — coerce
                elif shape == "date":
                    if isinstance(v, (datetime.date, datetime.datetime)):
                        continue  # yaml parses bare ISO dates natively
                    if not (isinstance(v, str) and _ISO_DATE_RE.match(v.strip())):
                        return (
                            f"`{field}[{i}].{key}` must be an ISO date (YYYY-MM-DD) — got "
                            f"{str(v)[:60]!r}"
                        )
                elif shape == "str":
                    if not isinstance(v, str):
                        return f"`{field}[{i}].{key}` must be a string — got {type(v).__name__}"
                elif shape == "bool":
                    if not isinstance(v, bool):
                        return f"`{field}[{i}].{key}` must be a boolean — got {type(v).__name__}"
                elif shape == "list":
                    if not isinstance(v, list):
                        return f"`{field}[{i}].{key}` must be a list — got {type(v).__name__}"
                elif shape == "dict":
                    if not isinstance(v, dict):
                        return f"`{field}[{i}].{key}` must be an object — got {type(v).__name__}"
    return None


def _int_shape_reject(p, fm) -> Optional[str]:
    """Coerce digit-strings in place; return a reject reason when a schema-declared int field holds
    anything else (list/path/prose/bool). None = clean."""
    if not isinstance(fm, dict):
        return None
    for k in _int_fields_for(p):
        v = fm.get(k)
        if v is None or (isinstance(v, int) and not isinstance(v, bool)):
            continue
        if isinstance(v, str) and v.strip().isdigit():
            fm[k] = int(v.strip())  # unambiguous intent — coerce
            continue
        return (
            f"field `{k}` must be an integer count (it is machine-computed by a metrics lane) "
            f"— got {type(v).__name__}: {str(v)[:80]!r}. Drop the field; do not hand-author it."
        )
    return None


def _normalize_refs(fm: dict, list_fields=frozenset()) -> dict:
    """Canonicalize frontmatter values at the single enforced-write chokepoint (so every extension's
    writes are fixed at once). Three coercions:
      - a schema-declared list field written as a scalar string -> a list (okengine#196);
      - a bare `[[x]]` wikilink string -> the plain path `x` (agents write `[[wikilinks]]`, but in a
        frontmatter VALUE that mangles — `field_mapped: [[c/x]]` -> `[[ "c/x" ]]`);
      - a list that mangled into nested lists, or holds `[[..]]` strings, -> a flat list of paths.
    Plain strings and plain lists are left untouched."""
    if not isinstance(fm, dict):
        return fm
    for k, v in list(fm.items()):
        if k in list_fields and isinstance(v, str):
            fm[k] = [
                s.strip() for s in v.split(",") if s.strip()
            ]  # scalar list-field -> list (#196)
        elif isinstance(v, str):
            fm[k] = _strip_wikilink(v)
        elif _looks_like_ref_list(v):
            fm[k] = _flatten_strip(v)
    return fm


def _coerce_fm(frontmatter_yaml: Union[str, dict, None], page_path=None) -> Optional[dict]:
    """Accept a YAML string OR a dict; return a dict (or None to signal a parse error vs an
    empty/absent value, which returns {}). Frontmatter values are canonicalized via _normalize_refs
    (wikilink -> plain path; scalar -> list for the page's schema-declared list fields). `page_path`
    selects the governing schema's list fields; None falls back to the universal base set."""
    list_fields = _list_fields_for(page_path)
    if frontmatter_yaml is None:
        return {}
    if isinstance(frontmatter_yaml, dict):
        return _normalize_refs(dict(frontmatter_yaml), list_fields)
    try:
        loaded = yaml.safe_load(frontmatter_yaml)
    except Exception:
        return None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        return None
    return _normalize_refs(loaded, list_fields)


def _compose(fm: dict, body: str) -> str:
    """Render frontmatter + body into a page, preserving key order."""
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    body = body or ""
    return f"---\n{fm_text}\n---\n{body}"


def _read_page(p: Path) -> tuple[dict, str]:
    """Split an existing page into (frontmatter dict, body). Empty fm on no match."""
    text = p.read_text(encoding="utf-8")
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    return fm, body
