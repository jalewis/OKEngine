"""Fault injection against the live disposable release stack.

Every scenario has a deadline, an observable failed/degraded state, an explicit recovery check,
and a durable timeline. Infrastructure is real: Docker processes, sockets, HTTP, and SQLite.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scripts.cron import llm_lib, reap_stale_sessions


pytestmark = pytest.mark.resilience
READER = os.environ.get("SMOKE_READER_URL", "http://127.0.0.1:9880")
COCKPIT = os.environ.get("SMOKE_COCKPIT_URL", "http://127.0.0.1:9881")
MCP = os.environ.get("SMOKE_MCP_URL", "http://127.0.0.1:8880")
REVIEW = os.environ.get("SMOKE_REVIEW_URL", "http://127.0.0.1:8881")
GATEWAY = os.environ.get("SMOKE_GATEWAY_URL", "http://127.0.0.1:8882")
ARTIFACTS = Path(os.environ.get("SMOKE_ARTIFACT_DIR", "artifacts/resilience-release"))
TIMELINE = ARTIFACTS / "fault-timeline.jsonl"
RECEIPTS = ARTIFACTS / "fault-receipts.jsonl"


def _event(scenario: str, state: str, **details):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with TIMELINE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), "scenario": scenario, "state": state,
                                 **details}, sort_keys=True) + "\n")


def _receipt(scenario: str, outcome: str, **details):
    """Write one machine-auditable terminal disposition for an injected fault."""
    assert outcome in {"FAILED", "DEGRADED", "RECOVERED", "RECONCILED"}
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    record = {"at": time.time(), "scenario": scenario, "outcome": outcome,
              "terminal": True, **details}
    with RECEIPTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _request(url: str, *, timeout: float = 3, data: dict | None = None,
             token: str = "") -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _docker(*args: str, deadline: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          timeout=deadline, check=False)


def _wait(url: str, expected: int, deadline: float = 20) -> None:
    stop = time.monotonic() + deadline
    last = "no response"
    while time.monotonic() < stop:
        try:
            status, _ = _request(url, timeout=1)
            last = f"HTTP {status}"
            if status == expected:
                return
        except (OSError, urllib.error.URLError) as exc:
            last = str(exc)
        time.sleep(0.2)
    pytest.fail(f"service did not recover at {url}: {last}")


@pytest.fixture(autouse=True)
def _stack_required():
    try:
        _wait(f"{GATEWAY}/healthz", 200, deadline=2)
    except pytest.fail.Exception:
        if os.environ.get("SMOKE_RELEASE") == "1":
            raise
        pytest.skip("resilience stack is not running")


@pytest.fixture(scope="session", autouse=True)
def _post_matrix_health_state():
    """Preserve and assert the recovered stack state even when a scenario fails."""
    # This module is collected by the ordinary all-tests/coverage lane, where every resilience test
    # is expected to skip because no disposable stack was requested.  A session finalizer still runs
    # after skipped tests; only enforce live-stack recovery in the explicit release invocation.
    if os.environ.get("SMOKE_RELEASE") != "1":
        yield
        return
    yield
    probes = {
        "reader": (f"{READER}/healthz", 200),
        "cockpit": (f"{COCKPIT}/api/dashboards", 200),
        "mcp": (f"{MCP}/mcp", 401),
        "review-write": (f"{REVIEW}/healthz", 200),
        "responses-gateway": (f"{GATEWAY}/healthz", 200),
    }
    health: dict[str, dict] = {}
    mismatches = []
    for service, (url, expected) in probes.items():
        try:
            status, _ = _request(url, timeout=2)
            healthy = status == expected
            health[service] = {"url": url, "status": status, "expected": expected,
                               "healthy": healthy}
        except (OSError, urllib.error.URLError) as exc:
            healthy = False
            health[service] = {"url": url, "expected": expected, "healthy": False,
                               "error": str(exc)}
        if not healthy:
            mismatches.append(service)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "health-state.json").write_text(
        json.dumps({"at": time.time(), "services": health}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    assert not mismatches, f"stack did not recover: {mismatches}"


@pytest.mark.parametrize("container,url,expected", [
    ("okengine-smoke-responses-gateway", f"{GATEWAY}/healthz", 200),
    ("okengine-smoke-mcp", f"{MCP}/mcp", 401),
])
def test_killed_dependencies_fail_loudly_and_recover(container, url, expected):
    scenario = f"kill:{container}"
    _event(scenario, "inject")
    assert _docker("stop", "--time", "1", container).returncode == 0
    started = time.monotonic()
    with pytest.raises((OSError, urllib.error.URLError)):
        _request(url, timeout=1)
    assert time.monotonic() - started < 3
    _event(scenario, "observable-failure")
    assert _docker("start", container).returncode == 0
    _wait(url, expected)
    _event(scenario, "recovered")
    _receipt(scenario, "RECOVERED")


def test_network_partition_times_out_and_recovers_without_hanging():
    scenario = "partition:cockpit"
    assert _docker("pause", "okengine-smoke-cockpit").returncode == 0
    _event(scenario, "inject")
    started = time.monotonic()
    try:
        with pytest.raises((OSError, TimeoutError, urllib.error.URLError)):
            _request(f"{COCKPIT}/api/dashboards", timeout=1)
        assert time.monotonic() - started < 3
        _event(scenario, "bounded-timeout")
    finally:
        assert _docker("unpause", "okengine-smoke-cockpit").returncode == 0
    _wait(f"{COCKPIT}/api/dashboards", 200)
    _event(scenario, "recovered")
    _receipt(scenario, "RECOVERED")


def test_responses_timeout_malformed_stream_and_retry_exhaustion_are_truthful(monkeypatch):
    monkeypatch.setattr(llm_lib.random, "uniform", lambda *_args: 0.0)
    base = f"{GATEWAY}/v1"
    assert llm_lib.chat("probe", base_url=base, model="fault-normal", timeout=2,
                        retries=0, client_id="okengine/resilience") == "ok"

    started = time.monotonic()
    with pytest.raises(llm_lib.LLMError, match="no output text"):
        llm_lib.chat("probe", base_url=base, model="fault-malformed", timeout=2,
                     retries=0, client_id="okengine/resilience")
    assert time.monotonic() - started < 3
    _event("responses:malformed", "failed-truthfully")
    _receipt("responses:malformed", "FAILED", fallback_used=False)

    started = time.monotonic()
    with pytest.raises(llm_lib.LLMError, match="after 1 attempt"):
        llm_lib.chat("probe", base_url=base, model="fault-timeout", timeout=1,
                     retries=0, client_id="okengine/resilience")
    assert time.monotonic() - started < 3
    _event("responses:timeout", "failed-truthfully")
    _receipt("responses:timeout", "FAILED", fallback_used=False)

    before = json.loads(_request(f"{GATEWAY}/events")[1])
    started = time.monotonic()
    with pytest.raises(llm_lib.LLMError, match="after 2 attempt"):
        llm_lib.chat("probe", base_url=base, model="fault-503", timeout=2,
                     retries=1, client_id="okengine/resilience")
    assert 2 <= time.monotonic() - started < 6
    after = json.loads(_request(f"{GATEWAY}/events")[1])
    attempts = after[len(before):]
    assert len(attempts) == 2
    assert {row["client_id"] for row in attempts} == {"okengine/resilience"}
    assert all(row["path"] == "/v1/responses" for row in attempts)
    _event("responses:retry-exhaustion", "failed-no-fallback", attempts=len(attempts))
    _receipt("responses:retry-exhaustion", "FAILED", attempts=len(attempts),
             fallback_used=False)


def test_queue_saturation_and_disk_full_fail_bounded_then_recover():
    base = f"{GATEWAY}/v1"

    def saturated_call(_index: int) -> str:
        try:
            return llm_lib.chat("probe", base_url=base, model="fault-saturated", timeout=2,
                                retries=0, client_id="okengine/resilience-saturation")
        except llm_lib.LLMError:
            return "failed"

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(saturated_call, range(8)))
    assert time.monotonic() - started < 4
    assert "ok" in outcomes and "failed" in outcomes
    _event("gateway:queue-saturation", "bounded-degradation",
           succeeded=outcomes.count("ok"), failed=outcomes.count("failed"))
    _receipt("gateway:queue-saturation", "DEGRADED", succeeded=outcomes.count("ok"),
             failed=outcomes.count("failed"))

    with pytest.raises(llm_lib.LLMError, match="507"):
        llm_lib.chat("probe", base_url=base, model="fault-disk-full", timeout=3,
                     retries=0, client_id="okengine/resilience-disk")
    _event("gateway:disk-full", "failed-truthfully")
    assert llm_lib.chat("probe", base_url=base, model="fault-normal", timeout=2,
                        retries=0, client_id="okengine/resilience-recovery") == "ok"
    _event("gateway:disk-full", "recovered")
    _receipt("gateway:disk-full", "RECOVERED", initial_outcome="FAILED")


def test_sqlite_lock_reports_failure_then_recovers_without_partial_state(tmp_path):
    database = tmp_path / "state.sqlite"
    owner = sqlite3.connect(database, timeout=1)
    owner.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
    owner.commit()
    owner.execute("BEGIN EXCLUSIVE")
    result: dict[str, str] = {}

    def contender():
        try:
            with sqlite3.connect(database, timeout=0.2) as connection:
                connection.execute("INSERT INTO queue(state) VALUES ('partial')")
        except sqlite3.OperationalError as exc:
            result["error"] = str(exc)

    worker = threading.Thread(target=contender)
    worker.start(); worker.join(timeout=2)
    assert not worker.is_alive() and "locked" in result.get("error", "")
    owner.rollback(); owner.close()
    with sqlite3.connect(database, timeout=1) as connection:
        assert connection.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
        connection.execute("INSERT INTO queue(state) VALUES ('recovered')")
        assert connection.execute("SELECT state FROM queue").fetchall() == [("recovered",)]
    _event("sqlite:exclusive-lock", "recovered-no-partial-state")
    _receipt("sqlite:exclusive-lock", "RECOVERED", partial_rows=0)


def test_killed_worker_orphan_gets_truthful_terminal_reconciliation(tmp_path):
    worker = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker.kill(); worker.wait(timeout=3)
    assert worker.returncode is not None and worker.returncode != 0

    database = tmp_path / "worker-state.sqlite"
    started = time.time() - 12 * 3600
    last_activity = started + 7
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL, "
            "ended_at REAL, end_reason TEXT);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL);"
        )
        connection.execute("INSERT INTO sessions VALUES ('killed', ?, NULL, NULL)", (started,))
        connection.execute("INSERT INTO messages(session_id,timestamp) VALUES ('killed', ?)",
                           (last_activity,))
    assert reap_stale_sessions.main(["--db", str(database), "--age-hours", "6"]) == 0
    with sqlite3.connect(database) as connection:
        ended_at, reason = connection.execute(
            "SELECT ended_at,end_reason FROM sessions WHERE id='killed'").fetchone()
    assert ended_at == pytest.approx(last_activity) and reason == "stale_reaper"
    _event("worker:sigkill", "terminal-reconciled", end_reason=reason)
    _receipt("worker:sigkill", "RECONCILED", end_reason=reason)


def test_read_only_vault_fails_visibly_then_recovers():
    scenario = "vault:read-only"
    deployed = Path(os.environ["SMOKE_VAULT"]) / "wiki" / "sources" / "review-smoke.md"
    payload = {
        "path": "sources/review-smoke", "decision": "approve", "reviewer": "resilience",
        "expected_version": 1, "expected_hash": hashlib.sha256(deployed.read_bytes()).hexdigest(),
    }
    assert _docker("exec", "--user", "root", "okengine-smoke-review-write",
                   "chmod", "-R", "a-w", "/opt/vault/wiki").returncode == 0
    _event(scenario, "inject")
    try:
        status, _ = _request(f"{REVIEW}/review/resolve", data=payload, timeout=3,
                             token="okengine-smoke-review-secret")
        assert status >= 500
        _event(scenario, "observable-failure", http_status=status)
    finally:
        assert _docker("exec", "--user", "root", "okengine-smoke-review-write",
                       "chmod", "-R", "u+w", "/opt/vault/wiki").returncode == 0
    status, body = _request(f"{REVIEW}/review/resolve", data=payload, timeout=5,
                            token="okengine-smoke-review-secret")
    assert status == 200 and json.loads(body)["state"] == "approved"
    assert _request(f"{READER}/api/page?path=sources/review-smoke")[0] == 200
    _event(scenario, "recovered")
    _receipt(scenario, "RECOVERED", initial_http_status=500)


def test_vault_owner_drift_fails_visibly_then_recovers():
    """Exercise the deployment failure where the mount owner no longer matches the service UID."""
    scenario = "vault:owner-drift"
    vault = Path(os.environ["SMOKE_VAULT"])
    source_dir = vault / "wiki" / "sources"
    deployed = source_dir / "review-permission.md"
    initial = source_dir.stat()
    service_uid = int(os.environ["SMOKE_UID"])
    mismatched_uid = 65_534 if service_uid != 65_534 else 65_533
    payload = {
        "path": "sources/review-permission", "decision": "approve",
        "reviewer": "resilience", "expected_version": 1,
        "expected_hash": hashlib.sha256(deployed.read_bytes()).hexdigest(),
    }

    assert _docker("exec", "--user", "root", "okengine-smoke-review-write",
                   "chown", f"{mismatched_uid}:{mismatched_uid}",
                   "/opt/vault/wiki/sources").returncode == 0
    _event(scenario, "inject", owner_uid=mismatched_uid, service_uid=service_uid)
    try:
        status, _ = _request(f"{REVIEW}/review/resolve", data=payload, timeout=3,
                             token="okengine-smoke-review-secret")
        assert status >= 500
        _event(scenario, "observable-failure", http_status=status)
    finally:
        assert _docker("exec", "--user", "root", "okengine-smoke-review-write",
                       "chown", f"{initial.st_uid}:{initial.st_gid}",
                       "/opt/vault/wiki/sources").returncode == 0

    status, body = _request(f"{REVIEW}/review/resolve", data=payload, timeout=5,
                            token="okengine-smoke-review-secret")
    assert status == 200 and json.loads(body)["state"] == "approved"
    assert _request(f"{READER}/api/page?path=sources/review-permission")[0] == 200
    _event(scenario, "recovered")
    _receipt(scenario, "RECOVERED", initial_http_status=500,
             restored_uid=initial.st_uid, restored_gid=initial.st_gid)
