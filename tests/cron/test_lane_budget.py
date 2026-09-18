"""Regression: a lane must be able to see, and stop inside, its own wall-clock ceiling.

cron-plus enforced a hard timeout and never published it, so a lane's only failure mode was being
killed mid-work — everything discarded, no partial result. Three weekly lanes failed exactly that
way for a week, each burning ~20 minutes of model time per run to produce nothing.
"""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "lane_budget.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="lane_budget absent")


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("lane_budget", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lane_budget"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_published_deadline_means_unlimited_not_zero(monkeypatch):
    """An unknown ceiling must never read as a ceiling of ZERO.

    Otherwise every lane refuses to do anything the moment this ships somewhere the runner patch
    has not reached — turning a missing signal into a fleet-wide outage.
    """
    mod = _load()
    monkeypatch.delenv("OKENGINE_RUN_DEADLINE_EPOCH", raising=False)
    monkeypatch.delenv("OKENGINE_RUN_TIMEOUT_SECONDS", raising=False)
    assert mod.remaining() == float("inf")
    assert not mod.exhausted()
    assert mod.fit_batch(5, per_item_seconds=999) == 5


def test_a_reserve_is_held_back_for_finishing(monkeypatch):
    """Stopping AT the deadline is stopping too late — writing results happens after the last item."""
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "1000")
    monkeypatch.setenv("OKENGINE_RUN_DEADLINE_EPOCH", str(time.time() + 1000))
    left = mod.remaining()
    assert 0 < left < 1000, left
    assert 1000 - left >= mod.MIN_RESERVE_SECONDS


def test_batch_is_cut_to_what_actually_fits(monkeypatch):
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OKENGINE_RUN_DEADLINE_EPOCH", str(time.time() + 600))
    assert mod.fit_batch(10, per_item_seconds=120) < 10


def test_a_spent_budget_still_yields_one_item(monkeypatch):
    """A lane that processes nothing produces no evidence of what it costs, so the estimate can
    never improve. One item plus an honest truncation beats a clean no-op."""
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OKENGINE_RUN_DEADLINE_EPOCH", str(time.time() - 5))
    assert mod.exhausted()
    assert mod.fit_batch(10, per_item_seconds=1) == 1


def test_a_malformed_deadline_is_unknown_not_expired(monkeypatch):
    """A parse failure is 'no data', never 'data says stop' — the distinction that keeps costing."""
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_DEADLINE_EPOCH", "not-a-number")
    monkeypatch.delenv("OKENGINE_RUN_TIMEOUT_SECONDS", raising=False)
    assert mod.remaining() == float("inf")


def test_the_runner_patch_publishes_what_this_reads():
    """The helper and the patch are one contract across two repos; a rename in either is silent."""
    patch = (REPO / "patches" / "cron-plus" / "run-deadline-env.patch").read_text(encoding="utf-8")
    helper = MOD.read_text(encoding="utf-8")
    for var in ("OKENGINE_RUN_DEADLINE_EPOCH", "OKENGINE_RUN_TIMEOUT_SECONDS"):
        assert var in patch, f"{var} is read by lane_budget but never published by the patch"
        assert var in helper


def test_patch_files_are_exempt_from_the_whitespace_gate():
    """A unified diff encodes a blank CONTEXT line as a single space, so trailing whitespace in a
    .patch is load-bearing — strip it and the patch stops applying.

    `git diff --check` flagged those as errors, which made adding or editing ANY carried patch fail
    CI. Latent rather than new: two existing patches already carry blank context lines and were
    simply never part of a diff after the gate was introduced.
    """
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "patches/**/*.patch -whitespace" in attrs


def test_reserve_falls_back_when_no_total_is_published(monkeypatch):
    """With no declared ceiling the reserve is the floor, never zero — a lane must always keep
    time to write what it finished."""
    mod = _load()
    monkeypatch.delenv("OKENGINE_RUN_TIMEOUT_SECONDS", raising=False)
    assert mod.reserve_seconds() == mod.MIN_RESERVE_SECONDS
    assert mod.reserve_seconds(1000) > mod.MIN_RESERVE_SECONDS


def test_a_malformed_total_does_not_crash_the_reserve(monkeypatch):
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "not-a-number")
    assert mod.reserve_seconds() == mod.MIN_RESERVE_SECONDS


def test_a_duration_only_environment_still_yields_a_deadline(monkeypatch):
    """A fresh subprocess may see only the duration. Treating it as starting NOW understates
    elapsed time, which never over-promises."""
    mod = _load()
    monkeypatch.delenv("OKENGINE_RUN_DEADLINE_EPOCH", raising=False)
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "600")
    assert mod.deadline_epoch() is not None
    assert mod.remaining() > 0


def test_a_zero_per_item_estimate_does_not_divide_by_zero(monkeypatch):
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("OKENGINE_RUN_DEADLINE_EPOCH", str(time.time() + 600))
    assert mod.fit_batch(7, per_item_seconds=0) == 7


def test_a_malformed_deadline_epoch_is_unknown_not_expired(monkeypatch):
    """A corrupt absolute deadline yields UNKNOWN — which `remaining()` turns into unlimited.

    It does NOT fall through to the duration: a published-but-unparseable epoch means the contract
    is broken, and quietly substituting a different clock would hide that. Unknown is the safe
    reading; expired is the dangerous one.
    """
    mod = _load()
    monkeypatch.setenv("OKENGINE_RUN_DEADLINE_EPOCH", "garbage")
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "600")
    assert mod.deadline_epoch() is None
    assert mod.remaining() == float("inf")


def test_a_malformed_duration_is_also_unknown(monkeypatch):
    mod = _load()
    monkeypatch.delenv("OKENGINE_RUN_DEADLINE_EPOCH", raising=False)
    monkeypatch.setenv("OKENGINE_RUN_TIMEOUT_SECONDS", "not-a-number")
    assert mod.deadline_epoch() is None
