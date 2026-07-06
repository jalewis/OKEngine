"""Reader UI extension points (okengine#160 Phase 1): the reader_panels manifest contract +
the composer. Declarative bindings only — no extension renderer code."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


MAN = _load("extension_manifest")
COMP = _load("extension_compose")


def _base(**extra):
    return {"id": "okengine.x", "kind": "operation", "version": "0.1.0",
            "requires": {"engine": ">=0.4.0"}, "trust": "in-gateway",
            "capabilities": {"read": ["wiki/**"]}, **extra}


def test_valid_reader_panels_passes():
    m = _base(reader_panels=[
        {"type": "whitespace-thesis", "kind": "two-axis", "x": "a", "y": "b", "label": "title"},
        {"type": "prediction", "kind": "fields", "fields": ["confidence", "status"]}])
    errors, _ = MAN.validate_manifest(m)
    assert not errors, errors


def test_reader_panels_contract_violations():
    e1, _ = MAN.validate_manifest(_base(reader_panels=[{"kind": "fields"}]))            # no type
    assert any("'type'" in x for x in e1)
    e2, _ = MAN.validate_manifest(_base(reader_panels=[{"type": "t", "kind": "bogus"}]))  # bad kind
    assert any("'kind' must be one of" in x for x in e2)
    e3, _ = MAN.validate_manifest(_base(reader_panels=[{"type": "t", "kind": "fields", "fields": "x"}]))
    assert any("'fields' must be a list" in x for x in e3)
    e4, _ = MAN.validate_manifest(_base(reader_panels={"not": "a list"}))
    assert any("reader_panels must be a list" in x for x in e4)


def test_collect_reader_panels_composes_and_dedups():
    resolved = {
        "okengine.viz": {"manifest": {"reader_panels": [{"type": "wardley", "kind": "two-axis"}]}},
        "okengine.predictions": {"manifest": {"reader_panels": [{"type": "prediction", "kind": "fields"}]}},
    }
    out, errors = COMP.collect_reader_panels(resolved)
    assert not errors
    assert set(out) == {"wardley", "prediction"}
    assert out["wardley"]["extension"] == "okengine.viz"        # provenance stamped
    # two extensions binding the same type -> fail-loud
    clash = {
        "a": {"manifest": {"reader_panels": [{"type": "dup", "kind": "fields"}]}},
        "b": {"manifest": {"reader_panels": [{"type": "dup", "kind": "two-axis"}]}},
    }
    _, errs2 = COMP.collect_reader_panels(clash)
    assert errs2 and "bound by both" in errs2[0]
