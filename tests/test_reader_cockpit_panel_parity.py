"""Reader/cockpit extension-panel rendering is one cross-surface contract."""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _function(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.dump(node, include_attributes=False)


def test_reader_and_cockpit_type_bound_panel_renderers_are_identical():
    """Manifest validation assumes both UIs implement the same bindable panel kinds."""
    reader = REPO / "okengine-reader" / "app.py"
    cockpit = REPO / "src" / "okengine" / "cockpit_services" / "page_metadata.py"
    assert _function(reader, "_panel_for") == _function(cockpit, "_panel_for")
