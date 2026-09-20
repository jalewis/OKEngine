"""Nothing checked the coverage source list against the repository it claims to cover.

`fail_under = 100` reads as *all of it*. It means *all of the seven directories somebody
remembered to list*. Two live services and the whole CI gate machinery were never in that list,
so `okengine-projection/service.py` sat at 44% while the gate reported 100.00% — and a written
claim that the file was fully covered survived three merge requests, because the number that
would have contradicted it was never computed.

That is the failure mode worth naming: a file outside the source list does not report low
coverage. It reports *nothing*, and nothing looks exactly like a file with nothing to report.
Same shape as an empty parse read as "nothing happened" — the absence of a measurement is
`unknown`, never `pass`.

So this is the standing detector for the class. Every directory in the repository holding
non-test Python is either measured by the floor, or carries a written reason here for why it
cannot be. A service tree added next month fails this test on the day it appears, instead of
being found by someone reading a coverage report for an unrelated reason.

The exemptions are deliberately *loud*: a reason is required, it must survive review as a
sentence, and a stale one fails just as hard as a missing one — an exemption for a directory that
no longer exists is a claim nobody has re-examined.
"""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"


# Directories of non-test Python the 100% floor does NOT measure, each with the reason.
# Adding a key here is the deliberate act; leaving one out is what this file exists to catch.
UNMEASURED_TREES: dict[str, str] = {
    "tests": (
        "the suite itself. Measuring it scores the tests rather than the code, and a test that "
        "never runs is caught by collection, not by coverage."
    ),
    "patches": (
        "carried patches for cron-plus, a PINNED EXTERNAL dependency. These files are copied "
        "into the plugin clone by install-cron-plus.sh and execute inside cron-plus's own "
        "package; importing them here measures a copy that no deployment runs. Their behaviour "
        "is gated by tests/test_patches_registered.py and the cron-plus lane instead."
    ),
    "templates": (
        "the pack skeleton shipped by `framework init`. It is rendered with {{PACK}} substitution "
        "into a NEW repository and runs there, under that pack's own CI — measuring the "
        "unrendered template scores a file that is never executed in this shape."
    ),
    "overlays": (
        "the v0.21.3 Hermes provider overlays are target-only while the production manifest "
        "pins v0.18.2. They execute in the pinned Python 3.13 external Hermes clone, not in "
        "this Python 3.12 engine suite; the exact-target private_ci lane runs their real provider "
        "contracts. This exemption must be retired when the production pin switches."
    ),
}

# Individual files inside a MEASURED tree that the floor cannot reach, each with the reason.
# A per-file exemption is a bigger lever than a pragma, so it is held to the same standard.
UNMEASURED_FILES: dict[str, str] = {
    "ci/cosmic_http_exec.py": (
        "This three-line process adapter imports Cosmic Ray, which exists only inside the mutation "
        "job image, and delegates directly to Cosmic Ray's HTTP executor without engine behavior."
    ),
    "ci/postgres_projection_integration.py": (
        "This module is itself the live PostgreSQL integration test program; measuring the test "
        "would describe which assertions executed, not coverage of the projection service it tests."
    ),
}


def measured_roots() -> list[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["coverage"]["run"]["source"]


def python_trees() -> set[str]:
    """Every top-level directory holding at least one tracked `.py` file.

    Tracked, not globbed: an untracked scratch file is not part of the repository's surface, and
    a generated tree under a gitignore should not be able to fail this.
    """
    listing = subprocess.run(["git", "ls-files", "--", "*.py"], cwd=REPO, check=True,
                             capture_output=True, text=True).stdout.split()
    return {name.split("/")[0] for name in listing if "/" in name}


def test_every_tree_of_python_is_measured_or_has_a_written_exemption():
    """The gap that let a 44% file be reported as complete: it was in no list at all."""
    unaccounted = python_trees() - set(measured_roots()) - set(UNMEASURED_TREES)
    assert not unaccounted, (
        f"{sorted(unaccounted)} hold Python that the 100% coverage floor never looks at, so they "
        f"report no number rather than a low one. Add them to `tool.coverage.run.source` in "
        f"pyproject.toml and bring them to the floor, or record why they cannot be measured in "
        f"UNMEASURED_TREES here."
    )


def test_an_exemption_for_a_tree_that_no_longer_exists_is_removed():
    """A stale exemption is a claim nobody has re-read. It should fail like a missing one."""
    trees = python_trees()
    stale = [name for name in UNMEASURED_TREES if name not in trees]
    assert not stale, (
        f"{stale} no longer hold Python — delete the exemption rather than leaving a standing "
        f"licence for a directory to come back unmeasured"
    )


def test_no_tree_is_both_measured_and_exempted():
    """Two answers to the same question is a config nobody can reason about."""
    contradiction = set(measured_roots()) & set(UNMEASURED_TREES)
    assert not contradiction, f"{sorted(contradiction)} are listed as both measured and exempt"


def test_every_exemption_states_a_reason_a_reviewer_can_disagree_with():
    """`# not testable` is not a reason. The point of writing it down is that someone can push
    back on it, which requires enough words to push back against."""
    for mapping, label in ((UNMEASURED_TREES, "tree"), (UNMEASURED_FILES, "file")):
        for name, reason in mapping.items():
            assert len(reason.split()) >= 12, (
                f"the {label} exemption for {name} is too thin to review: {reason!r}"
            )


def file_exemption_faults(names, roots) -> list[str]:
    """Why each named file-level exemption is not usable, if it isn't.

    A per-file exemption for a file inside an already-exempt tree is noise; one for a file that
    does not exist is a rule guarding nothing.
    """
    faults = []
    for name in names:
        if not (REPO / name).is_file():
            faults.append(f"{name} is exempted but does not exist")
        elif name.split("/")[0] not in roots:
            faults.append(f"{name} sits outside every measured tree, so the exemption is redundant")
    return faults


def test_exempted_files_are_real_and_sit_inside_a_measured_tree():
    assert file_exemption_faults(UNMEASURED_FILES, measured_roots()) == []


def test_the_file_exemption_check_can_actually_fail():
    """UNMEASURED_FILES is empty today, so the check above passes over an empty loop — a green
    result that proves nothing until someone adds an entry. Exercise the rule on synthetic input
    so the mechanism is known to work BEFORE it is first relied on."""
    roots = measured_roots()
    real = f"{roots[0]}/__does_not_exist__.py"
    assert file_exemption_faults([real], roots) == [
        f"{real} is exempted but does not exist"]
    inside_an_exempt_tree = Path(__file__).relative_to(REPO).as_posix()
    assert file_exemption_faults([inside_an_exempt_tree], roots) == [
        f"{inside_an_exempt_tree} sits outside every measured tree, so the exemption is redundant"]


def test_the_omit_list_carries_exactly_the_structural_patterns_and_the_named_files():
    """`omit` is the widest lever in the config — one pattern can exempt a whole subtree silently.
    Every non-structural entry must be a file this module has a reason for."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    omit = set(config["tool"]["coverage"]["run"].get("omit", []))
    structural = {"*/tests/*", "*/__pycache__/*"}
    named = omit - structural
    assert named == set(UNMEASURED_FILES), (
        f"pyproject omits {sorted(named)} but UNMEASURED_FILES documents "
        f"{sorted(UNMEASURED_FILES)} — every omitted file needs its reason recorded here"
    )
    assert structural <= omit, f"the structural omit patterns were dropped: {sorted(omit)}"
