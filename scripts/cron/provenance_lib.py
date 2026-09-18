#!/usr/bin/env python3
"""The value vocabulary a vault actually declares — resolved once, for every lane that asks.

`field_enums` + `enums` is the schema's value contract, and it is consulted from three very
different places: the enforced write path REJECTS a page that violates a closed enum
(`tools/schema_validator._enum_reject_reason`), `corpus_audit` REPORTS violations already
sitting in the corpus, and `repair_carried_provenance` REFUSES to carry a raw value the write
path would have rejected. Those three had two implementations between them, and the
difference mattered: one dropped the `extensible` flag — the single bit that decides whether
an unrecognised value is a schema violation or a pack legitimately growing its vocabulary.

An invariant resolved two ways is resolved no ways, so it is resolved here.

GRAMMAR. A rule is `{enum: <name>, extensible: <bool>}` referencing `schema['enums'][<name>]`
(base ∪ pack, merged by schema_lib), or a bare list used as a direct allowed-list (always
closed). `extensible: true` means a pack MAY add values at write time, so an unrecognised
value there is novel vocabulary rather than drift. A field with no rule is unconstrained.

A NOTE ON `extensible`. The engine's base schema ships `source_kind` extensible, but several
packs re-declare it as `{enum: source_kind}` with no flag, which CLOSES it after composition.
So whether a given value is legal is a property of the composed schema of the vault in hand,
never of the engine's default — resolve it, do not assume it.
"""
from __future__ import annotations

# Classification results for a single field value.
CONFORMANT = None      # declared, or the field is unconstrained
DRIFT = "drift"        # closed enum: the write path would reject this value
NOVEL = "novel"        # extensible enum: legal, but silent growth is pre-drift


def enum_rules(schema: dict) -> dict[str, tuple[set[str], bool]]:
    """field -> (allowed values, extensible), with the write path's semantics."""
    enums = schema.get("enums") or {}
    out: dict[str, tuple[set[str], bool]] = {}
    for field, rule in (schema.get("field_enums") or {}).items():
        if isinstance(rule, list):
            out[field] = ({str(v) for v in rule}, False)
        elif isinstance(rule, dict):
            allowed = enums.get(rule.get("enum"))
            if isinstance(allowed, list):
                out[field] = ({str(v) for v in allowed}, bool(rule.get("extensible")))
    return out


def closed_enums(schema: dict) -> dict[str, set[str]]:
    """field -> allowed values, for CLOSED enums only.

    The set a writer must not step outside. An extensible field is deliberately absent: a lane
    that refused to write outside an extensible enum would be enforcing a rule the schema
    explicitly declined to make.
    """
    return {field: allowed for field, (allowed, ext) in enum_rules(schema).items() if not ext}


def classify_value(field: str, value, rules: dict[str, tuple[set[str], bool]]):
    """CONFORMANT / DRIFT / NOVEL for one field value against resolved rules.

    Only STRING values are classified. A field the schema does not constrain, one that is
    absent, and one holding a list or a number are all somebody else's check — an enum rule
    describes a scalar vocabulary, and stringifying a list here would report `['a', 'b']` as an
    out-of-enum value on every page that carries one.

    An empty string is NOT exempt: the key is present and the ingest wrote no value into it,
    which is a lane producing a blank where a vocabulary was required.
    """
    rule = rules.get(field)
    if rule is None or not isinstance(value, str):
        return CONFORMANT
    allowed, extensible = rule
    if value in allowed:
        return CONFORMANT
    return NOVEL if extensible else DRIFT
