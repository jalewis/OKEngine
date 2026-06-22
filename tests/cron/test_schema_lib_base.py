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
    assert base.get("okf", {}).get("required") == ["type"]
    assert base.get("okf", {}).get("should") == ["id"]
    assert base.get("strict_types") is False
    assert "id" in base.get("common_optional", [])


def test_merge_packless_falls_back_to_base(tmp_path):
    m = _load()
    root = _vault(tmp_path, None)  # no pack schema
    merged = m.merged_schema(root)
    assert merged["okf"]["required"] == ["type"]
    assert merged["okf"]["should"] == ["id"]
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
    # pack keys pass through
    assert set(merged["types"]) == {"entity"}
    assert merged["partitioning"]["namespaces"]["entities"]["strategy"] == "flat"
    # base owns the globals; required is the union (type always present)
    assert merged["okf"]["required"] == ["type"]
    assert merged["okf"]["should"] == ["id"]
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


def test_required_unions_and_strict_types_engine_owned(tmp_path):
    m = _load()
    # a pack that adds a global required field AND tries to set strict_types: true
    root = _vault(tmp_path, (
        "strict_types: true\n"
        "okf: {required: [type, name]}\n"
        "types:\n  entity: {required: [type]}\n"
    ))
    merged = m.merged_schema(root)
    # required is the union (sorted; stricter-only); base guarantees `type`
    assert merged["okf"]["required"] == ["name", "type"]
    # strict_types is ENGINE-OWNED: the pack's `true` is IGNORED, base default wins
    assert merged["strict_types"] is False
    # a pack that omits strict_types likewise inherits the engine-base default
    root2 = _vault(tmp_path / "p2", "types:\n  entity: {required: [type]}\n")
    assert m.merged_schema(root2)["strict_types"] is False
