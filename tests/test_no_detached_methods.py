"""A method must not drift out of its class (okengine#816 aftermath).

Inserting a module-level `def` into a class body silently ends the class: every method below it
becomes a module-level function that still takes `self`, so callers get
`TypeError: f() missing 1 required positional argument` at runtime — or, worse, nothing at all
until that path is exercised. It happened here while editing framework_validate_metadata.py, and
`ast.parse` was perfectly happy: the file was valid Python the whole time, just not the class it
used to be. "It parses" is not "it is intact".

A module-level function whose first parameter is `self` (or `cls`) is that fingerprint and has no
legitimate use, so it is a cheap, exact detector for the whole tree.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("scripts", "tools", "okengine-mcp", "okengine-reader", "okengine-cockpit",
         "okengine-projection", "okengine-operations", "extensions", "migrations", "ci", "src")


def _python_files():
    for root in ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


class Unparseable(Exception):
    """A file this guard could not read is UNDETECTABLE, never clean."""


def _detached(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    except SyntaxError as exc:
        # Returning [] here would make every unparseable file silently pass — the exact shape of
        # vacuous pass this guard exists to prevent. Syntax has its own gate; this one says so out
        # loud rather than reporting clean.
        raise Unparseable(f"{path}: {exc}") from exc
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args.posonlyargs + node.args.args
        if args and args[0].arg in ("self", "cls"):
            out.append(f"{node.name} (line {node.lineno}, first arg {args[0].arg!r})")
    return out


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_no_module_level_function_takes_self(path):
    try:
        detached = _detached(path)
    except Unparseable as exc:
        pytest.fail(f"cannot verify class structure: {exc}")
    assert not detached, (
        f"{path.relative_to(REPO)} has module-level function(s) taking self/cls: {detached}. "
        "A method has drifted out of its class — most likely a module-level def was inserted into "
        "the class body, which silently ends it."
    )


def test_the_detector_sees_a_detached_method(tmp_path):
    """A parametrized scan that matched nothing would pass vacuously; prove the matcher works."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "class C:\n    def kept(self):\n        return 1\n\n\n"
        "def helper():\n    return 2\n\n\n"
        "def drifted(self, r):\n    return 3\n", encoding="utf-8")
    found = _detached(probe)
    assert len(found) == 1 and found[0].startswith("drifted"), found
    assert list(_python_files()), "no files scanned — the guard would pass on an empty tree"


def test_an_unparseable_file_is_undetectable_not_clean(tmp_path):
    probe = tmp_path / "broken.py"
    probe.write_text("class C:\ndef oops(:\n", encoding="utf-8")
    with pytest.raises(Unparseable):
        _detached(probe)
