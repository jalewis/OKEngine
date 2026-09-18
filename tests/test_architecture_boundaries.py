import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "architecture_boundaries", ROOT / "ci/architecture_boundaries.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_repository_boundaries_are_acyclic_and_ratcheted():
    assert MODULE.validate(ROOT) == []


def test_gate_rejects_entrypoint_growth(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "entry.py").write_text("one\ntwo\n")
    policy = {
        "schema_version": 1,
        "entrypoint_budgets": {"entry.py": {"maximum_lines": 1, "target_lines": 0}},
        "layers": ["transport", "storage"],
        "allowed_dependencies": {"transport": ["storage"], "storage": []},
    }
    (tmp_path / "config/architecture-boundaries.json").write_text(json.dumps(policy))
    assert any("exceeds ratchet" in error for error in MODULE.validate(tmp_path))


def test_gate_accepts_an_achieved_target_as_the_enforced_ceiling(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "entry.py").write_text("one\n")
    policy = {
        "schema_version": 1,
        "entrypoint_budgets": {"entry.py": {"maximum_lines": 1, "target_lines": 1}},
        "layers": ["transport", "storage"],
        "allowed_dependencies": {"transport": ["storage"], "storage": []},
    }
    (tmp_path / "config/architecture-boundaries.json").write_text(json.dumps(policy))
    assert MODULE.validate(tmp_path) == []


def test_gate_rejects_extracted_service_growth(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "service.py").write_text("one\ntwo\n")
    policy = {
        "schema_version": 1,
        "entrypoint_budgets": {},
        "module_budgets": {"service.py": {"maximum_lines": 1, "target_lines": 1}},
        "layers": ["domain"],
        "allowed_dependencies": {"domain": []},
    }
    (tmp_path / "config/architecture-boundaries.json").write_text(json.dumps(policy))
    assert any("service.py" in error for error in MODULE.validate(tmp_path))


def test_gate_rejects_dependency_cycles(tmp_path):
    (tmp_path / "config").mkdir()
    policy = {
        "schema_version": 1, "entrypoint_budgets": {},
        "layers": ["domain", "storage", "transport"],
        "allowed_dependencies": {
            "domain": ["storage"], "storage": ["transport"], "transport": ["storage"]},
    }
    (tmp_path / "config/architecture-boundaries.json").write_text(json.dumps(policy))
    assert any("dependency cycle" in error for error in MODULE.validate(tmp_path))


def test_gate_rejects_target_above_ratchet_and_incomplete_graph(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "entry.py").write_text("one\n")
    policy = {
        "entrypoint_budgets": {
            "entry.py": {"maximum_lines": 1, "target_lines": 2}},
        "layers": ["domain", "storage"],
        "allowed_dependencies": {"domain": []},
    }
    (tmp_path / "config/architecture-boundaries.json").write_text(json.dumps(policy))
    errors = MODULE.validate(tmp_path)
    assert "entry.py: target must not exceed the current ratchet" in errors
    assert "dependency graph must define every declared layer exactly once" in errors


def test_cli_reports_success_and_failure(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "validate", lambda root: [])
    assert MODULE.main() == 0
    assert "dependency DAG valid" in capsys.readouterr().out
    monkeypatch.setattr(MODULE, "validate", lambda root: ["broken"])
    assert MODULE.main() == 1
    assert "ERROR: broken" in capsys.readouterr().out
