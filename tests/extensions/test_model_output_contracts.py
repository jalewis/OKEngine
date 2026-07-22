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
