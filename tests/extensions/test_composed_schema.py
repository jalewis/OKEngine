"""Regression tests for composed-schema generation + write-path enforcement (okengine#133).

enable an extension that brings its own schema -> .okengine/composed-schema.yaml is
generated and the validator (write path) enforces the extension's type contract;
disable -> the artifact is removed (the pack schema.yaml governs again).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
COMP = REPO / "scripts" / "extension_compose.py"
DISC = REPO / "scripts" / "extension_discovery.py"

pytestmark = pytest.mark.skipif(not COMP.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _pack_with_schema_ext(tmp_path):
    pack = tmp_path / "pack"
    (pack / "wiki").mkdir(parents=True)
    # schema.yaml lives at the vault ROOT (sibling to wiki/ and .okengine/), as real packs do
    (pack / "schema.yaml").write_text(yaml.safe_dump({
        "apply_under": ["wiki/"],
        "partitioning": {"namespaces": {"entities": {}}},
        "types": {"entity": {"required": ["type"]}},
    }), encoding="utf-8")
    d = pack / "extensions" / "demo.pred"
    (d / "schema").mkdir(parents=True)
    (d / "extension.yaml").write_text(yaml.safe_dump({
        "id": "demo.pred", "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
        "requires": {"engine": ">=0.3.0"},
        "capabilities": {"read": ["wiki/**"], "write": ["forecasts/**"]},
        "schema": ["schema/forecasts.schema.yaml"],
        "operation": {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                      "entrypoint": {"script": "run.py"}},
    }), encoding="utf-8")
    # the extension OWNS a non-core namespace/type (core predictions/prediction are engine-owned
    # now, okengine#90 — an extension writes INTO core namespaces but cannot own them).
    (d / "schema" / "forecasts.schema.yaml").write_text(yaml.safe_dump({
        "owns": {"namespaces": ["forecasts"],
                 "types": {"forecast": {"required": ["claim"]}}},
    }), encoding="utf-8")
    return pack


def test_enable_generates_composed_schema_then_disable_removes(tmp_path):
    comp = _load("extension_compose", COMP)
    disc = _load("extension_discovery", DISC)
    pack = _pack_with_schema_ext(tmp_path)
    artifact = pack / ".okengine" / "composed-schema.yaml"

    disc.set_enabled(pack, "demo.pred", True)
    assert comp.write_composed_schema(pack) == []
    assert artifact.is_file()
    composed = yaml.safe_load(artifact.read_text())
    assert "forecast" in composed["types"]
    assert composed["owners"]["types"]["forecast"] == "ext:demo.pred"

    # disable -> no extension brings schema -> artifact removed (schema.yaml governs)
    disc.set_enabled(pack, "demo.pred", False)
    assert comp.write_composed_schema(pack) == []
    assert not artifact.is_file()


def test_composer_loads_schema_lib_from_flat_gateway_staging(tmp_path):
    """deploy-cron-scripts flattens scripts/cron into /opt/data/scripts."""
    comp = _load("extension_compose", COMP)
    staged = tmp_path / "scripts"
    staged.mkdir()
    (staged / "schema_lib.py").write_text("LAYOUT = 'flat-staged'\n", encoding="utf-8")
    comp._HERE = staged
    assert comp._schema_lib().LAYOUT == "flat-staged"


def test_write_path_enforces_extension_type_via_composed_artifact(tmp_path, monkeypatch):
    comp = _load("extension_compose", COMP)
    disc = _load("extension_discovery", DISC)
    pack = _pack_with_schema_ext(tmp_path)
    disc.set_enabled(pack, "demo.pred", True)
    assert comp.write_composed_schema(pack) == []

    sv = _load("schema_validator", REPO / "tools" / "schema_validator.py")
    sv._dir_to_schema.clear()
    sv._schema_cache.clear()
    page = pack / "wiki" / "forecasts" / "p1.md"
    page.parent.mkdir(parents=True)

    # a forecast MISSING its required `claim` (declared by the extension fragment) is rejected
    bad = "---\ntype: forecast\nid: forecast:p1\n---\n# p1\n"
    assert sv.schema_reject_reason(str(page), bad)              # truthy = rejected
    # with claim present it passes
    good = "---\ntype: forecast\nid: forecast:p1\nclaim: X beats Y\n---\n# p1\n"
    assert not sv.schema_reject_reason(str(page), good)


def test_live_recompose_agrees_with_artifact_via_recorded_fragments(tmp_path):
    """okengine#195: the deploy-side composer (extension_compose) and the in-gateway recompose
    (schema_lib.compose_schema with NO explicit fragments — deployment_validate/conformance_audit's
    call shape) must AGREE when a schema-bearing extension is enabled. Pre-fix, the live recompose
    silently omitted every enabled extension's schema: the staleness WARN fired forever on okcti
    (lacuna) and no redeploy could clear it. The artifact now records its fragment INPUTS
    (_fragments) and compose_schema auto-loads them."""
    comp = _load("extension_compose", COMP)
    schema_lib = _load("schema_lib", REPO / "scripts" / "cron" / "schema_lib.py")
    disc = _load("extension_discovery", DISC)
    pack = _pack_with_schema_ext(tmp_path)

    disc.set_enabled(pack, "demo.pred", True)
    assert comp.write_composed_schema(pack) == []
    artifact = yaml.safe_load((pack / ".okengine" / "composed-schema.yaml").read_text())
    assert artifact.get("_fragments"), "artifact must record its fragment inputs"

    live, errors = schema_lib.compose_schema(pack)          # fragments=None -> auto-load recorded
    assert errors == [], errors
    # the extension's contributions are present in the LIVE recompose...
    assert "forecast" in live["types"], "live recompose omitted the extension type (the #195 bug)"
    assert "forecasts" in live["partitioning"]["namespaces"]
    assert live["owners"]["types"]["forecast"] == "ext:demo.pred"
    # ...and the two compositions agree on every governance key the staleness check compares
    for key in ("types", "enums", "partitioning", "permissions", "owners", "review"):
        assert live.get(key) == artifact.get(key), f"composers disagree on {key!r}"


def test_recompose_without_artifact_or_fragments_is_base_pack_only(tmp_path):
    """No artifact (or a pre-#195 artifact without _fragments) -> the exact old behavior."""
    schema_lib = _load("schema_lib", REPO / "scripts" / "cron" / "schema_lib.py")
    pack = _pack_with_schema_ext(tmp_path)                  # extension present but NOT enabled/composed
    live, errors = schema_lib.compose_schema(pack)
    assert errors == []
    assert "forecast" not in live["types"]                  # nothing auto-enables
    # a hand-planted artifact with garbage _fragments must not crash or leak
    art = pack / ".okengine" / "composed-schema.yaml"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text("_fragments: 'not a list'\n", encoding="utf-8")
    live2, errors2 = schema_lib.compose_schema(pack)
    assert errors2 == [] and "forecast" not in live2["types"]
