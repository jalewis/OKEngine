"""P0 regression: the engine base schema merges under a pack schema correctly.

The engine owns config/base-schema.yaml; `merged_schema()` layers it under the
governing pack schema. The base owns the global toggles (okf.required/should,
strict_types); the pack owns types/partitioning/etc.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "cron" / "schema_lib.py"


def _load():
    spec = importlib.util.spec_from_file_location("schema_lib", LIB)
    m = importlib.util.module_from_spec(spec)
    sys.modules["schema_lib"] = m
    spec.loader.exec_module(m)
    m._BASE_CACHE.clear()
    m._SCHEMA_CACHE.clear()
    return m


def _vault(tmp_path: Path, schema_yaml: str | None) -> Path:
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    if schema_yaml is not None:
        (tmp_path / "wiki" / "schema.yaml").write_text(schema_yaml)
    return tmp_path


def test_base_schema_ships_globals():
    m = _load()
    base = m.base_schema()
    assert base.get("okf", {}).get("required") == ["type", "id"]   # id promoted WARN->MUST
    assert base.get("okf", {}).get("should") == []
    assert base.get("strict_types") is False
    assert "id" in base.get("common_optional", [])


def test_merge_packless_falls_back_to_base(tmp_path):
    m = _load()
    root = _vault(tmp_path, None)  # no pack schema
    merged = m.merged_schema(root)
    assert merged["okf"]["required"] == ["id", "type"]   # union, sorted; id now required
    assert merged["okf"].get("should", []) == []   # empty WARN tier may be omitted from merge
    assert merged["strict_types"] is False
    assert "id" in merged["common_optional"]


def test_merge_layers_pack_on_base(tmp_path):
    m = _load()
    root = _vault(tmp_path, (
        "okf: {required: [type]}\n"
        "types:\n  entity: {required: [type]}\n"
        "partitioning:\n  namespaces: {entities: {strategy: flat}}\n"
        "common_optional: [vendor_field]\n"
    ))
    merged = m.merged_schema(root)
    # pack keys pass through + the pack INHERITS the engine-owned core (okengine#90)
    assert "entity" in merged["types"]                                    # pack's domain type
    assert {"source", "concept", "prediction"} <= set(merged["types"])    # core inherited from base
    assert merged["partitioning"]["namespaces"]["entities"]["strategy"] == "flat"   # pack overrides core default
    # base owns the globals; required is the union (type + id always present)
    assert merged["okf"]["required"] == ["id", "type"]
    assert merged["okf"].get("should", []) == []   # empty WARN tier may be omitted from merge
    # common_optional unions base + pack
    assert "id" in merged["common_optional"] and "vendor_field" in merged["common_optional"]


def test_type_id_authority(tmp_path):
    m = _load()
    root = _vault(tmp_path, (
        "types:\n"
        "  attack-pattern: {required: [type], id_authority: mitre, id_field: technique_id}\n"
        "  entity: {required: [type]}\n"
    ))
    sch = m.governing_schema(root)
    assert m.type_id_authority(sch, "attack-pattern") == ("mitre", "technique_id")
    assert m.type_id_authority(sch, "entity") == (None, "external_id")   # no authority
    assert m.type_id_authority(sch, "nonexistent") == (None, "external_id")


def test_type_owner_and_field_owners(tmp_path):
    m = _load()
    root = _vault(tmp_path, (
        "types:\n"
        "  attack-pattern:\n"
        "    required: [type]\n"
        "    owner: okpack-attack\n"
        "    field_owners: {detection: okpack-hunt}\n"
        "  entity: {required: [type]}\n"
    ))
    sch = m.governing_schema(root)
    assert m.type_owner(sch, "attack-pattern") == "okpack-attack"
    assert m.field_owners(sch, "attack-pattern") == {"detection": "okpack-hunt"}
    assert m.type_owner(sch, "entity") is None          # undeclared -> no enforcement
    assert m.field_owners(sch, "entity") == {}


def test_required_unions_and_pack_can_opt_into_strict_types(tmp_path):
    m = _load()
    # a pack that adds a global required field AND tries to set strict_types: true
    root = _vault(tmp_path, (
        "strict_types: true\n"
        "okf: {required: [type, name]}\n"
        "types:\n  entity: {required: [type]}\n"
    ))
    merged = m.merged_schema(root)
    # required is the union (sorted; stricter-only); base guarantees `type` + `id`
    assert merged["okf"]["required"] == ["id", "name", "type"]
    # strictness is monotonic: the pack can close the composed taxonomy
    assert merged["strict_types"] is True
    # a pack that omits strict_types likewise inherits the engine-base default
    root2 = _vault(tmp_path / "p2", "types:\n  entity: {required: [type]}\n")
    assert m.merged_schema(root2)["strict_types"] is False


# --- reference-catalog classification (KB-health: reference imports != content debt) ---

def test_reference_policy_reads_top_level_keys():
    m = _load()
    rp = m.reference_policy({"reference_types": ["vulnerability"], "reference_fields": ["mitre_id"]})
    assert rp["types"] == {"vulnerability"} and rp["fields"] == {"mitre_id"}


def test_reference_policy_defaults_empty_when_unset():
    m = _load()
    rp = m.reference_policy({})
    assert rp == {"types": set(), "fields": set()}


def test_is_reference_page_by_type_and_by_field():
    m = _load()
    rp = m.reference_policy({"reference_types": ["vulnerability"], "reference_fields": ["mitre_id"]})
    assert m.is_reference_page({"type": "vulnerability"}, rp) is True              # CVE catalog (by type)
    assert m.is_reference_page({"type": "intrusion-set", "mitre_id": "G1036"}, rp) is True  # ATT&CK (by field)
    # the discriminating case: SAME type, but source-cited synthesized content is NOT reference
    assert m.is_reference_page({"type": "intrusion-set", "sources": ["sources/x"]}, rp) is False
    assert m.is_reference_page({"type": "lacuna"}, rp) is False


def test_is_reference_page_noop_without_policy():
    m = _load()
    rp = m.reference_policy({})                      # pack didn't opt in
    assert m.is_reference_page({"type": "vulnerability", "mitre_id": "x"}, rp) is False


def test_field_shapes_merge_and_list_fields(tmp_path):
    """okengine#196 generalized: base declares the universal list fields; a pack ADDS its own
    (and WINS on a conflicting key), same as field_enums. `list_fields` returns the list-shaped set
    the write path coerces scalars into."""
    m = _load()
    root = _vault(tmp_path, (
        "types:\n  entity: {required: [type]}\n"
        "field_shapes:\n"
        "  refs: list\n"        # pack ADDS a domain list field
        "  aliases: scalar\n"   # pack OVERRIDES a base list field (proves pack-wins precedence)
    ))
    merged = m.merged_schema(root)
    fs = merged.get("field_shapes") or {}
    assert fs.get("tags") == "list" and fs.get("maintained_by") == "list"   # base contributes
    assert fs.get("refs") == "list"                                          # pack adds
    assert fs.get("aliases") == "scalar"                                     # pack wins on conflict
    lf = m.list_fields(merged)
    assert "refs" in lf and "tags" in lf
    assert "aliases" not in lf                                               # now scalar per the pack
    # base-only (packless) still exposes the universal list fields
    assert "aliases" in m.list_fields(m.merged_schema(_vault(tmp_path / "p2", None)))


def test_int_fields_from_base_and_pack(tmp_path):
    """The `int` shape class (machine-owned counts — the recent_reports live incident): base declares
    the universal count fields; a pack can add its own; int_fields returns the set the write path
    coerces digit-strings for and REJECTS other shapes on."""
    m = _load()
    root = _vault(tmp_path, (
        "types:\n  entity: {required: [type]}\n"
        "field_shapes:\n"
        "  citation_count: int\n"    # pack ADDS a domain count field
    ))
    merged = m.merged_schema(root)
    infl = m.int_fields(merged)
    assert "recent_reports" in infl and "total_mentions" in infl   # base contributes
    assert "citation_count" in infl                                # pack adds
    assert "aliases" not in infl                                   # list fields stay out


def test_governing_schema_reloads_after_mtime_change(tmp_path):
    """invariant-audit HIGH: _SCHEMA_CACHE/_BASE_CACHE were cached FOREVER (path-keyed, no mtime),
    so the long-running write path validated every write against the pre-edit schema for the life of
    the gateway after any hand-edit. They must now invalidate on mtime, like _COMPOSED_CACHE."""
    import os, time
    m = _load()
    v = _vault(tmp_path, "types:\n  vendor: {required: [type]}\n")
    assert "vendor" in (m.governing_schema(v).get("types") or {})
    assert "widget" not in (m.governing_schema(v).get("types") or {})
    # hand-edit schema.yaml (no restart) + bump mtime — a fresh read must see the new type
    sp = v / "wiki" / "schema.yaml"
    sp_mtime = sp.stat().st_mtime
    sp.write_text("types:\n  vendor: {required: [type]}\n  widget: {required: [type]}\n")
    os.utime(sp, (sp_mtime + 2, sp_mtime + 2))       # guarantee a distinct mtime
    got = m.governing_schema(v).get("types") or {}
    assert "widget" in got, "governing_schema served a STALE schema after an on-disk edit"


def test_base_schema_reloads_after_mtime_change(tmp_path, monkeypatch):
    import os
    m = _load()
    bp = tmp_path / "base-schema.yaml"
    bp.write_text("okf:\n  required: [type]\ntypes: {a: {required: [type]}}\n")
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(bp))
    assert "a" in (m.base_schema().get("types") or {}) and "b" not in (m.base_schema().get("types") or {})
    mt = bp.stat().st_mtime
    bp.write_text("okf:\n  required: [type]\ntypes: {a: {required: [type]}, b: {required: [type]}}\n")
    os.utime(bp, (mt + 2, mt + 2))
    assert "b" in (m.base_schema().get("types") or {}), "base_schema served a STALE base after an edit"
