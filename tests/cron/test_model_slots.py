import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path


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
