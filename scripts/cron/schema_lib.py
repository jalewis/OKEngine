#!/usr/bin/env python3
"""schema_lib — shared, domain-agnostic accessors over the governing schema.yaml.

The engine ships ZERO domain knowledge: page types, type-canonicalization aliases,
protected fields, and the knowledge-namespace layout are all PACK inputs declared
in `schema.yaml`. Engine cron scripts must read them through these helpers instead
of hardcoding sec/intel taxonomies. Every accessor degrades safely to a generic
default when the pack hasn't declared the optional block, so a minimal pack (or a
packless test) still runs without domain assumptions.

Schema keys consumed (all optional except `types`):
  types:                 {<type>: {required: [...]}}      # the canonical type set
  type_aliases:          {<old-or-alias>: <canonical>}    # pack-supplied remap (default {})
  protected_fields:      [<field>, ...]                   # never silently dropped (default [])
  partitioning.namespaces: {<ns>: {...}}                  # the knowledge namespaces
  exclude:               [wiki/<ns>/, ...]                # non-knowledge / derived dirs

The engine also ships a BASE schema (config/base-schema.yaml) merged under every
pack schema via `merged_schema()`: the universal field set + the engine-owned
global toggles (`okf.required`/`okf.should`, `strict_types`). Packs declare only
their domain types on top; the base is not pack-settable under composition.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import yaml

_SCHEMA_CACHE: dict[str, dict] = {}
_BASE_CACHE: dict[str, dict] = {}

# config/base-schema.yaml ships with the engine. Resolve repo-relative by default
# (scripts/cron/schema_lib.py -> repo root), overridable for deployed layouts.
_DEFAULT_BASE = Path(__file__).resolve().parents[2] / "config" / "base-schema.yaml"


def base_schema() -> dict:
    """The engine-owned base schema (config/base-schema.yaml): the universal field
    set + the global toggles (okf.required/should, strict_types) that live in the
    engine, not in packs. Returns {} if absent. Path override: OKENGINE_BASE_SCHEMA."""
    p = Path(os.environ.get("OKENGINE_BASE_SCHEMA") or _DEFAULT_BASE)
    k = str(p)
    if k not in _BASE_CACHE:
        try:
            _BASE_CACHE[k] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            _BASE_CACHE[k] = {}
    return _BASE_CACHE[k]


def merged_schema(root: Path, namespace: str = "") -> dict:
    """The governing pack schema merged UNDER the engine base schema — the runtime
    contract a page validates against. The base owns the global toggles
    (`okf.required` is the union with `[type]` always present; `okf.should` and
    `strict_types` are engine-owned); the pack owns `types`/`partitioning`/`tier`/
    `permissions` and any extra keys, which pass through unchanged."""
    base = base_schema()
    pack = governing_schema(root, namespace)
    out = dict(pack)  # pack owns most keys; base overrides only the globals below
    b_okf = base.get("okf") if isinstance(base.get("okf"), dict) else {}
    p_okf = pack.get("okf") if isinstance(pack.get("okf"), dict) else {}
    required = sorted(set(b_okf.get("required") or ["type"]) | set(p_okf.get("required") or []))
    okf = {"required": required}
    if b_okf.get("should"):
        okf["should"] = list(b_okf["should"])
    out["okf"] = okf
    # strict_types is ENGINE-OWNED: the base is authoritative and a pack-level
    # value is ignored. Under composition a pack must not be able to loosen or
    # tighten the global type taxonomy (one pack's `strict_types: true` would
    # reject another pack's valid types). `framework validate` WARNs a pack that
    # sets it. Default False keeps the format open/extensible (OKF passes extras).
    out["strict_types"] = base.get("strict_types", False)
    common = set(base.get("common_optional") or []) | set(pack.get("common_optional") or [])
    if common:
        out["common_optional"] = sorted(common)
    return out


def governing_schema(root: Path, namespace: str = "") -> dict:
    """The schema.yaml governing wiki/<namespace>/ — walk UP from the namespace dir
    (a sub-domain's own schema.yaml, else the vault root's). `namespace=""` resolves
    the vault-root schema. Returns {} if none found. Cached by path."""
    cur = (root / "wiki" / namespace) if namespace else (root / "wiki")
    while True:
        sp = cur / "schema.yaml"
        if sp.is_file():
            k = str(sp)
            if k not in _SCHEMA_CACHE:
                try:
                    _SCHEMA_CACHE[k] = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
                except Exception:
                    _SCHEMA_CACHE[k] = {}
            return _SCHEMA_CACHE[k]
        if cur == root or cur.parent == cur:
            return {}
        cur = cur.parent


def canonical_types(schema: dict) -> set[str]:
    """The pack's declared page types (schema.yaml `types:` keys). Empty set if
    undeclared — callers should treat 'no declared types' as 'accept anything',
    never as 'fall back to a built-in domain taxonomy'."""
    types = schema.get("types")
    return set(types.keys()) if isinstance(types, dict) else set()


def type_aliases(schema: dict) -> dict[str, str]:
    """Pack-supplied {alias -> canonical} type remap. Default {} (no remapping)."""
    aliases = schema.get("type_aliases")
    return {str(k): str(v) for k, v in aliases.items()} if isinstance(aliases, dict) else {}


def type_id_authority(schema: dict, type_name: str) -> tuple[str | None, str]:
    """Identity binding for a page type: ``(authority, id_field)``.

    A type that maps to an external canonical id declares it in the schema, e.g.::

        types:
          attack-pattern: {required: [type], id_authority: mitre, id_field: technique_id}

    `authority` is the id scope (`mitre`) and `id_field` is the frontmatter field
    holding the local id (default `external_id`). Returns ``(None, "external_id")``
    when the type declares no authority — pages of that type get a minted slug id."""
    t = (schema.get("types") or {}).get(type_name)
    if not isinstance(t, dict):
        return (None, "external_id")
    auth = t.get("id_authority")
    field = t.get("id_field") or "external_id"
    return (str(auth) if auth else None, str(field))


def type_owner(schema: dict, type_name: str) -> str | None:
    """The pack that OWNS a type and its pages (``types.<t>.owner``). None when
    undeclared — converge-on-write then enforces no ownership (single-pack
    back-compat). Full pack-metadata ownership arrives with composition (P3)."""
    t = (schema.get("types") or {}).get(type_name)
    return str(t["owner"]) if isinstance(t, dict) and t.get("owner") else None


def field_owners(schema: dict, type_name: str) -> dict[str, str]:
    """Per-field ownership grants for a type (``types.<t>.field_owners``): a map
    of frontmatter field -> the non-owner pack allowed to maintain it (e.g. a hunt
    pack owning `detection` on an attack-pattern). Default {}."""
    t = (schema.get("types") or {}).get(type_name)
    fo = t.get("field_owners") if isinstance(t, dict) else None
    return {str(k): str(v) for k, v in fo.items()} if isinstance(fo, dict) else {}


def protected_fields(schema: dict) -> set[str]:
    """Frontmatter fields a pack marks as curated / never-silently-dropped.
    Default empty — the engine guards nothing domain-specific on its own."""
    pf = schema.get("protected_fields")
    return {str(x) for x in pf} if isinstance(pf, (list, tuple, set)) else set()


def knowledge_namespaces(schema: dict) -> set[str]:
    """Top-level namespaces that hold knowledge pages (schema.yaml
    `partitioning.namespaces` keys). Empty set if undeclared."""
    ns = (schema.get("partitioning") or {}).get("namespaces")
    return set(ns.keys()) if isinstance(ns, dict) else set()


def excluded_dirs(schema: dict) -> set[str]:
    """Namespace names the pack excludes from conformance/indexing (derived from
    schema.yaml `exclude:` paths like `wiki/operational/`). Default empty."""
    out: set[str] = set()
    for p in schema.get("exclude") or []:
        seg = str(p).strip("/").split("/")
        if seg and seg[0] == "wiki" and len(seg) > 1:
            out.add(seg[1])
        elif seg:
            out.add(seg[-1])
    return out
