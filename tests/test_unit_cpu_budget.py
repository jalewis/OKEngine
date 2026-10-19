"""The per-unit CPU budget bills a test for its own work, not for the session's garbage collector.

In the unit layer, the automatic full (generation-2) collection scans the whole session heap and
fires on whichever test's allocations cross the threshold. On MR !1342 that was
``test_a_hostile_name_still_ties_when_a_cited_source_glosses_it`` at 0.25-0.31s CPU in three
pipelines, a test that costs about 0.005s on its own. These tests run the REAL tests/conftest.py in
a disposable pytest session. One fixture test triggers an automatic collection that is made
expensive on purpose; that collection must not be billed to it. The others show that the budget
still rejects real CPU work, including an explicit ``gc.collect()``, and that the collector is
re-enabled afterward.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]

_FIXTURE = '''
import gc
import time

import pytest


def _burn(seconds):
    end = time.process_time() + seconds
    while time.process_time() < end:
        pass


_ARMED = []


def _expensive_collection(phase, info):
    """Stands in for a full collection over a large session heap: CPU spent in the collector."""
    if phase == "start" and _ARMED:
        _ARMED.clear()
        _burn(0.15)


gc.callbacks.append(_expensive_collection)


def _allocate_under_an_armed_collector():
    threshold = gc.get_threshold()
    gc.set_threshold(1, 1, 1)
    _ARMED.append(True)
    try:
        return [[index, [index]] for index in range(2000)]
    finally:
        _ARMED.clear()
        gc.set_threshold(*threshold)


@pytest.mark.unit
def test_an_automatic_collection_is_not_billed():
    assert len(_allocate_under_an_armed_collector()) == 2000


@pytest.mark.unit
def test_real_cpu_work_is_billed():
    _burn(0.15)


@pytest.mark.unit
def test_an_explicit_collection_is_billed():
    _ARMED.append(True)
    try:
        gc.collect()
    finally:
        _ARMED.clear()


@pytest.mark.unit
def test_a_failing_unit_test():
    raise AssertionError("deliberate")


@pytest.mark.integration
def test_the_collector_is_enabled_after_the_unit_tests():
    assert gc.isenabled()


@pytest.mark.integration
def test_an_integration_test_is_not_budgeted():
    _burn(0.15)
'''


@pytest.fixture(scope="module")
def session_output(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("budget")
    (root / "tests").mkdir()
    shutil.copy(REPO / "tests" / "conftest.py", root / "tests" / "conftest.py")
    (root / "tests" / "test_fixture.py").write_text(_FIXTURE, encoding="utf-8")
    # The disposable session has no ci/ helpers, so CI's tmpfs check must not follow it in.
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTEST_ADDOPTS", "OKENGINE_CI_TEST_TMPFS"}}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-rA", "--strict-layer-skips",
         "-p", "no:cacheprovider", "-p", "no:randomly", "--import-mode=importlib"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120)
    return result.stdout + result.stderr


def _outcome(output: str, name: str) -> str:
    for line in output.splitlines():
        outcome, _, rest = line.partition(" ")
        if outcome in {"PASSED", "FAILED", "ERROR"} and (
                rest == f"tests/test_fixture.py::{name}"
                or rest.startswith(f"tests/test_fixture.py::{name} ")):
            return outcome
    raise AssertionError(f"{name} has no outcome in the session:\n{output}")


def test_an_automatic_collection_inside_a_unit_test_is_not_billed(session_output):
    assert _outcome(session_output, "test_an_automatic_collection_is_not_billed") == "PASSED", (
        session_output)


def test_real_cpu_work_in_a_unit_test_still_exceeds_the_budget(session_output):
    assert _outcome(session_output, "test_real_cpu_work_is_billed") == "FAILED", session_output
    assert "test_real_cpu_work_is_billed used 0.1" in session_output, session_output


def test_an_explicit_collection_is_the_tests_own_work(session_output):
    assert _outcome(session_output, "test_an_explicit_collection_is_billed") == "FAILED", (
        session_output)


def test_the_collector_is_re_enabled_after_unit_tests_including_a_failing_one(session_output):
    assert _outcome(session_output, "test_a_failing_unit_test") == "FAILED", session_output
    assert _outcome(session_output, "test_the_collector_is_enabled_after_the_unit_tests") == (
        "PASSED"), session_output


def test_a_non_unit_test_is_not_budgeted(session_output):
    assert _outcome(session_output, "test_an_integration_test_is_not_budgeted") == "PASSED", (
        session_output)
