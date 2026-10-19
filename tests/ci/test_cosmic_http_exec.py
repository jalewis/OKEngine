"""ci/cosmic_http_exec.py -- the HTTP client must outwait the mutant timeout (okengine#1047).

Nightly 13260's shard 3/3 (job 163104) failed convergence.py on three mutants recorded as
``worker='abnormal', test=None`` with empty output. Their coverage-guided selection runs took
359 s, 301 s and 331 s: the only three of 610 over 300 s, which is aiohttp's default total client
timeout. Cosmic Ray's HTTP distributor sends each mutant without a timeout, so the client gave up
while the worker was still testing and logged the empty ``asyncio.TimeoutError`` as abnormal.

These tests drive Cosmic Ray's real distributor against a real local HTTP worker stand-in. The
300 s default is modeled by shrinking ``aiohttp.client.DEFAULT_TIMEOUT``, the value aiohttp reads
when no timeout is passed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import aiohttp
import aiohttp.client
import pytest
from aiohttp import web
from cosmic_ray import cli as cosmic_cli
from cosmic_ray.distribution import http as cosmic_http
from cosmic_ray.work_item import MutationSpec, WorkItem, WorkerOutcome

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ci" / "cosmic_http_exec.py"
SPEC = importlib.util.spec_from_file_location("cosmic_http_exec", SCRIPT)
wrapper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wrapper)

DEFAULT_CLIFF = aiohttp.ClientTimeout(total=0.3, sock_connect=5)  # stands in for the 300 s default
SLOW_WORKER_SECONDS = 1.0                                         # stands in for a 331 s test run


@pytest.fixture(autouse=True)
def _restore_distributor(monkeypatch):
    """Every test leaves Cosmic Ray's module and aiohttp's default as it found them."""
    monkeypatch.setattr(cosmic_http, "aiohttp", cosmic_http.aiohttp)
    monkeypatch.setattr(aiohttp.client, "DEFAULT_TIMEOUT", DEFAULT_CLIFF)


@pytest.fixture
def slow_worker():
    """A local HTTP worker that answers like Cosmic Ray's, after SLOW_WORKER_SECONDS."""
    async def handle(_request):
        await asyncio.sleep(SLOW_WORKER_SECONDS)
        return web.json_response({"worker_outcome": "normal", "output": "1164 passed",
                                  "test_outcome": "survived", "diff": ""})

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    loop = asyncio.new_event_loop()
    app = web.Application()
    app.router.add_post("/", handle)
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def _distribute(url: str) -> dict:
    """Run one mutant through Cosmic Ray's real HttpDistributor; return job_id -> WorkResult."""
    item = WorkItem(job_id="j1", mutations=[MutationSpec(
        module_path=Path("src/okengine/write_services/convergence.py"),
        operator_name="core/ReplaceComparisonOperator_Eq_Lt", occurrence=0,
        start_pos=(64, 13), end_pos=(64, 15))])
    results: dict = {}
    distributor = cosmic_http.HttpDistributor()
    asyncio.run(distributor._process(
        [item], "pytest -x", 580, {"worker-urls": [url]},
        lambda job_id, result: results.__setitem__(job_id, result)))
    return results


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cosmic-ray.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_worker_slower_than_the_default_client_timeout_is_recorded_abnormal(slow_worker):
    """Negative fixture: the defect itself, reproduced through Cosmic Ray's own distributor.
    A worker that is still testing when aiohttp's default expires comes back exactly as the
    nightly recorded it: abnormal, no test outcome, empty output."""
    result = _distribute(slow_worker)["j1"]
    assert result.worker_outcome == WorkerOutcome.ABNORMAL
    assert result.test_outcome is None
    assert result.output == ""


def test_the_installed_client_timeout_lets_a_slow_worker_report(tmp_path, slow_worker):
    """With the wrapper's timeout installed, the same slow worker's verdict reaches the session."""
    config = _config(tmp_path, "[cosmic-ray]\ntimeout = 2.0\n")
    wrapper.install_client_timeout(wrapper.client_timeout(config))
    result = _distribute(slow_worker)["j1"]
    assert result.worker_outcome == WorkerOutcome.NORMAL, result.output
    assert result.test_outcome.value == "survived"
    assert result.output == "1164 passed"


def test_the_client_waits_for_the_configured_mutant_timeout_plus_the_margin(tmp_path):
    """convergence.py's session in nightly 13260 configured `timeout = 580.0`."""
    timeout = wrapper.client_timeout(_config(tmp_path, "[cosmic-ray]\ntimeout = 580.0\n"))
    assert timeout.total == 580.0 + wrapper.RESPONSE_MARGIN_SECONDS
    assert timeout.total > 300, "must outlast aiohttp's 300 s default for a slow survivor"
    assert timeout.sock_connect == wrapper.CONNECT_SECONDS


@pytest.mark.parametrize("body", [
    "[cosmic-ray]\n", "", "[cosmic-ray]\ntimeout = 0\n", "[cosmic-ray]\ntimeout = -5\n",
    "[cosmic-ray]\ntimeout = true\n", "[cosmic-ray]\ntimeout = \"580\"\n",
])
def test_a_config_without_a_positive_timeout_is_refused(tmp_path, body):
    """No configured bound must not quietly fall back to the 300 s default it replaces."""
    with pytest.raises(ValueError, match="timeout must be a positive number"):
        wrapper.client_timeout(_config(tmp_path, body))


def test_the_distributor_still_reaches_the_rest_of_aiohttp():
    wrapper.install_client_timeout(aiohttp.ClientTimeout(total=10))
    assert cosmic_http.aiohttp.ClientTimeout is aiohttp.ClientTimeout
    assert cosmic_http.aiohttp.request is not aiohttp.request


def test_main_installs_the_timeout_and_a_loop_before_exec(tmp_path, monkeypatch):
    config = _config(tmp_path, "[cosmic-ray]\ntimeout = 580.0\n")
    seen = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        seen["timeout"] = cosmic_http.aiohttp.request.keywords["timeout"]
        seen["loop"] = asyncio.get_event_loop()
        return 0

    cosmic = mock.create_autospec(cosmic_cli.main, side_effect=fake_main)
    monkeypatch.setattr(cosmic_cli, "main", cosmic)
    assert wrapper.main([str(config), "session.sqlite"]) == 0
    assert seen["argv"] == ["exec", str(config), "session.sqlite"]
    assert seen["timeout"].total == 580.0 + wrapper.RESPONSE_MARGIN_SECONDS
    assert not seen["loop"].is_closed()
    seen["loop"].close()


def test_main_refuses_to_run_without_a_config_and_session():
    with pytest.raises(SystemExit, match="usage"):
        wrapper.main(["only-config.toml"])


def test_the_script_runs_as_the_gate_invokes_it(tmp_path):
    """Real invocation, as ci/mutation_gate.py's execute_session runs it: the script path under
    the current interpreter. A missing config fails before any mutant is sent."""
    proc = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "absent.toml"),
                           str(tmp_path / "session.sqlite")],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "absent.toml" in proc.stderr
