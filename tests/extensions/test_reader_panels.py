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
        {"type": "assessment", "kind": "fields", "fields": ["verdict", "confidence"]},
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


def test_deploy_does_not_swallow_reader_panels_staging_failure():  # invariant-audit B6.4
    """The collision above is detected by collect_reader_panels and surfaced as a non-zero
    `stage-panels` exit — but the deploy step used to run it as `stage-panels … || echo "skipped"`,
    swallowing that exit (and every real extension-config error) and shipping the deploy GREEN with an
    ambiguous panel map. stage-panels writes `{}` and exits 0 even with zero panels, so a non-zero
    exit is ALWAYS real: deploy-cron-scripts.sh must FAIL the deploy on it."""
    import re
    sh = (REPO / "scripts" / "deploy-cron-scripts.sh").read_text()
    assert "extensions stage-panels" in sh                              # still invoked
    assert "reader-panels staging skipped" not in sh, "stage-panels failure still swallowed as 'skipped'"
    assert "|| \\\n    echo" not in sh.split("stage-panels", 1)[1][:120], "still swallowed with `|| echo`"
    # the failure fails the deploy (exit 1 in the block right after the invocation)
    after = sh.split("extensions stage-panels", 1)[1][:400]
    assert "exit 1" in after, "stage-panels failure no longer fails the deploy"


def test_timeline_binding_rejected_no_renderer():  # invariant-audit #26
    """`timeline` has no renderer on any surface — a reader_panels binding for it validated GREEN then
    produced panel:null on every bound page. It must be rejected at the manifest gate now."""
    e, _ = MAN.validate_manifest(_base(reader_panels=[{"type": "event", "kind": "timeline", "fields": ["date"]}]))
    assert any("kind" in x for x in e), e
    assert "timeline" not in MAN._BINDABLE_PANEL_KINDS


def test_two_axis_binding_rejected_only_self_declared_renders():  # invariant-audit #351
    """A `kind: two-axis` reader_panels BINDING validated GREEN but the reader's type-bound path
    (_panel_for) builds a panel only for kind:fields — two-axis needs per-page nodes that exist only
    when a page SELF-DECLARES `panel:`. So a two-axis binding rendered panel:null on every bound page
    (the same never-renders class as timeline, #26). It must be rejected as a binding now."""
    e, _ = MAN.validate_manifest(_base(reader_panels=[{"type": "wardley", "kind": "two-axis", "x": "a", "y": "b"}]))
    assert any("'kind' must be one of" in x for x in e), e
    assert "two-axis" not in MAN._BINDABLE_PANEL_KINDS
