import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("product_boundary", ROOT / "ci/product_boundary_gate.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_accepted_product_boundary_is_complete():
    assert MODULE.validate(ROOT) == []


def test_gate_rejects_runtime_reclassification(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "docs/design").mkdir(parents=True)
    policy = json.loads((ROOT / "config/product-boundary.json").read_text())
    policy["runtime"]["hermes"] = "product-kernel"
    (tmp_path / "config/product-boundary.json").write_text(json.dumps(policy))
    (tmp_path / "docs/design/product-boundary.md").write_text(
        (ROOT / "docs/design/product-boundary.md").read_text())
    assert "Hermes must be classified as the execution adapter" in MODULE.validate(tmp_path)


def _write_product_fixture(tmp_path, policy, text=""):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "docs/design").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config/product-boundary.json").write_text(json.dumps(policy))
    (tmp_path / "docs/design/product-boundary.md").write_text(text)


def test_gate_reports_every_policy_and_document_drift(tmp_path):
    policy = {
        "schema_version": 2, "decision": "different", "kernel": [],
        "runtime": {}, "consumers": [], "adapter_contract_version": 0,
        "non_goals": ["one"],
    }
    _write_product_fixture(tmp_path, policy)
    errors = MODULE.validate(tmp_path)
    assert len(errors) == 13
    assert "schema_version must be 1" in errors
    assert "primary product decision is missing or changed" in errors
    assert "stable kernel classification drifted" in errors
    assert "adapter_contract_version must be a positive integer" in errors
    assert any("missing section" in error for error in errors)


def test_gate_fails_closed_on_unreadable_inputs(tmp_path):
    assert "product boundary unreadable" in MODULE.validate(tmp_path)[0]
    (tmp_path / "config").mkdir()
    policy = json.loads((ROOT / "config/product-boundary.json").read_text())
    (tmp_path / "config/product-boundary.json").write_text(json.dumps(policy))
    assert "product boundary document unreadable" in MODULE.validate(tmp_path)[0]


def test_cli_reports_success_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "validate", lambda root: [])
    assert MODULE.main() == 0
    assert "accepted decision" in capsys.readouterr().out
    monkeypatch.setattr(MODULE, "validate", lambda root: ["broken"])
    assert MODULE.main() == 1
    assert "ERROR: broken" in capsys.readouterr().out
