"""Regression tests for the N-way schema compose fold (okengine#90 P3 / #133).

schema_lib.compose_schema folds engine base ⊕ pack ⊕ Σ(extension fragments) with an
owner map and fail-loud Own/Reuse/Extend rules.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "schema_lib.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="schema_lib not present")


def _sl():
    spec = importlib.util.spec_from_file_location("schema_lib", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["schema_lib"] = m
    spec.loader.exec_module(m)
    return m


def _pack(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "schema.yaml").write_text(yaml.safe_dump({
        "partitioning": {"namespaces": {"entities": {}}},
        "types": {"entity": {"required": ["type"], "extensible": True}},
        "field_enums": {"source_kind": {"enum": "source_kind", "extensible": True}},
        "enums": {"source_kind": ["news", "blog"]},
    }), encoding="utf-8")
    return tmp_path


_FRAG = {
    # the extension OWNS its own non-core namespace/type — core predictions/prediction are
    # engine-owned now (okengine#90), so an extension writes INTO core but never owns it.
    "owns": {"namespaces": ["projections"],
             "types": {"projection": {"required": ["claim"],
                                      "fields": {"about": {"type": "ref", "to": "entity"}}}}},
    "extends": {"entity": {"fields": {"predicted_by": {"type": "ref", "to": "projection",
                                                       "optional": True}}},
                "source_kind": {"add": ["forecast"]}},
}


def test_fold_own_reuse_extend(tmp_path):
    s = _sl()
    composed, errors = s.compose_schema(_pack(tmp_path), [("ext:demo.pred", _FRAG)])
    assert errors == []
    assert {"entity", "projection"} <= set(composed["types"])   # pack + extension types
    assert "source" in composed["types"]                        # inherits the engine core (okengine#90)
    assert "projections" in composed["partitioning"]["namespaces"]
    assert "predicted_by" in composed["types"]["entity"]["fields"]   # extend
    assert "forecast" in composed["enums"]["source_kind"]            # enum extend
    o = composed["owners"]
    assert o["types"]["projection"] == "ext:demo.pred" and o["types"]["entity"] == "pack"
    assert o["namespaces"]["projections"] == "ext:demo.pred"
    assert o["fields"]["entity.predicted_by"] == "ext:demo.pred"
    assert o["enum_values"]["source_kind.forecast"] == "ext:demo.pred"


def test_owned_namespace_preserves_extension_partition_strategy(tmp_path):
    s = _sl()
    fragment = {"owns": {"namespaces": {
        "assessments": {"strategy": "by-letter", "reshard_by": "second-letter"},
    }}}
    composed, errors = s.compose_schema(_pack(tmp_path), [("ext:assessments", fragment)])
    assert errors == []
    assert composed["partitioning"]["namespaces"]["assessments"] == {
        "strategy": "by-letter", "reshard_by": "second-letter",
    }
    assert composed["owners"]["namespaces"]["assessments"] == "ext:assessments"


def test_owned_namespace_rejects_non_mapping_partition_strategy(tmp_path):
    s = _sl()
    _, errors = s.compose_schema(_pack(tmp_path), [
        ("ext:bad", {"owns": {"namespaces": {"bad": "flat"}}}),
    ])
    assert any("partition must be a mapping" in error for error in errors)


@pytest.mark.parametrize("unknown", ["own", "field_shape", "required"])
def test_unknown_top_level_fragment_key_fails_loudly(tmp_path, unknown):
    s = _sl()
    composed, errors = s.compose_schema(_pack(tmp_path), [
        ("ext:typo", {unknown: {"types": {"silently_lost": {}}}}),
    ])
    assert errors and "unknown top-level schema fragment key" in errors[0]
    assert repr(unknown) in errors[0] and "ext:typo" in errors[0]
    assert "silently_lost" not in composed["types"]


def test_every_documented_top_level_fragment_key_is_recognized(tmp_path):
    s = _sl()
    fragment = {
        "owns": {}, "enums": {}, "field_enums": {}, "field_shapes": {},
        "extends": {}, "field_items": {}, "partitioning": {}, "tier": {},
    }
    _, errors = s.compose_schema(_pack(tmp_path), [("ext:grammar", fragment)])
    assert errors == []


def test_extension_can_set_tier_policy_for_its_owned_namespace(tmp_path):
    s = _sl()
    fragment = {
        "owns": {"namespaces": {"inquiry": {"strategy": "flat"}}},
        "tier": {"namespaces": {
            "inquiry": {"status_field": "status", "open_values": ["open"],
                        "open_floor": "hot"},
        }},
    }
    composed, errors = s.compose_schema(_pack(tmp_path), [("ext:inquiry", fragment)])
    assert errors == []
    assert composed["tier"]["namespaces"]["inquiry"]["open_floor"] == "hot"


def test_extension_cannot_set_tier_policy_for_anothers_namespace(tmp_path):
    s = _sl()
    _, errors = s.compose_schema(_pack(tmp_path), [
        ("ext:intruder", {"owns": {"namespaces": {"owned": {}}},
                          "tier": {"namespaces": {
                              "entities": {"open_floor": "hot"},
                              "owned": {"open_floor": "warm"},
                          }}}),
    ])
    assert any("owned by the same extension" in error for error in errors)


def test_legacy_partition_policy_is_limited_to_owned_namespace(tmp_path):
    s = _sl()
    valid_pack = _pack(tmp_path / "valid")
    fragment = {
        "owns": {"namespaces": ["inquiry"]},
        "partitioning": {"namespaces": {"inquiry": {"strategy": "flat"}}},
    }
    composed, errors = s.compose_schema(valid_pack, [("ext:inquiry", fragment)])
    assert errors == []
    assert composed["partitioning"]["namespaces"]["inquiry"] == {"strategy": "flat"}

    invalid_composed, errors = s.compose_schema(_pack(tmp_path / "invalid"), [
        ("ext:intruder", {"owns": {"namespaces": {"owned": {}}},
                          "partitioning": {"namespaces": {
                              "entities": {}, "owned": {"strategy": "by-letter"},
                          }}}),
    ])
    assert any("owned by the same extension" in error for error in errors)
    assert invalid_composed["partitioning"]["namespaces"]["owned"] == {
        "strategy": "by-letter",
    }


def test_invalid_partition_and_tier_policy_do_not_hide_later_valid_policy(tmp_path):
    s = _sl()
    fragment = {
        "owns": {"namespaces": {"bad": {}, "good": {}}},
        "partitioning": {"namespaces": {"bad": "flat", "good": {"strategy": "flat"}}},
        "tier": {"namespaces": {"bad": "hot", "good": {"open_floor": "hot"}}},
    }
    composed, errors = s.compose_schema(_pack(tmp_path), [("ext:demo", fragment)])
    assert any("partitioning.namespaces.bad must be a mapping" in error for error in errors)
    assert any("tier.namespaces.bad must be a mapping" in error for error in errors)
    assert composed["partitioning"]["namespaces"]["good"] == {"strategy": "flat"}
    assert composed["tier"]["namespaces"]["good"] == {"open_floor": "hot"}


@pytest.mark.parametrize("section,value,error", [
    ("partitioning", "flat", "partitioning must be a mapping"),
    ("partitioning", {"namespaces": ["bad"]}, "partitioning.namespaces must be a mapping"),
    ("tier", "hot", "tier must be a mapping"),
    ("tier", {"namespaces": ["bad"]}, "tier.namespaces must be a mapping"),
])
def test_extension_namespace_policy_containers_must_be_mappings(
        tmp_path, section, value, error):
    s = _sl()
    _, errors = s.compose_schema(_pack(tmp_path), [("ext:bad", {section: value})])
    assert any(error in item for item in errors)


def test_own_conflict_fails(tmp_path):
    s = _sl()
    _, errors = s.compose_schema(_pack(tmp_path),
                                 [("ext:x", {"owns": {"types": {"entity": {}}}})])
    assert any("already owned" in e for e in errors)


def test_extend_nonextensible_fails(tmp_path):
    s = _sl()
    p = _pack(tmp_path)
    # make entity NOT extensible
    sp = p / "wiki" / "schema.yaml"
    data = yaml.safe_load(sp.read_text())
    data["types"]["entity"].pop("extensible")
    sp.write_text(yaml.safe_dump(data), encoding="utf-8")
    s._SCHEMA_CACHE.clear()
    _, errors = s.compose_schema(p, [("ext:x", {"extends": {"entity": {"fields": {"z": {}}}}})])
    assert any("not marked extensible" in e for e in errors)


def test_reuse_ref_to_unknown_type_fails(tmp_path):
    s = _sl()
    _, errors = s.compose_schema(_pack(tmp_path), [("ext:x", {"owns": {"types": {
        "p": {"fields": {"about": {"type": "ref", "to": "nonexistent"}}}}}})])
    assert any("unknown type 'nonexistent'" in e for e in errors)


def test_extend_required_field_fails(tmp_path):
    s = _sl()
    _, errors = s.compose_schema(_pack(tmp_path), [("ext:x", {"extends": {"entity": {
        "fields": {"z": {"type": "string", "optional": False}}}}})])
    assert any("must be optional" in e for e in errors)


def test_no_fragments_is_baseline_with_owners(tmp_path):
    s = _sl()
    composed, errors = s.compose_schema(_pack(tmp_path), [])
    assert errors == []
    assert composed["owners"]["types"]["entity"] == "pack"


def test_merged_schema_prefers_composed_artifact(tmp_path):
    """The write-server guards call merged_schema -> they must see extension-owned
    namespaces/types when a composed artifact exists (the live #133 gap)."""
    s = _sl()
    pack = _pack(tmp_path)
    (pack / ".okengine").mkdir()
    (pack / ".okengine" / "composed-schema.yaml").write_text(yaml.safe_dump({
        "partitioning": {"namespaces": {"entities": {}, "watchlists": {}}},
        "types": {"entity": {"required": ["type"]}, "watchlist": {"required": ["focus"]}},
        "okf": {"required": ["type", "id"]},
    }), encoding="utf-8")
    s._COMPOSED_CACHE.clear()
    m = s.merged_schema(pack)
    assert "watchlists" in s.knowledge_namespaces(m)   # namespace guard would now allow it
    assert "watchlist" in s.canonical_types(m)


def test_compose_schema_does_not_refold_artifact(tmp_path):
    """compose_schema builds from base⊕pack, never the existing artifact — so regen
    with the artifact present doesn't raise 'already owned' (no double-fold)."""
    s = _sl()
    pack = _pack(tmp_path)
    (pack / ".okengine").mkdir()
    (pack / ".okengine" / "composed-schema.yaml").write_text(yaml.safe_dump({
        "types": {"entity": {}, "watchlist": {}},
        "partitioning": {"namespaces": {"watchlists": {}}},
    }), encoding="utf-8")
    s._COMPOSED_CACHE.clear()
    composed, errors = s.compose_schema(pack, [("ext:demo", {"owns": {
        "namespaces": ["watchlists"], "types": {"watchlist": {"required": ["focus"]}}}})])
    assert errors == []                                 # not "already owned"
    assert composed["owners"]["types"]["watchlist"] == "ext:demo"
