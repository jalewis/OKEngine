import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


MODULE = Path(__file__).parents[2] / "patches" / "cron-plus" / "model_slots.py"
spec = importlib.util.spec_from_file_location("model_slots", MODULE)
slots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slots)


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: custom\n  base_url: http://ollama:11434/v1\n"
        "  default: qwen3:30b\n",
        encoding="utf-8",
    )


def _contender(tmp_path, job):
    code = (
        "import importlib.util, json, os; "
        f"s=importlib.util.spec_from_file_location('model_slots', {str(MODULE)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"j=json.loads({__import__('json').dumps(__import__('json').dumps(job))}); "
        "\nwith m.model_slot(j): print('acquired', flush=True)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_identity_uses_runtime_defaults_and_job_overrides(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    assert slots.model_identity({}) == "custom|http://ollama:11434/v1|qwen3:30b"
    assert slots.model_identity({"model": "qwen3:8b"}) == (
        "custom|http://ollama:11434/v1|qwen3:8b"
    )
    assert slots.model_identity({"no_agent": True}) is None
    assert slots.model_concurrency({}) == 1
    assert slots.model_concurrency({"model_concurrency": 2}) == 2
    assert slots.model_concurrency({"model_concurrency": "invalid"}) == 1
    # Bounded by the run's own ceiling (1200s default * 0.5), NOT the raw 1800s default --
    # see test_slot_wait_is_bounded_by_the_runs_own_ceiling for why that matters.
    assert slots.model_slot_wait_seconds({}) == 600.0
    assert slots.model_slot_wait_seconds({"model_slot_wait_seconds": "2.5"}) == 2.5


def test_same_model_waits_but_different_model_does_not(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    with slots.model_slot({}):
        same = _contender(tmp_path, {})
        time.sleep(0.2)
        assert same.poll() is None

        other = _contender(tmp_path, {"model": "qwen3:8b"})
        stdout, stderr = other.communicate(timeout=5)
        assert other.returncode == 0, stderr
        assert stdout.strip() == "acquired"

    stdout, stderr = same.communicate(timeout=5)
    assert same.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_slot_releases_after_exception(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    try:
        with slots.model_slot({}):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with slots.model_slot({}):
        pass


def test_waiting_for_a_slot_has_a_fail_visible_deadline(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    with slots.model_slot({}):
        started = time.monotonic()
        try:
            with slots.model_slot({"model_slot_wait_seconds": 0.1}):
                raise AssertionError("contender unexpectedly acquired the held slot")
        except TimeoutError as exc:
            assert "model slot unavailable" in str(exc)
        assert time.monotonic() - started < 1


def test_slot_releases_when_holder_process_is_terminated(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    holder_code = (
        "import importlib.util, time; "
        f"s=importlib.util.spec_from_file_location('model_slots', {str(MODULE)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        "\nwith m.model_slot({}): print('held', flush=True); time.sleep(30)"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code], env={**os.environ, "HERMES_HOME": str(tmp_path)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    contender = _contender(tmp_path, {})
    time.sleep(0.2)
    assert contender.poll() is None
    holder.terminate()
    holder.wait(timeout=5)
    stdout, stderr = contender.communicate(timeout=5)
    assert contender.returncode == 0, stderr
    assert stdout.strip() == "acquired"


def test_slot_wait_is_bounded_by_the_runs_own_ceiling(tmp_path, monkeypatch):
    """A wait that outlives the run's hard deadline can never expire, so it never diagnoses.

    cron-plus arms the deadline before entering the slot wait, so SIGALRM wins any race and
    replaces "model slot unavailable (identity=..., limit=N)" with the generic hard-timeout
    error. That is how 1,558 zero-turn capacity failures were filed as lane-work timeouts.
    """
    _configure(tmp_path, monkeypatch)

    # The shipped defaults are the exact misconfiguration: 1800s wait, 1200s ceiling.
    monkeypatch.delenv("OKENGINE_MODEL_SLOT_WAIT_SECONDS", raising=False)
    monkeypatch.delenv("OKENGINE_AGENT_RUN_TIMEOUT_SECONDS", raising=False)
    assert slots.model_slot_wait_seconds({}) < 1200, "wait must expire before the deadline"

    # An operator raising the wait past the ceiling is capped too: waiting longer than the
    # run can live is not a preference the deployment gets to express.
    monkeypatch.setenv("OKENGINE_MODEL_SLOT_WAIT_SECONDS", "99999")
    assert slots.model_slot_wait_seconds({}) == 600.0

    # A per-job timeout moves the ceiling with it.
    assert slots.model_slot_wait_seconds({"timeout": 60}) == 30.0

    # Below the ceiling the operator's value is honoured untouched.
    monkeypatch.setenv("OKENGINE_MODEL_SLOT_WAIT_SECONDS", "45")
    assert slots.model_slot_wait_seconds({}) == 45.0


def test_ceiling_is_none_not_zero_when_run_timeout_cannot_be_resolved(tmp_path, monkeypatch):
    """An undiscoverable ceiling must not collapse to zero.

    Returning 0.0 would make every lane refuse to wait for a slot the moment this shipped
    somewhere run_timeout is absent -- a worse outage than the misreporting it prevents.
    """
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(slots, "_run_timeout_looked_up", True)
    monkeypatch.setattr(slots, "_run_timeout_module", None)
    assert slots.slot_wait_ceiling({}) is None
    monkeypatch.setenv("OKENGINE_MODEL_SLOT_WAIT_SECONDS", "123")
    assert slots.model_slot_wait_seconds({}) == 123.0


def test_starved_run_reports_the_slot_diagnostic_not_a_generic_timeout(tmp_path, monkeypatch):
    """End-to-end: the two guards composed, in the order the runner composes them.

    This is the behaviour the whole change exists for -- an unavailable slot must name itself.
    """
    import importlib.util as _ilu
    rt_path = MODULE.with_name("run_timeout.py")
    rt_spec = _ilu.spec_from_file_location("run_timeout_probe", rt_path)
    run_timeout = _ilu.module_from_spec(rt_spec)
    rt_spec.loader.exec_module(run_timeout)

    _configure(tmp_path, monkeypatch)
    job = {"timeout": 2}          # ceiling 2s -> slot wait capped to 1s
    assert slots.model_slot_wait_seconds(job) == 1.0

    held = slots.model_slot(job)
    held.__enter__()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            with run_timeout.run_deadline(job):        # armed FIRST, exactly like the runner
                with slots.model_slot(job):
                    pass
        message = str(excinfo.value)
        assert "model slot unavailable" in message, message
        assert "hard timeout" not in message, message
    finally:
        held.__exit__(None, None, None)
