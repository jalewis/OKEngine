"""Release version declarations must remain one coherent public contract."""
from __future__ import annotations

import ast
from pathlib import Path

import tomllib
import yaml


REPO = Path(__file__).resolve().parents[1]


def _assigned_string(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                return node.value.value
    raise AssertionError(f"{path.relative_to(REPO)} does not declare {name}")


def test_all_release_version_declarations_match():
    manifest = yaml.safe_load((REPO / "engine-manifest.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    expected = str(manifest["engine_release"]).removeprefix("v")

    assert project["project"]["version"] == expected
    assert _assigned_string(REPO / "src/okengine/__init__.py", "__version__") == expected
    assert _assigned_string(REPO / "scripts/build_engine_wheel.py", "VERSION") == expected
