"""okengine#1065: under --strict-layer-skips, an xfail is either honest or a counted failure.

The blocking-layer hook in tests/conftest.py turns a skipped report into a failed one. pytest
reports an expected xfail as `skipped` with `wasxfail` set, and the hook used to flip only the
outcome. pytest's session does not count a failed report that carries `wasxfail`, and JUnit writes
it as "xfail-marked test passes unexpectedly". So security-properties job 176029 printed "1 failed"
and exited 0. These tests run the REAL conftest in disposable sessions and read the exit status,
which is what a CI lane trusts.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]

_FIXTURE = '''
import pytest


@pytest.mark.unit
@pytest.mark.xfail(strict=True, reason="known defect")
def test_strict_xfail_that_fails():
    assert 1 == 2


@pytest.mark.unit
@pytest.mark.xfail(strict=True, reason="known defect, now fixed")
def test_strict_xfail_that_passes():
    assert 1 == 1


@pytest.mark.unit
@pytest.mark.xfail(reason="non-strict")
def test_non_strict_xfail_that_fails():
    assert 1 == 2


@pytest.mark.unit
@pytest.mark.xfail(reason="non-strict")
def test_non_strict_xfail_that_passes():
    assert 1 == 1


@pytest.mark.unit
def test_imperative_xfail():
    pytest.xfail("imperative")


@pytest.mark.unit
@pytest.mark.xfail(strict=True, reason="strict marker, imperative call")
def test_imperative_xfail_under_a_strict_marker():
    pytest.xfail("imperative")


@pytest.mark.unit
@pytest.mark.xfail(strict=True, run=False, reason="never run")
def test_strict_xfail_not_run():
    assert 1 == 2


@pytest.mark.unit
def test_plain_skip():
    pytest.skip("no capability")


@pytest.mark.unit
def test_plain_pass():
    assert True
'''


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("xfail")
    (root / "tests").mkdir()
    shutil.copy(REPO / "tests" / "conftest.py", root / "tests" / "conftest.py")
    (root / "tests" / "test_fixture.py").write_text(_FIXTURE, encoding="utf-8")
    return root


def _session(root: Path, name: str, *, strict: bool = True) -> tuple[int, str, ET.Element]:
    """Run one fixture test; return the exit status, the output and its JUnit testcase."""
    junit = root / f"{name}-{strict}.xml"
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTEST_ADDOPTS", "OKENGINE_CI_TEST_TMPFS"}}
    argv = [sys.executable, "-m", "pytest", f"tests/test_fixture.py::{name}", "-q", "-rA",
            "-p", "no:cacheprovider", "-p", "no:randomly", "--import-mode=importlib",
            f"--junitxml={junit}"]
    if strict:
        argv.append("--strict-layer-skips")
    result = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, timeout=120)
    case = next(ET.parse(junit).getroot().iter("testcase"))
    return result.returncode, result.stdout + result.stderr, case


def _junit_outcome(case: ET.Element) -> str:
    for tag in ("failure", "error", "skipped"):
        if case.find(tag) is not None:
            return tag
    return "passed"


def test_an_honest_strict_xfail_stays_an_xfail_in_a_blocking_layer(root):
    rc, out, case = _session(root, "test_strict_xfail_that_fails")
    assert rc == 0, f"an honest strict xfail must not fail the lane:\n{out}"
    assert "1 xfailed" in out, out
    assert case.find("skipped") is not None and "passes unexpectedly" not in ET.tostring(
        case, encoding="unicode"), "JUnit must record an xfail, not an unexpected pass"


@pytest.mark.parametrize("strict", [True, False], ids=["blocking-layer", "plain"])
def test_a_strict_xpass_fails_the_run_in_every_lane(root, strict):
    rc, out, case = _session(root, "test_strict_xfail_that_passes", strict=strict)
    assert rc == 1, f"a strict XPASS must fail the run (strict-layer-skips={strict}):\n{out}"
    assert _junit_outcome(case) == "failure", ET.tostring(case, encoding="unicode")


@pytest.mark.parametrize("name", [
    "test_non_strict_xfail_that_fails",
    "test_non_strict_xfail_that_passes",
    "test_imperative_xfail",
    "test_imperative_xfail_under_a_strict_marker",
    "test_strict_xfail_not_run",
    "test_plain_skip",
])
def test_every_other_skip_or_xfail_is_a_counted_failure_in_a_blocking_layer(root, name):
    """The 176029 defect: the summary said "failed" but the exit status was 0. Both must agree."""
    rc, out, case = _session(root, name)
    assert rc == 1, f"{name} was reported but not counted as a failure:\n{out}"
    # `run=False` is reported at setup, which pytest counts as an error rather than a failure.
    assert "1 failed" in out or "1 error" in out, out
    assert _junit_outcome(case) in {"failure", "error"}, ET.tostring(case, encoding="unicode")
    assert "passes unexpectedly" not in ET.tostring(case, encoding="unicode")


def test_a_plain_pass_is_untouched(root):
    rc, out, case = _session(root, "test_plain_pass")
    assert rc == 0 and _junit_outcome(case) == "passed", out
