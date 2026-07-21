"""The operator list must report the same effective state the deploy composer uses."""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name):
    path = REPO / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_list_json_marks_default_on_core_as_effectively_enabled(tmp_path, capsys):
    cli = _load("framework_extensions")
    assert cli.main(["list", str(tmp_path), "--json"]) == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["extensions"]}
    contradiction = rows["okengine.contradictions"]
    assert contradiction["enabled"] is True
    assert contradiction["explicitly_enabled"] is False
    assert contradiction["core"] is True
    assert contradiction["state"] == "enabled (core default)"


def test_list_shows_explicit_core_opt_out(tmp_path, capsys):
    discovery = _load("extension_discovery")
    cli = _load("framework_extensions")
    assert discovery.set_enabled(tmp_path, "okengine.contradictions", False) == []
    assert cli.main(["list", str(tmp_path), "--json"]) == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["extensions"]}
    contradiction = rows["okengine.contradictions"]
    assert contradiction["enabled"] is False
    assert contradiction["state"] == "disabled (core opt-out)"


def test_strict_validation_is_clean_for_first_party_tree(tmp_path):
    cli = _load("framework_extensions")
    assert cli.main(["validate", str(tmp_path), "--strict-warnings", "--quiet"]) == 0
