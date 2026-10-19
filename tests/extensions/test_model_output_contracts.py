"""Repository-wide boundary checks for first-party extension model lanes."""
from pathlib import Path
import json

import yaml

from scripts.cron.output_contract import validate


ROOT = Path(__file__).resolve().parents[2]
EXTENSIONS = ROOT / "extensions"


def _operations(manifest):
    if isinstance(manifest.get("operation"), dict):
        yield "operation", manifest["operation"]
    for name, operation in (manifest.get("operations") or {}).items():
        yield name, operation


def _all_operations(path, manifest):
    yield from _operations(manifest)
    for dropin in sorted((path.parent / "crons").glob("*.cron.json")):
        yield dropin.name.removesuffix(".cron.json"), json.loads(
            dropin.read_text(encoding="utf-8"))


def test_every_first_party_model_operation_has_a_valid_contract_and_fixture():
    found = 0
    for path in sorted(EXTENSIONS.glob("okengine.*/extension.yaml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, operation in _all_operations(path, manifest):
            if not operation.get("prompt_file"):
                continue
            found += 1
            where = f"{manifest['id']}:{name}"
            assert not validate(operation.get("output_contract"), where), where
            fixtures = operation.get("adversarial_fixtures")
            assert isinstance(fixtures, list) and fixtures, where
            for fixture in fixtures:
                assert (ROOT / fixture).is_file(), f"{where}: missing {fixture}"
    assert found >= 24


def test_model_contract_wildcards_are_limited_to_domain_generic_maintenance():
    permitted = {"okengine.completeness", "okengine.dedupe", "okengine.grounding"}
    for path in sorted(EXTENSIONS.glob("okengine.*/extension.yaml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, operation in _all_operations(path, manifest):
            contract = operation.get("output_contract") or {}
            if "*" in contract.get("allowed_namespaces", []) or "*" in contract.get("allowed_types", []):
                assert manifest["id"] in permitted, f"{manifest['id']}:{name}"


# okengine#878: batch-triage lanes whose correct outcome is often "defer everything" are judged
# per selected item. Under `completion: run` the runner fails a run with zero verified writes, so a
# correctly dispositioned all-deferred batch recorded as a failure. Pinned here so a lane cannot
# silently drift back to whole-run completion.
BATCH_TRIAGE_LANES = {
    ("okengine.predictions", "candidate-watch"),
    ("okengine.predictions", "grade"),
    ("okengine.predictions", "regrade"),
    ("okengine.predictions", "prediction-schema-drain"),
    ("okengine.predictions", "prediction-falsification-search"),
    ("okengine.completeness", "gap-drain"),
}


def test_batch_triage_lanes_are_per_selected_item_with_manifest_writing_selectors():
    seen = set()
    for ext_id, op in sorted(BATCH_TRIAGE_LANES):
        path = EXTENSIONS / ext_id / "extension.yaml"
        operation = yaml.safe_load(path.read_text(encoding="utf-8"))["operations"][op]
        assert operation["output_contract"]["completion"] == "per-selected-item", f"{ext_id}:{op}"
        selector = (path.parent / operation["entrypoint"]).read_text(encoding="utf-8")
        assert "write_selection_manifest(" in selector, f"{ext_id}:{op} writes no manifest"
        assert "print_receipt_template(" in selector, f"{ext_id}:{op} prints no receipt"
        seen.add((ext_id, op))
    assert seen == BATCH_TRIAGE_LANES
