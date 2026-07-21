import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "scripts/cron/source_connector.py"
EXAMPLES = REPO / "examples/source-connectors"


def _load():
    sys.modules.pop("source_connector", None)
    spec = importlib.util.spec_from_file_location("source_connector", RUNTIME)
    module = importlib.util.module_from_spec(spec)
    sys.modules["source_connector"] = module
    spec.loader.exec_module(module)
    return module


CASES = [
    ("github-status-incidents", {}, 2, {"inc-001", "inc-002"}),
    ("federal-register-documents", {}, 2, {"2026-15001", "2026-15002"}),
    ("sec-company-submissions",
     {"cik": "0000320193", "user_agent": "Fixture Research fixture@example.org"},
     1, {"0000320193"}),
]


@pytest.mark.parametrize(("name", "inputs", "count", "native_ids"), CASES)
def test_reference_connector_is_conformant_idempotent_and_observable(
        tmp_path, name, inputs, count, native_ids):
    runtime = _load()
    manifest = yaml.safe_load((EXAMPLES / f"{name}.yaml").read_text())
    assert runtime.validate_manifest(manifest) == []
    kwargs = {
        "inputs": inputs,
        "state_root": tmp_path / "state",
        "archive_root": tmp_path / "archive",
        "health_root": tmp_path / "health",
        "ledger_root": tmp_path / "ledger",
        "fixture": EXAMPLES / "fixtures" / f"{name}.fixture.json",
        "observed_at": "2026-07-18T12:00:00Z",
        "sleep": lambda _seconds: None,
    }
    first = runtime.execute(manifest, **kwargs)
    second = runtime.execute(manifest, **kwargs)
    assert first["records"] == count and first["new_revisions"] == count
    assert second["new_revisions"] == 0
    assert {row["source_native_id"] for row in first["items"]} == native_ids
    assert all(row["source_authority"] == manifest["trust"]["source_authority"]
               and row["license"] == manifest["license"] for row in first["items"])
    checkpoint = tmp_path / "state" / manifest["checkpoint"]["path"]
    health = tmp_path / "health" / manifest["health"]["path"]
    assert checkpoint.is_file() and json.loads(health.read_text())["ok"]
    attempts = [json.loads(line) for path in (tmp_path / "ledger").glob("attempts-*.ndjson")
                for line in path.read_text().splitlines()]
    assert len(attempts) == 2 and all(row["outcome"] == "success" for row in attempts)
    assert manifest["rate_limit"]["max_requests"] >= 1


def test_examples_are_zero_seed_and_contain_no_credentials():
    assert not (EXAMPLES / "domain-crons.json").exists()
    for path in EXAMPLES.glob("*.yaml"):
        text = path.read_text()
        assert "schedule:" not in text and "Authorization:" not in text
        manifest = yaml.safe_load(text)
        assert manifest["auth"]["secret_refs"] == {}
        assert manifest["license"]["url"].startswith("https://")
