import importlib.resources
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.cron import schema_lib as m


def test_source_checkout_package_requires_exact_existing_source_root(monkeypatch, tmp_path):
    expected = tmp_path / "repo" / "src"

    class FakeModulePath:
        def resolve(self):
            return self

        @property
        def parents(self):
            return [tmp_path / "wrong-0", tmp_path / "wrong-1", tmp_path / "repo"]

    monkeypatch.setattr(m, "Path", lambda _value: FakeModulePath())
    monkeypatch.setattr(sys, "path", ["sentinel"])

    m._add_source_checkout_package()
    assert sys.path == ["sentinel"]

    (expected / "okengine").mkdir(parents=True)
    m._add_source_checkout_package()
    assert sys.path == [str(expected), "sentinel"]

    m._add_source_checkout_package()
    assert sys.path == [str(expected), "sentinel"]


def test_schema_lib_import_survives_missing_installed_package(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/cron/schema_lib.py"
    monkeypatch.setattr(
        importlib.resources,
        "files",
        lambda _package: (_ for _ in ()).throw(ModuleNotFoundError("not installed")),
    )
    spec = importlib.util.spec_from_file_location("schema_lib_without_package", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    assert module._PACKAGE_ROOT == Path("/__okengine_package_not_installed__")


def test_yaml_cache_missing_scalar_invalid_and_hit(tmp_path):
    cache={}
    assert m._yaml_mtime_cached(tmp_path/"missing",cache)=={}
    p=tmp_path/"x.yaml";p.write_text("- scalar")
    assert m._yaml_mtime_cached(p,cache)=={}
    p.write_text("[bad")
    assert m._yaml_mtime_cached(p,{})=={}
    p.write_text("x: 1")
    first=m._yaml_mtime_cached(p,cache)
    assert m._yaml_mtime_cached(p,cache) is first


def test_yaml_cache_requires_exact_mtime_match_even_when_clock_moves_back(tmp_path):
    cache = {}
    path = tmp_path / "schema.yaml"
    path.write_text("value: old\n")
    os.utime(path, (200, 200))
    assert m._yaml_mtime_cached(path, cache) == {"value": "old"}
    path.write_text("value: new\n")
    os.utime(path, (100, 100))
    assert m._yaml_mtime_cached(path, cache) == {"value": "new"}


def test_merge_base_pack_exercises_optional_composition_blocks(monkeypatch,tmp_path):
    base={
      "okf":{"required":["type"],"should":["id"]},"strict_types":True,
      "common_optional":["a"],"types":{"core":{"extensible":True,"fields":{}}},
      "partitioning":{"namespaces":{"core":{}}},"tier":{"namespaces":{"core":"hot"}},
      "enums":{"e":["a"]},"field_enums":{"f":"e"},"field_shapes":{"xs":"list"},
      "field_items":{"items":{"x":{"shape":"str"}}},
      "conformance":{"rules":[{"id":"base"},{"kind":"anonymous"}]},
    }
    pack={
      "okf":{"required":["name"]},"common_optional":["b"],"types":{"local":{}},
      "partitioning":{"namespaces":{"local":{}}},"tier":{"namespaces":{"local":"warm"}},
      "extends":{
        "core":{"fields":{"extra":{"shape":"str"}}},
        "missing":{"fields":{"x":{}}},
      },
      "enums":{"e":["a","b"],"p":["x"]},"field_enums":{"g":"p"},
      "field_shapes":{"ys":"list"},"field_items":{"other":{}},
      "conformance":{"rules":[{"id":"base","pack":True},None]},
    }
    monkeypatch.setattr(m,"base_schema",lambda:base)
    monkeypatch.setattr(m,"governing_schema",lambda *_:pack)
    out=m._merge_base_pack(tmp_path)
    assert out["okf"]=={"required":["name","type"],"should":["id"]}
    assert out["strict_types"] and out["common_optional"]==["a","b"]
    assert set(out["types"])=={"core","local"}
    assert out["types"]["core"]["fields"]["extra"]=={"shape":"str"}
    assert out["enums"]["e"]==["a","b"] and out["enums"]["p"]==["x"]
    assert set(out["partitioning"]["namespaces"])=={"core","local"}
    assert out["conformance"]["rules"][0]["pack"] is True


def test_item_rules_malformed_and_all_supported_shapes():
    schema={"enums":{"named":["A",2]},"field_items":{
      "bad":"scalar",
      "items":{
        "_item":{"shape":"dict","required":["x",""]},
        "skip":"scalar","inline":{"enum":["a",2]},"named":{"enum":"named"},
        "unknown":{"enum":"missing"},"number":{"shape":"number"}}
    }}
    rules=m.item_rules(schema)
    assert "bad" not in rules and "_item" not in rules["items"]
    assert rules["items"]["inline"]["enum"]=={"a","2"}
    assert rules["items"]["named"]["enum"]=={"A","2"}
    assert "unknown" not in rules["items"] and rules["items"]["number"]["shape"]=="number"


def test_recorded_fragments_reference_and_excluded_edges(tmp_path):
    assert m._recorded_fragments(tmp_path)==[]
    art=tmp_path/".okengine/composed-schema.yaml";art.parent.mkdir()
    art.write_text("_fragments:\n- [owner, {types: {}}]\n- bad\n- [1, {}]\n")
    assert m._recorded_fragments(tmp_path)==[("owner",{"types":{}})]
    assert not m.is_reference_page([],{"types":{"x"}})
    assert m.is_reference_page({"type":"x"},{"types":{"x"},"fields":set()})
    assert m.is_reference_page({"external":"1"},{"types":set(),"fields":{"external"}})
    assert m.excluded_dirs({"exclude":["wiki/operational/","cache","/wiki/entities/"]})=={
      "operational","cache","entities"}


def test_merge_base_pack_all_optional_blocks_absent(monkeypatch,tmp_path):
    monkeypatch.setattr(m,"base_schema",lambda:{})
    monkeypatch.setattr(m,"governing_schema",lambda *_:{})
    out=m._merge_base_pack(tmp_path)
    assert out=={"okf":{"required":["type"]},"strict_types":False}


def test_merge_base_pack_keeps_types_when_only_one_side_declares_them(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "base_schema", lambda: {"types": {"base": {}}})
    monkeypatch.setattr(m, "governing_schema", lambda *_: {})
    assert m._merge_base_pack(tmp_path)["types"] == {"base": {}}

    monkeypatch.setattr(m, "base_schema", lambda: {})
    monkeypatch.setattr(m, "governing_schema", lambda *_: {"types": {"pack": {}}})
    assert m._merge_base_pack(tmp_path)["types"] == {"pack": {}}


def test_compose_rejects_nonmapping_fragment(monkeypatch,tmp_path):
    monkeypatch.setattr(m,"merged_schema",lambda *_:{
      "types":{},"partitioning":{"namespaces":{}}})
    monkeypatch.setattr(m,"base_schema",lambda:{})
    _schema,errors=m.compose_schema(tmp_path,[("bad","scalar")])
    assert errors==["bad: schema fragment is not a mapping"]


def test_packaged_dependency_search_and_default_base_fallback(monkeypatch, tmp_path):
    configured = str(tmp_path / "configured")
    discovered = str(tmp_path / "discovered")
    for candidate in (configured, discovered):
        while candidate in m.sys.path:
            m.sys.path.remove(candidate)
    probes = iter([None, None, object()])
    monkeypatch.setenv("OKENGINE_PACKAGED_SITE_PACKAGES", configured)
    # schema_lib imports glob as a function, so replace that binding directly.
    monkeypatch.setattr(m, "glob", lambda *_a: [discovered])
    monkeypatch.setattr(m.importlib.util, "find_spec", lambda _name: next(probes))
    m._add_packaged_dependencies()
    assert configured in m.sys.path and discovered in m.sys.path

    # An already-present candidate takes the no-insert branch before discovery succeeds.
    probes = iter([None, object()])
    monkeypatch.setattr(m.importlib.util, "find_spec", lambda _name: next(probes))
    monkeypatch.setattr(m, "glob", lambda *_a: [])
    m._add_packaged_dependencies()

    probes = iter([None, None])
    monkeypatch.setattr(m.importlib.util, "find_spec", lambda _name: next(probes))
    monkeypatch.setattr(m, "glob", lambda *_a: [])
    m._add_packaged_dependencies()

    missing_a, missing_b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(m, "_BASE_CANDIDATES", (missing_a, missing_b))
    assert m._default_base() == missing_a


def test_composed_and_recorded_artifact_parse_failures_and_protected_fields(tmp_path, monkeypatch):
    artifact = tmp_path / ".okengine/composed-schema.yaml"
    artifact.parent.mkdir()
    artifact.write_text("[")
    assert m._composed_artifact_at(tmp_path) is None
    assert m._composed_artifact(tmp_path) is None

    artifact.write_text("types: {}\n")
    m._COMPOSED_CACHE.clear()
    first = m._composed_artifact_at(tmp_path)
    assert m._composed_artifact_at(tmp_path) is first

    artifact.write_text("owners: {}\n")
    assert m._recorded_fragments(tmp_path) == []

    artifact.write_text("_fragments: []\n")
    monkeypatch.setattr(
        m, "fast_load", lambda _text: (_ for _ in ()).throw(ValueError("bad yaml")),
    )
    assert m._recorded_fragments(tmp_path) == []
    assert m.protected_fields({"protected_fields": ["a", 2]}) == {"a", "2"}
    assert m.protected_fields({"protected_fields": "bad"}) == set()
    assert m.conformance_rules({"conformance": {"rules": [
        {"id": "ok", "kind": "ref_fields"}, {"id": "missing-kind"}, "bad",
    ]}}) == [{"id": "ok", "kind": "ref_fields"}]
    assert m.conformance_rules({}) == []
    assert m.excluded_dirs({"exclude": ["cache", "other"]}) == {"cache", "other"}


def test_composed_artifact_cache_requires_exact_mtime_match(tmp_path):
    artifact = tmp_path / ".okengine" / "composed-schema.yaml"
    artifact.parent.mkdir()
    artifact.write_text("value: old\n")
    os.utime(artifact, (200, 200))
    m._COMPOSED_CACHE.clear()
    assert m._composed_artifact_at(tmp_path) == {"value": "old"}

    artifact.write_text("value: new\n")
    # A restored/checked-out file can move backwards in time. It must still
    # invalidate the cache; only equality proves the bytes are unchanged.
    os.utime(artifact, (100, 100))
    assert m._composed_artifact_at(tmp_path) == {"value": "new"}


def test_governing_directory_and_schema_stop_at_nearest_domain(tmp_path):
    root_schema = tmp_path / "schema.yaml"
    root_schema.write_text("types: {root: {}}\n")
    domain = tmp_path / "wiki" / "domains" / "one"
    domain.mkdir(parents=True)
    (domain / "schema.yaml").write_text("types: {domain: {}}\n")

    assert m._governing_dir(tmp_path, "domains/one/deep/page") == domain
    assert m.governing_schema(tmp_path, "domains/one/deep/page") == {
        "types": {"domain": {}}}
    assert m._governing_dir(tmp_path, "unowned/deep") == tmp_path
    assert m.governing_schema(tmp_path, "unowned/deep") == {"types": {"root": {}}}


def test_governing_walk_never_escapes_the_vault_root(tmp_path):
    outside_schema = tmp_path / "schema.yaml"
    outside_schema.write_text("types: {outside: {}}\n")
    vault = tmp_path / "vault"
    (vault / "wiki" / "nested").mkdir(parents=True)

    assert m._governing_dir(vault, "nested/page") == vault
    assert m.governing_schema(vault, "nested/page") == {}


def test_conformance_merge_preserves_pack_precedence_and_anonymous_rules(monkeypatch, tmp_path):
    base = {"conformance": {"rules": [
        {"id": "same", "origin": "base"}, {"id": "base"}, {"kind": "anonymous-base"}]}}
    pack = {"conformance": {"rules": [
        {"id": "same", "origin": "pack"}, {"id": "pack"}, {"kind": "anonymous-pack"}]}}
    monkeypatch.setattr(m, "base_schema", lambda: base)
    monkeypatch.setattr(m, "governing_schema", lambda *_: pack)
    assert m._merge_base_pack(tmp_path)["conformance"]["rules"] == [
        {"id": "same", "origin": "pack"},
        {"id": "pack"},
        {"kind": "anonymous-pack"},
        {"id": "base"},
        {"kind": "anonymous-base"},
    ]


def test_compose_auto_fragments_and_owned_namespace_collision(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "_recorded_fragments", lambda _root: [
        ("ext:test", {"owns": {"namespaces": {"core": {}}}}),
    ])
    monkeypatch.setattr(m, "_merge_base_pack", lambda *_a: {
        "types": {}, "partitioning": {"namespaces": {"core": {}}},
    })
    monkeypatch.setattr(m, "base_schema", lambda: {
        "types": {}, "partitioning": {"namespaces": {"core": {}}},
    })
    _schema, errors = m.compose_schema(tmp_path, fragments=None)
    assert any("namespace 'core' already owned" in error for error in errors)


def test_owner_maps_distinguish_engine_and_pack_contracts(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "base_schema", lambda: {
        "types": {"core": {}},
        "partitioning": {"namespaces": {"core": {}}},
        "enums": {"core_enum": []},
        "field_enums": {"core_field": {}},
        "field_shapes": {"core_shape": "list"},
    })
    monkeypatch.setattr(m, "_merge_base_pack", lambda *_a: {
        "types": {"core": {}, "pack_type": {}},
        "partitioning": {"namespaces": {"core": {}, "pack_ns": {}}},
        "enums": {"core_enum": [], "pack_enum": []},
        "field_enums": {"core_field": {}, "pack_field": {}},
        "field_shapes": {"core_shape": "list", "pack_shape": "list"},
    })
    composed, errors = m.compose_schema(tmp_path, [])
    assert errors == []
    assert composed["owners"] == {
        "namespaces": {"core": "engine", "pack_ns": "pack"},
        "types": {"core": "engine", "pack_type": "pack"},
        "fields": {}, "enum_values": {},
        "enums": {"core_enum": "engine", "pack_enum": "pack"},
        "field_enums": {"core_field": "engine", "pack_field": "pack"},
        "field_shapes": {"core_shape": "engine", "pack_shape": "pack"},
    }


def test_excluded_wiki_path_uses_first_namespace_not_leaf():
    import pytest
    for ambiguous in (
        "wiki/operational/archive/", "wiki/entities/private/", "cache/nested",
    ):
        with pytest.raises(ValueError, match="not a namespace exclusion"):
            m.excluded_dirs({"exclude": [ambiguous]})


def test_list_fields_requires_exact_shape_name():
    assert m.list_fields({"field_shapes": {
        "actual": "list", "integer": "int", "mapping": "dict", "text": "str",
    }}) == {"actual"}
