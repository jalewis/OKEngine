"""Keep test doubles bound to the production contracts they represent."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _has_keyword(call: ast.Call, *names: str) -> bool:
    return any(keyword.arg in names for keyword in call.keywords)


def _aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "unittest.mock":
                    aliases[item.asname or "unittest"] = (
                        "unittest.mock" if item.asname else "unittest")
        elif isinstance(node, ast.ImportFrom) and node.module in {"unittest", "unittest.mock"}:
            for item in node.names:
                target = f"{node.module}.{item.name}"
                aliases[item.asname or item.name] = target
    return aliases


def _canonical(name: str, aliases: dict[str, str]) -> str:
    head, separator, tail = name.partition(".")
    replacement = aliases.get(head)
    return f"{replacement}.{tail}" if replacement and separator else replacement or name


def _bare_double(call: ast.Call, aliases: dict[str, str]) -> bool:
    name = _canonical(_name(call.func), aliases)
    leaf = name.rsplit(".", 1)[-1]
    if leaf in {"Mock", "MagicMock", "AsyncMock", "NonCallableMock"}:
        return not _has_keyword(call, "spec", "spec_set")
    if name.endswith("patch.dict"):
        return False
    if name.endswith("patch.object"):
        # A third positional argument or explicit new= is a concrete fake/value,
        # not a generated mock whose interface needs an autospec.
        explicit_new = len(call.args) >= 3 or _has_keyword(call, "new")
        return not explicit_new and not _has_keyword(call, "autospec", "spec", "spec_set")
    if leaf == "patch":
        explicit_new = len(call.args) >= 2 or _has_keyword(call, "new")
        return not explicit_new and not _has_keyword(call, "autospec", "spec", "spec_set")
    return False


def test_generated_mocks_are_bound_to_real_interfaces():
    violations: list[str] = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _bare_double(node, aliases):
                violations.append(f"{path.relative_to(REPO)}:{node.lineno} {_name(node.func)}")
    assert not violations, (
        "generated mocks must use autospec=True/create_autospec or spec/spec_set; "
        "concrete new= fakes and patch.dict are exempt:\n" + "\n".join(violations))


@pytest.mark.parametrize("source", [
    "from unittest.mock import patch as p\np('module.fn')",
    "from unittest import mock as m\nm.patch.object(module, 'fn')",
    "from unittest.mock import MagicMock as MM\nMM()",
])
def test_detector_catches_aliased_bare_doubles(source):
    tree = ast.parse(source)
    aliases = _aliases(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert any(_bare_double(call, aliases) for call in calls)


@pytest.mark.parametrize("source", [
    "from unittest.mock import patch\npatch('module.fn', autospec=True)",
    "from unittest.mock import patch\npatch.object(module, 'flag', False)",
    "from unittest.mock import patch\npatch.dict(mapping, {'key': 'value'})",
    "from unittest.mock import MagicMock\nMagicMock(spec_set=Interface)",
])
def test_detector_accepts_contract_bound_or_concrete_doubles(source):
    tree = ast.parse(source)
    aliases = _aliases(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(_bare_double(call, aliases) for call in calls)
