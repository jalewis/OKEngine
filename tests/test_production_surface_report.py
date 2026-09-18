"""Machine-readable coverage and typing inventory for production Python."""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _tracked_python() -> list[str]:
    return subprocess.run(
        ["git", "ls-files", "--", "*.py"], cwd=ROOT, check=True,
        capture_output=True, text=True).stdout.splitlines()


def production_surface() -> dict:
    measured_roots = set(CONFIG["tool"]["coverage"]["run"]["source"])
    intentional_exclusions = {
        "tests": "test code", "patches": "externally installed carried patches",
        "templates": "unrendered scaffolding", "ci": "gate implementation debt #603",
    }
    overlay_files = [path for path in _tracked_python()
                     if path.split("/", 1)[0] == "overlays"]
    if overlay_files:
        manifest = yaml.safe_load((ROOT / "engine-manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["runtime"]["pinned_version"] == "v0.21.3", (
            "Hermes overlay exclusion is valid only for the separately instrumented v0.21.3 target"
        )
        assert all(path.startswith("overlays/hermes-v2026.9.14/") for path in overlay_files), (
            "unexpected overlay Python cannot inherit the v0.21.3 target-contract exemption"
        )
        intentional_exclusions["overlays"] = (
            "external Hermes v0.21.3 overlay measured by hermes-target-contracts"
        )
    production = [path for path in _tracked_python()
                  if path.split("/", 1)[0] not in intentional_exclusions]
    measured = [path for path in production if path.split("/", 1)[0] in measured_roots]
    typed = set(CONFIG["tool"]["mypy"]["files"])
    return {
        "schema_version": 1,
        "production_files": len(production),
        "coverage_measured_files": len(measured),
        "coverage_unmeasured_files": sorted(set(production) - set(measured)),
        "typed_files": len(set(production) & typed),
        "fully_typed_service_roots": CONFIG["tool"]["okengine"]["typing"][
            "fully_typed_service_roots"],
        "intentional_exclusions": intentional_exclusions,
    }


def test_all_production_files_are_in_the_coverage_boundary():
    report = production_surface()
    assert report["production_files"] == report["coverage_measured_files"]
    assert report["coverage_unmeasured_files"] == []


def test_target_overlay_exemption_rejects_an_unmeasured_runtime_generation():
    with patch.object(yaml, "safe_load", autospec=True,
                      return_value={"runtime": {"pinned_version": "v0.22.0"}}):
        with pytest.raises(AssertionError, match="separately instrumented"):
            production_surface()


def test_fully_typed_service_roots_are_an_exhaustive_ratchet():
    typed = set(CONFIG["tool"]["mypy"]["files"])
    roots = CONFIG["tool"]["okengine"]["typing"]["fully_typed_service_roots"]
    service_modules = {path for path in _tracked_python()
                       if path.split("/", 1)[0] in roots and "/tests/" not in path}
    assert service_modules <= typed, (
        "new production modules in fully typed services must enter blocking mypy: "
        f"{sorted(service_modules - typed)}")


def test_surface_report_is_json_serializable_and_counts_both_live_services():
    report = json.loads(json.dumps(production_surface()))
    assert report["typed_files"] >= 5
    assert report["fully_typed_service_roots"] == [
        "okengine-operations", "okengine-projection"]
