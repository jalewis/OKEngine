"""Cockpit consumes policy artifacts; it does not re-derive policy decisions."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "okengine-cockpit" / "app.py"


def _load(vault, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_policy_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_policy_app", APP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_policy_api_reads_structured_runtime_artifacts(tmp_path, monkeypatch):
    state = tmp_path / ".okengine"
    state.mkdir()
    (state / "effective-policy.json").write_text(json.dumps({
        "digest": "abc", "rules": [{"id": "r1"}],
        "capabilities": {"cron:x": {"body": "deny"}}, "waivers": [],
    }))
    (state / "policy-coverage.json").write_text(json.dumps({
        "generated_at": "2026-07-18T12:00:00Z", "rules": [{"rule_id": "r1", "covered": True}],
    }))
    (state / "policy-findings.json").write_text(json.dumps({
        "generated_at": "2026-07-18T13:00:00Z",
        "findings": [{"rule_id": "r1", "outcome": "reject"}],
    }))
    out = _load(tmp_path, monkeypatch).api_policy()
    assert out["digest"] == "abc"
    assert out["rules"][0]["id"] == "r1"
    assert out["coverage"][0]["covered"] is True
    assert out["findings"][0]["outcome"] == "reject"
    assert out["generated_at"] == "2026-07-18T13:00:00Z"


def test_policy_api_is_explicitly_empty_before_first_materialization(tmp_path, monkeypatch):
    assert _load(tmp_path, monkeypatch).api_policy() == {
        "digest": None, "rules": [], "capabilities": {}, "waivers": [],
        "coverage": [], "findings": [], "generated_at": None,
    }
