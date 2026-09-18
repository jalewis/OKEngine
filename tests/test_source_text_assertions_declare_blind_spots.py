"""A test that reads a script's TEXT must say what it cannot see (okengine#602, item 3).

Asserting that a substring appears in a shell script is a real check — it catches a deleted line —
and for a script that needs a live Docker stack it is sometimes the only check available offline.
It is also weak in a specific, demonstrated way: **it cannot tell "the check is gone" from "the
check moved", and it cannot see whether the guard RUNS, is reached, or does anything.**

Both halves of that have already cost real work:

* `tests/test_post_deploy_verify.py` pinned the cockpit surface as the literal
  `'grep -Fxq "$COCKPIT"'`. Folding three duplicated `docker compose config` probes into one
  `has_service()` helper — strictly better behaviour — made the test fail for a change that
  *improved* the thing it guarded.
* A pack conformance test asserted `"frontier" not in rail`, written when frontier-watch was
  optional. The pack later made it a hard requirement, and the literal outlived its reason,
  forbidding a reference that had become correct. Nothing flagged it, because a string assertion
  stays green until somebody edits the string.

Both were caught by a human reading the test during unrelated work, which is the wrong detection
mechanism. #602's third acceptance item is that the remaining ones are *converted or annotated
with what they cannot detect*.

Prose annotation alone would rot the same way the assertions do, so this makes it enforceable: a
test that reads a script's source must carry a `CANNOT DETECT:` line in its docstring. The
requirement is not paperwork — writing that line forces the author to state the gap at the moment
they still know it, and it tells the next reader what a green result is *not* evidence of.

Behavioural tests are exempt by construction: they do not read source, so they never match.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MARKER = "CANNOT DETECT:"

# Test modules that guard shell scripts. A file joins this list when it starts asserting on script
# text; the point is that the rule is applied somewhere specific rather than aspirationally
# everywhere.
GUARDED = (
    "tests/test_post_deploy_verify.py",
    "tests/test_post_deploy_verify_behaviour.py",
    "tests/test_deploy_ownership_hardening.py",
    "tests/test_deploy_preserves_pause.py",
    "tests/test_deploy_exit_contract.py",
)

def script_constants(tree: ast.Module, text: str) -> set[str]:
    """Module-level names bound to a path inside the engine's script trees.

    Reading a file is not the thing being ruled on — reading a SCRIPT is. A behavioural test that
    inspects an artifact the script produced (the `.env` a deploy pinned, a receipt a lane wrote)
    is evidence about behaviour and must not be swept up. Anchoring on the constants keeps the two
    apart; the first draft matched any `.read_text(` and flagged exactly that case.
    """
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        segment = ast.get_source_segment(text, node) or ""
        if "REPO" in segment and ("scripts" in segment or "tests/e2e" in segment):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def source_reading_tests(relative: str) -> list[tuple[str, str | None]]:
    """(test name, docstring) for each test in `relative` that asserts on a SCRIPT's text."""
    path = REPO / relative
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    constants = script_constants(tree, text)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        reads = False
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Attribute) and inner.attr == "read_text"
                    and isinstance(inner.value, ast.Name) and inner.value.id in constants):
                reads = True
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) \
                    and inner.func.id == "_func_body":
                reads = True
        if reads:
            found.append((node.name, ast.get_docstring(node)))
    return found


@pytest.mark.parametrize("relative", GUARDED)
def test_every_source_text_assertion_declares_its_blind_spot(relative):
    undeclared = [
        name for name, doc in source_reading_tests(relative)
        if not doc or MARKER not in doc
    ]
    assert not undeclared, (
        f"{relative}: these assert on a script's TEXT without saying what that cannot detect. A "
        f"substring check proves a line exists — not that it runs, is reached, or has any effect, "
        f"and it cannot distinguish a deleted check from a moved one. Add a `{MARKER} ...` line to "
        f"each docstring, or convert the test to run the script: {undeclared}"
    )


def test_the_rule_applies_to_something():
    """A convention nobody triggers is not enforced. If every guarded module stopped reading source
    this file would silently pass forever, so require that the population is real."""
    total = sum(len(source_reading_tests(relative)) for relative in GUARDED)
    assert total >= 10, (
        f"only {total} source-reading tests found across {len(GUARDED)} modules — either they were "
        f"converted (delete this rule) or the detector stopped recognising them"
    )


def test_the_detector_recognises_a_source_read_and_ignores_a_behavioural_test(tmp_path):
    """A green red-check. The rule is only worth having if it actually distinguishes the two."""
    module = tmp_path / "sample.py"
    module.write_text(
        'SCRIPT = REPO / "scripts" / "thing.sh"\n'
        'def test_reads():\n'
        '    """No marker here."""\n'
        '    assert "x" in SCRIPT.read_text()\n'
        'def test_runs():\n'
        '    """Runs the thing, and reads what it produced."""\n'
        '    assert subprocess.run(["bash", SCRIPT]).returncode == 0\n'
        '    assert "y" in (tmp / "produced.env").read_text()\n',
        encoding="utf-8")
    global REPO
    original, REPO = REPO, tmp_path
    try:
        found = dict(source_reading_tests("sample.py"))
    finally:
        REPO = original
    assert list(found) == ["test_reads"], (
        "the detector must ignore a test that RUNS the script — including when it reads an "
        "artifact the run produced, which is evidence about behaviour, not about source text")
    assert MARKER not in (found["test_reads"] or ""), "an unannotated read must be recognised"
