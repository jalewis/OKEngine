"""The first-party okengine.predictions extension composes into 3 wake-gated agent jobs.

This is the proving case for the multi-op + agent-op + bundled-prompt work — the engine's
canonical example extension, migrated out of the cron fleet (extensions/okengine.predictions).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.predictions"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not EXT.is_dir() or not COMPOSE.is_file(),
                                reason="okengine.predictions extension or compose module absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _manifest():
    import yaml
    return yaml.safe_load((EXT / "extension.yaml").read_text(encoding="utf-8"))


@pytest.mark.contract
def test_manifest_is_valid_and_first_party():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.predictions"
    assert mod.is_reserved_id(m["id"])                 # first-party okengine.* namespace
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


@pytest.mark.contract
def test_composes_into_agent_and_no_agent_jobs():
    c = _load("extension_compose", COMPOSE)
    rec = {"id": "okengine.predictions", "tier": "engine", "dir": str(EXT), "manifest": _manifest()}
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    by_name = {j["name"]: j for j in jobs}
    agent = ["okengine.predictions:candidate-watch", "okengine.predictions:grade",
             "okengine.predictions:regrade", "okengine.predictions:base-rates",
             "okengine.predictions:prediction-falsification-search",
             "okengine.predictions:output-outcome-eval",
             "okengine.predictions:prediction-structural-backfill",
             "okengine.predictions:prediction-schema-drain",
             "okengine.predictions:forecast-review"]
    no_agent = ["okengine.predictions:calibration-refresh", "okengine.predictions:prediction-date-audit",
                "okengine.predictions:prediction-schema-audit",
                "okengine.predictions:confidence-recommender"]
    assert sorted(by_name) == sorted(agent + no_agent)
    for n in agent:
        j = by_name[n]
        assert j["no_agent"] is False
        assert isinstance(j["prompt"], str) and j["prompt"].strip()   # bundled prompt loaded
        assert "select_" in j["script"]
        assert "okengine-write" in j["enabled_toolsets"]
    for n in no_agent:                                  # forecasting-discipline measurement lanes (#159)
        j = by_name[n]
        assert j["no_agent"] is True                    # script-only, no prompt
        assert not (j.get("prompt") or "").strip()
        assert j["script"].endswith(".py")


@pytest.mark.contract
def test_bundled_prompt_files_exist_and_nonempty():
    for op in ("candidate-watch", "grade", "regrade", "base-rates",
               "falsification-search", "output-outcome-eval", "structural-backfill", "schema-drain"):
        f = EXT / "prompts" / f"{op}.md"
        assert f.is_file() and f.read_text(encoding="utf-8").strip(), op


@pytest.mark.contract
def test_schema_drain_transcribes_deterministic_horizon_from_digest():
    prompt = (EXT / "prompts" / "schema-drain.md").read_text(encoding="utf-8")
    assert "transcribe" in prompt
    assert "authoritative" in prompt
    assert "Do not independently reclassify" in prompt


@pytest.mark.contract
def test_write_capabilities():
    m = _manifest()
    # predictions (the book) + dashboards (derived base-rates / outcome-eval).
    assert m["capabilities"]["write"] == ["predictions/**", "dashboards/**"]
    # #217: the extension SHIPS a fragment — but only the evidence ITEM contract. The
    # prediction TYPE stays pack-owned: the fragment must never grow owns/extends (that
    # was this assertion's original point, now stated precisely instead of as "no schema").
    assert m.get("schema") == ["schema/predictions.schema.yaml"]
    import yaml as _y
    frag = _y.safe_load((EXT / "schema" / "predictions.schema.yaml").read_text(encoding="utf-8"))
    # still ONLY field contracts — the fragment must never grow owns/extends (the prediction TYPE
    # stays pack-owned). `field_enums` gave way to `field_shapes` in okengine#563.
    assert set(frag) == {"enums", "field_items", "field_shapes"}, (
        f"fragment grew beyond field contracts: {set(frag)}")
    # okengine#563: `confidence` on a prediction is the PROBABILITY this extension's calibration
    # lane feeds to a Brier score, so it is bound by SHAPE, not by a band vocabulary. The
    # prediction_confidence enum stays defined for a pack that carries a separate band field.
    assert "field_enums" not in frag, (
        "binding `confidence` to a band enum contradicts calibration_refresh's Brier score and 91% "
        "of the corpus; declare a numeric shape instead")
    assert frag["field_shapes"]["confidence"]["by_type"]["prediction"] == "number"
    assert frag["enums"]["prediction_confidence"] == [
        "very-low", "low", "medium-low", "medium", "medium-high", "high", "very-high",
    ]


def test_confidence_is_declared_numeric_for_predictions():
    """The contract moved from vocabulary to SHAPE. calibration_refresh computes
    `sum((confidence - outcome) ** 2) / n`; a band string cannot participate in that, and the
    corpus is 79-91% numeric on the two live vaults measured."""
    import yaml as _y
    frag = _y.safe_load((EXT / "schema" / "predictions.schema.yaml").read_text(encoding="utf-8"))
    assert frag["field_shapes"]["confidence"]["by_type"] == {"prediction": "number"}
    validator = _load("predictions_schema_validator", REPO / "tools" / "schema_validator.py")
    # and no enum governs it any more, on prediction or anywhere else
    assert validator._enum_reject_reason(frag, "prediction", {"confidence": 0.65}) is None
    assert validator._enum_reject_reason(frag, "assessment", {"confidence": 0.65}) is None


def test_structural_backfill_selection_is_bound_to_prewrite_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(EXT))
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    selector = _load(
        "prediction_structural_selector",
        EXT / "select_prediction_structural_backfill.py",
    )
    page = tmp_path / "wiki" / "predictions" / "one.md"
    page.parent.mkdir(parents=True)
    page.write_text("# One\n")

    key = selector._selection_key(page, tmp_path)

    assert key.startswith("wiki/predictions/one.md|sha256:")
    assert len(key.rsplit(":", 1)[1]) == 64
