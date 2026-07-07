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
_COMPOSED_CACHE: dict[str, tuple[float, dict]] = {}

# config/base-schema.yaml ships with the engine. It is resolved from one of two
# layouts: the REPO (scripts/cron/schema_lib.py -> ../../config) or the DEPLOYED cron
# staging dir (/opt/data/scripts/schema_lib.py -> ../config == /opt/data/config, where
# deploy-cron-scripts.sh stages it). First existing wins; OKENGINE_BASE_SCHEMA overrides.
_BASE_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "config" / "base-schema.yaml",   # repo: root/config
    Path(__file__).resolve().parents[1] / "config" / "base-schema.yaml",   # staged: /opt/data/config
)



# Fast frontmatter loader — libyaml (CSafeLoader) when built, ~7x the pure-Python loader. The hot
# path for full-vault audits over 10k+ pages (okengine#74). Falls back to the pure loader.
try:
    from yaml import CSafeLoader as _FAST_LOADER       # noqa: N814
except Exception:                                       # pragma: no cover
    from yaml import SafeLoader as _FAST_LOADER

def fast_load(text):
    """yaml.safe_load via libyaml when available. Returns the parsed object (dict/None/...)."""
    import yaml
    return yaml.load(text, _FAST_LOADER)

def _default_base() -> Path:
    for c in _BASE_CANDIDATES:
        if c.is_file():
            return c
    return _BASE_CANDIDATES[0]


def base_schema() -> dict:
    """The engine-owned base schema (config/base-schema.yaml): the universal core
    (types/namespaces/tiering) + cross-cutting optional fields + the global toggles
    (okf.required/should, strict_types). Returns {} if absent. Resolution: the
    OKENGINE_BASE_SCHEMA override, else the first existing repo/staged candidate."""
    p = Path(os.environ.get("OKENGINE_BASE_SCHEMA") or _default_base())
    k = str(p)
    if k not in _BASE_CACHE:
        try:
            _BASE_CACHE[k] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            _BASE_CACHE[k] = {}
    return _BASE_CACHE[k]


def _composed_artifact_at(dir_: Path) -> dict | None:
    """The generated composed schema (engine ⊕ pack ⊕ enabled extensions, okengine#133) at
    ``<dir_>/.okengine/composed-schema.yaml``, mtime-cached. None when absent. ``dir_`` is the
    GOVERNING schema's directory — the vault root OR a walk-up sub-domain (okengine#177) — so a
    sub-domain's own composed artifact governs its pages instead of always the root's."""
    p = Path(dir_) / ".okengine" / "composed-schema.yaml"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    k = str(p)
    hit = _COMPOSED_CACHE.get(k)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    data = data if isinstance(data, dict) else {}
    _COMPOSED_CACHE[k] = (mt, data)
    return data


def _composed_artifact(root: Path) -> dict | None:
    """Back-compat shim: the root vault's composed artifact."""
    return _composed_artifact_at(root)


def _governing_dir(root: Path, namespace: str = "") -> Path:
    """The DIRECTORY whose schema governs ``wiki/<namespace>/`` — walk UP from the namespace dir to
    the nearest ``schema.yaml`` (a sub-domain's, else the vault root's). Mirrors governing_schema's
    walk but returns the dir, so merged_schema can look for a composed artifact AT that same
    location (root: ``<root>/.okengine/…``; sub-domain: ``wiki/<subdomain>/.okengine/…``)."""
    cur = (root / "wiki" / namespace) if namespace else (root / "wiki")
    while True:
        if (cur / "schema.yaml").is_file():
            return cur
        if cur == root or cur.parent == cur:
            return root                       # root schema lives at <root>/schema.yaml (above wiki/)
        cur = cur.parent


def merged_schema(root: Path, namespace: str = "") -> dict:
    """The runtime governing schema a page validates against. Prefers the generated composed schema
    (engine ⊕ pack ⊕ enabled extensions, okengine#133) when present, so the write server's
    namespace/type/owner guards see extension-owned ids; otherwise the base⊕pack merge.

    Sub-domain aware (okengine#177): resolve the composed artifact at the GOVERNING location — walk
    up from ``wiki/<namespace>`` to the nearest schema.yaml and prefer a composed artifact beside it
    — not always the root's. A walk-up sub-domain page therefore resolves the sub-domain's schema
    (its custom types/authorities/owners) here, matching where the permission/shape guards resolve
    (schema_validator._find_schema, a page-path walk-up) — closing the split-brain where id/ownership
    saw root while permissions saw the sub-domain. For a single-pack vault the govdir is the root, so
    this is identical to the pre-#177 behavior. NOTE: compose_schema must NOT call this (it would
    re-fold an already-composed artifact) — it uses _merge_base_pack directly."""
    govdir = _governing_dir(root, namespace)
    # The ROOT pack's composed artifact lives at <root>/.okengine even when its schema.yaml sits at
    # <root>/wiki/schema.yaml (governing_schema accepts both); a walk-up SUB-DOMAIN's artifact lives
    # beside its own schema at wiki/<subdomain>/.okengine. So map the root-pack govdir (root or its
    # wiki/) to the root artifact, and only a genuine sub-domain reads its own.
    artdir = root if govdir in (root, root / "wiki") else govdir
    composed = _composed_artifact_at(artdir)
    if composed is not None:
        return composed
    return _merge_base_pack(root, namespace)


def _merge_base_pack(root: Path, namespace: str = "") -> dict:
    """The governing pack schema merged UNDER the engine base schema. The base owns the
    global toggles (`okf.required` is the union with `[type]` always present; `okf.should`
    and `strict_types` are engine-owned); the pack owns `types`/`partitioning`/`tier`/
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
    # Core OKF structure (okengine#90 P2): the engine base provides the universal types +
    # namespaces + tiering as DEFAULTS. A pack that still DECLARES one of these overrides the core
    # copy (so existing single-pack deploys are unchanged); a pack that OMITS it inherits the core
    # copy. This is what makes the core engine-owned without re-declaration per pack.
    if base.get("types") or pack.get("types"):
        out["types"] = {**(base.get("types") or {}), **(pack.get("types") or {})}
    b_part, p_part = base.get("partitioning") or {}, pack.get("partitioning") or {}
    if b_part or p_part:
        merged_part = {**b_part, **p_part}
        merged_part["namespaces"] = {**(b_part.get("namespaces") or {}), **(p_part.get("namespaces") or {})}
        out["partitioning"] = merged_part
    b_tier, p_tier = base.get("tier") or {}, pack.get("tier") or {}
    if b_tier or p_tier:
        merged_tier = {**b_tier, **p_tier}
        merged_tier["namespaces"] = {**(b_tier.get("namespaces") or {}), **(p_tier.get("namespaces") or {})}
        out["tier"] = merged_tier
    # Pack-level `extends`: a pack adds OPTIONAL domain fields onto an `extensible` core type
    # (the "core/spine extension" grammar, okengine#90 P2). Additive + optional only — a pack
    # CANNOT make a core type stricter (that would reject other packs' pages under composition).
    for tname, ext in (pack.get("extends") or {}).items():
        base_t = (out.get("types") or {}).get(tname)
        if not isinstance(base_t, dict) or not base_t.get("extensible"):
            continue
        t = dict(base_t)
        t["fields"] = dict(t.get("fields") or {})
        for fname, fdef in ((ext or {}).get("fields") or {}).items():
            t["fields"].setdefault(fname, fdef if isinstance(fdef, dict) else {})
        out["types"][tname] = t
    # Cross-cutting enums (okengine#90 P2): base vocabularies + the pack's, UNION-ing values per key
    # so a pack EXTENDS a base enum (adds domain values) rather than replacing it. field_enums merge
    # with the pack winning on a key.
    b_enums, p_enums = base.get("enums") or {}, pack.get("enums") or {}
    if b_enums or p_enums:
        merged_enums = {k: list(v) for k, v in b_enums.items()}
        for k, vals in p_enums.items():
            cur = merged_enums.get(k, [])
            merged_enums[k] = cur + [v for v in (vals or []) if v not in cur]
        out["enums"] = merged_enums
    b_fe, p_fe = base.get("field_enums") or {}, pack.get("field_enums") or {}
    if b_fe or p_fe:
        out["field_enums"] = {**b_fe, **p_fe}
    # Field SHAPES (okengine#196 generalized): base declares the universal list fields; a pack ADDS
    # its domain field shapes (pack wins on a key). The enforced write path reads this to coerce a
    # scalar written for a list field into a list, so no such page can enter the vault.
    b_fs, p_fs = base.get("field_shapes") or {}, pack.get("field_shapes") or {}
    if b_fs or p_fs:
        out["field_shapes"] = {**b_fs, **p_fs}
    # Conformance rules (okengine#158): engine FLOOR (base) ⊕ pack additions, additive + deduped by
    # `id` (pack can't drop an engine rule; a same-id pack rule overrides the floor copy). So the
    # audit + write-guard see one merged rule set, not the raw pack's.
    b_conf = (base.get("conformance") or {}).get("rules") or []
    p_conf = (pack.get("conformance") or {}).get("rules") or []
    if b_conf or p_conf:
        merged_rules, seen = [], set()
        for r in list(p_conf) + list(b_conf):    # pack first so a same-id pack rule wins
            rid = r.get("id") if isinstance(r, dict) else None
            if rid is None or rid not in seen:
                merged_rules.append(r)
                if rid is not None:
                    seen.add(rid)
        out["conformance"] = {"rules": merged_rules}
    return out


def list_fields(schema: dict) -> set:
    """Field names a schema declares with `list` shape (`field_shapes`). The enforced write path
    coerces a scalar string written for one of these into a list (okengine#196 generalized), so a
    list-consuming lane can't crash on a page authored as `aliases: A, B`."""
    shapes = (schema or {}).get("field_shapes") or {}
    return {k for k, v in shapes.items() if v == "list"}


def compose_schema(root: Path, fragments=None, namespace: str = "") -> tuple[dict, list[str]]:
    """N-way additive schema composition (okengine#90 P3 / #133).

    Folds the engine base ⊕ the pack schema (via merged_schema) ⊕ Σ(enabled-extension
    fragments) into one composed schema with an ``owners`` map, fail-loud on conflict.

    ``fragments`` is a list of ``(owner, fragment)`` where owner is ``"ext:<id>"`` and
    fragment uses the §5 grammar — ``owns`` (new namespaces/types) / ``extends``
    (additive optional fields or extensible-enum values). Returns ``(composed, errors)``;
    a non-empty errors list means the composition is unsound (do not generate/deploy).
    Back-compat: no fragments => merged_schema + an owners map for engine/pack.

    Owner tokens: ``engine`` (base globals), ``pack`` (pack-declared types/namespaces),
    ``ext:<id>`` (an extension's owned/extended ids).
    """
    import copy
    composed = copy.deepcopy(_merge_base_pack(root, namespace))   # base⊕pack only — never the artifact
    errors: list[str] = []

    composed.setdefault("types", {})
    composed.setdefault("enums", {})
    part = composed.setdefault("partitioning", {})
    part.setdefault("namespaces", {})

    # Core OKF structure (base-schema) is ENGINE-owned; everything else the merged schema carries
    # is the pack's domain (okengine#90 P2). So a pack that still OWNS a core id collides with
    # 'engine' — the signal to strip core from its `owns` and inherit/`extend` it instead.
    _base = base_schema()
    _core_types = set(_base.get("types") or {})
    _core_ns = set((_base.get("partitioning") or {}).get("namespaces") or {})
    owners = {"namespaces": {}, "types": {}, "fields": {}, "enum_values": {}}
    for ns in composed["partitioning"]["namespaces"]:
        owners["namespaces"][ns] = "engine" if ns in _core_ns else "pack"
    for t in composed["types"]:
        owners["types"][t] = "engine" if t in _core_types else "pack"

    for owner, frag in (fragments or []):
        if not isinstance(frag, dict):
            errors.append(f"{owner}: schema fragment is not a mapping")
            continue
        owns = frag.get("owns") or {}
        # --- Own: new namespaces ---
        for ns in (owns.get("namespaces") or []):
            if ns in owners["namespaces"]:
                errors.append(f"{owner}: namespace '{ns}' already owned by "
                              f"{owners['namespaces'][ns]} (own = new ids only)")
                continue
            composed["partitioning"]["namespaces"][ns] = {}
            owners["namespaces"][ns] = owner
        # --- Own: new types ---
        for tname, tdef in (owns.get("types") or {}).items():
            if tname in owners["types"]:
                errors.append(f"{owner}: type '{tname}' already owned by "
                              f"{owners['types'][tname]} (own = new ids only)")
                continue
            composed["types"][tname] = copy.deepcopy(tdef) if isinstance(tdef, dict) else {}
            owners["types"][tname] = owner
        # --- Extend: additive optional fields / enum values on an existing type|enum ---
        for tname, ext_def in (frag.get("extends") or {}).items():
            if not isinstance(ext_def, dict):
                errors.append(f"{owner}: extends.{tname} must be a mapping")
                continue
            # enum extension: {add: [values]} against an extensible enum
            if "add" in ext_def and tname in composed["enums"]:
                if not _is_extensible_enum(composed, tname):
                    errors.append(f"{owner}: enum '{tname}' is not extensible")
                    continue
                for val in (ext_def.get("add") or []):
                    key = f"{tname}.{val}"
                    if val in (composed["enums"].get(tname) or []):
                        errors.append(f"{owner}: enum value '{key}' already exists")
                        continue
                    composed["enums"].setdefault(tname, []).append(val)
                    owners["enum_values"][key] = owner
                continue
            # type field extension
            target = composed["types"].get(tname)
            if target is None:
                errors.append(f"{owner}: extends unknown type '{tname}'")
                continue
            if not target.get("extensible"):
                errors.append(f"{owner}: type '{tname}' is not marked extensible by its owner")
                continue
            for fname, fdef in (ext_def.get("fields") or {}).items():
                fkey = f"{tname}.{fname}"
                if fkey in owners["fields"] or fname in (target.get("fields") or {}):
                    errors.append(f"{owner}: field '{fkey}' already claimed")
                    continue
                if isinstance(fdef, dict) and fdef.get("optional") is False:
                    errors.append(f"{owner}: extended field '{fkey}' must be optional")
                    continue
                target.setdefault("fields", {})[fname] = copy.deepcopy(fdef) \
                    if isinstance(fdef, dict) else {}
                owners["fields"][fkey] = owner

    # Second pass: Reuse — every {type: ref, to: X} must resolve to a composed type.
    known = set(composed["types"])
    for tname, tdef in composed["types"].items():
        for fname, fdef in (tdef.get("fields") or {}).items():
            if isinstance(fdef, dict) and fdef.get("type") == "ref":
                tgt = fdef.get("to")
                if tgt and tgt not in known:
                    errors.append(f"type '{tname}.{fname}' references unknown type '{tgt}'")

    composed["owners"] = owners
    return composed, errors


def _is_extensible_enum(schema: dict, enum_name: str) -> bool:
    """An enum is extensible if its field_enums entry marks it so (reuses the existing
    `field_enums.<f>.extensible` marker, schema_validator)."""
    fe = schema.get("field_enums")
    if isinstance(fe, dict):
        for spec in fe.values():
            if isinstance(spec, dict) and spec.get("enum") == enum_name and spec.get("extensible"):
                return True
            if isinstance(spec, dict) and spec.get("extensible") and enum_name in (
                    (spec.get("by_type") or {}).values()):
                return True
    # also honor a direct enums.<name>.extensible convention if present
    return False


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


def reference_policy(schema: dict) -> dict:
    """Pack-declared recognition of REFERENCE-CATALOG pages — deterministically-imported
    reference data (a CVE feed, the MITRE ATT&CK catalog, a threat-group encyclopedia) as
    opposed to agent-SYNTHESIZED content. Such pages are link-target scaffolding: a catalog
    entry with no inbound links yet isn't content debt — it's waiting to be cited. So the
    content-health metrics (orphans, page-quality stubs) treat them separately, not as defects.

    Read from schema.yaml `reference_types` (page `type`s that are reference catalogs) and
    `reference_fields` (a frontmatter field whose mere presence marks a reference import — e.g.
    `mitre_id`, so ATT&CK records are caught regardless of type while a source-cited entity of
    the same type still counts). Top-level keys, alongside `depth_critical_types`. Empty (the
    default) ⇒ nothing is treated as reference, so behaviour is unchanged for non-opted-in packs."""
    rt = schema.get("reference_types")
    rf = schema.get("reference_fields")
    return {
        "types": {str(t) for t in rt} if isinstance(rt, list) else set(),
        "fields": {str(f) for f in rf} if isinstance(rf, list) else set(),
    }


def is_reference_page(fm: dict, refpol: dict) -> bool:
    """True iff this page is pack-declared reference-catalog data (see `reference_policy`):
    its `type` is a declared reference type, OR it carries a declared reference field."""
    if not isinstance(fm, dict) or not refpol:
        return False
    if str(fm.get("type", "")) in refpol.get("types", ()):
        return True
    return any(f in fm for f in refpol.get("fields", ()))


def conformance_rules(schema: dict) -> list[dict]:
    """Pack+engine CONTENT-conformance rules (okengine#158): mechanically-checkable rules beyond
    field presence/type, from the composed schema's top-level `conformance.rules` list. The
    conformance audit checks existing pages against these; the write-guard will enforce going
    forward. Empty (default) ⇒ no content rules. Each rule is a dict with at least `id` + `kind`."""
    c = schema.get("conformance")
    rules = c.get("rules") if isinstance(c, dict) else None
    return [r for r in rules if isinstance(r, dict) and r.get("id") and r.get("kind")] \
        if isinstance(rules, list) else []


def is_page_ref(entry) -> bool:
    """A frontmatter ref entry is a PAGE-PATH (graph edge) vs PROSE (links nothing). Page-path =
    contains '/' (e.g. `sources/2026/06/x`, `[[entities/a/foo]]`) or ends `.md`. Prose = free text
    like 'Cisco Talos disclosure'. The discriminator the `ref_fields` conformance rule uses."""
    s = str(entry).strip().strip("[]")
    return "/" in s or s.lower().endswith(".md")


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
