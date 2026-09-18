"""Bounded production-like performance gate for OKEngine's critical paths.

The API load uses 100 simultaneous virtual users, ten requests each: exactly 1,000
requests per surface and therefore both global minimums in one deterministic run.
The remaining cases exercise the real MCP protocol, filesystem, connector,
selection, assembly, and receipt implementations rather than timing mocks.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


pytestmark = pytest.mark.performance
ROOT = Path(__file__).resolve().parents[2]
READER = os.environ.get("SMOKE_READER_URL", "http://127.0.0.1:9880")
COCKPIT = os.environ.get("SMOKE_COCKPIT_URL", "http://127.0.0.1:9881")
MCP = os.environ.get("SMOKE_MCP_URL", "http://127.0.0.1:8880")
GATEWAY = os.environ.get("SMOKE_GATEWAY_URL", "http://127.0.0.1:8882")
MCP_TOKEN = os.environ.get("SMOKE_MCP_TOKEN", "okengine-local")
ARTIFACTS = Path(os.environ.get("SMOKE_ARTIFACT_DIR", "artifacts/performance-release"))
USERS = 100
REQUESTS_PER_USER = 10
LOAD_SAMPLES = USERS * REQUESTS_PER_USER
MIN_RPM = 1_000.0
QUERY_P95_MS = 100.0
# End-to-end lexical search starts a fresh bounded qmd/Node process for every MCP call.  The
# database-only path is held to the global 100 ms budget below; this separate process boundary is
# calibrated at 1.5 s so a one-CPU release container has deterministic noisy-neighbour headroom.
MCP_SEARCH_P95_MS = 1_500.0
CPU_PRESSURE_AVG10_MAX = 20.0
MAX_TYPICAL_MEMORY_BYTES = 512 * 1024 * 1024
EXPECTED_RPM = 100
CRITICAL_MULTIPLIER = 10
CONNECTOR_MAX_S = 5.0
SELECTION_MAX_S = 1.0
ASSEMBLY_MAX_S = 5.0
RECEIPT_MAX_S = 5.0


def _effective_cpus() -> float | None:
    """CPUs this container may actually use, from the cgroup -- or None when it cannot be read.

    None is deliberate: an unknown quota must not be recorded as the host's core count, which is
    the misreading this exists to correct. Absent is honest; wrong is not.
    """
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
    except (OSError, ValueError):
        return None
    if quota == "max":
        return None
    try:
        return int(quota) / int(period)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_cpu_pressure(text: str) -> float | None:
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "some":
            values = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
            try:
                return float(values["avg10"])
            except (KeyError, ValueError):
                return None
    return None


def _cpu_pressure() -> float | None:
    try:
        return _parse_cpu_pressure(Path("/sys/fs/cgroup/cpu.pressure").read_text())
    except OSError:
        return None


def _classify_cpu_pressure(start: float | None, end: float | None) -> str:
    if start is None or end is None or max(start, end) > CPU_PRESSURE_AVG10_MAX:
        return "environment_indeterminate"
    return "controlled"


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        raise AssertionError("performance metric has zero samples")
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def _evaluate(samples: list[float], *, elapsed_s: float, min_samples: int = 1,
              min_rpm: float | None = None, max_p95_ms: float | None = None) -> dict:
    if not samples:
        raise AssertionError("performance metric has zero samples")
    if len(samples) < min_samples:
        raise AssertionError(f"performance metric has {len(samples)} samples; need {min_samples}")
    if elapsed_s <= 0:
        raise AssertionError("performance elapsed time must be positive")
    result = {
        "samples": len(samples),
        "elapsed_s": elapsed_s,
        "rpm": len(samples) / elapsed_s * 60,
        "p50_ms": statistics.median(samples),
        "p95_ms": _percentile(samples, 0.95),
        "max_ms": max(samples),
    }
    if min_rpm is not None and result["rpm"] < min_rpm:
        raise AssertionError(f"throughput {result['rpm']:.1f} rpm is below {min_rpm:.1f}")
    if max_p95_ms is not None and result["p95_ms"] >= max_p95_ms:
        raise AssertionError(
            f"p95 {result['p95_ms']:.2f}ms must be below {max_p95_ms:.2f}ms")
    return result


@pytest.fixture(scope="session")
def metrics():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    data: dict[str, dict] = {}
    raw: dict[str, list] = {}
    pressure_start = _cpu_pressure()
    yield data, raw
    (ARTIFACTS / "performance-summary.json").write_text(
        json.dumps({"thresholds": {
            "concurrent_users": USERS, "minimum_rpm": MIN_RPM,
            "query_p95_ms_exclusive": QUERY_P95_MS,
            "mcp_search_p95_ms_exclusive": MCP_SEARCH_P95_MS,
            "cpu_pressure_avg10_max": CPU_PRESSURE_AVG10_MAX,
            "typical_memory_bytes_exclusive": MAX_TYPICAL_MEMORY_BYTES,
            "critical_path_multiplier": CRITICAL_MULTIPLIER,
            "declared_expected_rpm": EXPECTED_RPM,
            "connector_1000_records_max_s": CONNECTOR_MAX_S,
            "selection_10000_candidates_max_s": SELECTION_MAX_S,
            "assembly_10000_observations_max_s": ASSEMBLY_MAX_S,
            "receipt_1000_items_max_s": RECEIPT_MAX_S,
        }, "metrics": data}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ARTIFACTS / "performance-raw.json").write_text(
        json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    docker = shutil.which("docker")
    compose = subprocess.run(
        [docker, "inspect", *CONTAINERS], capture_output=True, text=True, timeout=20,
        check=False) if docker else None
    # CPU topology is NOT namespaced: inside a job container os.cpu_count() and
    # sched_getaffinity() both report the HOST's cores, not the cgroup quota the container is held
    # to (measured: 12 reported against a 4.0-CPU cpu.max). Recording only that number stamps this
    # evidence artifact with a machine the run never had -- and the whole purpose of the artifact is
    # to describe the environment a latency measurement was taken in. Record the effective quota as
    # the primary figure and keep the host count clearly labelled as such. (okengine#583)
    pressure_end = _cpu_pressure()
    pressure_samples = [value for value in (pressure_start, pressure_end) if value is not None]
    classification = _classify_cpu_pressure(pressure_start, pressure_end)
    environment = {
        "python": sys.version, "platform": platform.platform(),
        "cpu_count_host": os.cpu_count(),
        "cpus_effective": _effective_cpus(),
        "cpu_pressure_some_avg10": {
            "start": pressure_start, "end": pressure_end,
            "maximum": max(pressure_samples) if pressure_samples else None,
        },
        "git_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                  capture_output=True, check=False).stdout.strip(),
        "container_inspect": json.loads(compose.stdout) if compose and compose.returncode == 0 else [],
    }
    environment["measurement_classification"] = classification
    (ARTIFACTS / "performance-environment.json").write_text(
        json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if classification != "controlled":
        pytest.fail(
            "performance environment is indeterminate: cgroup CPU pressure must be available "
            f"at start and end and remain <= {CPU_PRESSURE_AVG10_MAX}%")


@pytest.fixture(autouse=True)
def _stack_required():
    try:
        status, _ = _request(f"{READER}/healthz", timeout=2)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
        if os.environ.get("SMOKE_RELEASE") == "1":
            pytest.fail(f"performance stack unavailable: {exc}")
        pytest.skip(f"performance stack is not running: {exc}")


def _request(url: str, *, timeout: float = 10, data: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _load_case(name: str, url: str, expected_status: int, *, data: bytes | None = None,
               headers: dict | None = None, body_marker: bytes | None = None) -> tuple[dict, list]:
    barrier = threading.Barrier(USERS)

    def user(user_id: int) -> list[dict]:
        del user_id
        barrier.wait(timeout=10)
        observations = []
        for _ in range(REQUESTS_PER_USER):
            started = time.perf_counter()
            status, body = _request(url, timeout=15, data=data, headers=headers)
            latency = (time.perf_counter() - started) * 1000
            assert status == expected_status, f"{name}: HTTP {status}, expected {expected_status}"
            if body_marker is not None:
                assert body_marker in body, f"{name}: response contract marker absent"
            observations.append({"latency_ms": latency, "status": status})
        return observations

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=USERS, thread_name_prefix=f"perf-{name}") as pool:
        nested = list(pool.map(user, range(USERS)))
    elapsed = time.perf_counter() - started
    observations = [row for rows in nested for row in rows]
    summary = _evaluate([row["latency_ms"] for row in observations], elapsed_s=elapsed,
                        min_samples=LOAD_SAMPLES, min_rpm=MIN_RPM)
    summary.update({"concurrent_users": USERS, "requests_per_user": REQUESTS_PER_USER,
                    "expected_rpm": EXPECTED_RPM,
                    "load_multiplier": summary["rpm"] / EXPECTED_RPM})
    assert summary["load_multiplier"] >= CRITICAL_MULTIPLIER
    return summary, observations


@pytest.mark.parametrize("samples,elapsed,kwargs,message", [
    ([], 1.0, {}, "zero samples"),
    ([1.0], 60.0, {"min_rpm": 1_000}, "below"),
    ([101.0] * 20, 1.0, {"max_p95_ms": 100}, "must be below"),
])
def test_metric_evaluator_fails_closed(samples, elapsed, kwargs, message):
    with pytest.raises(AssertionError, match=message):
        _evaluate(samples, elapsed_s=elapsed, **kwargs)


@pytest.mark.parametrize("text,expected", [
    ("some avg10=7.25 avg60=3.00 avg300=1.00 total=4\nfull avg10=0.0", 7.25),
    ("full avg10=1.0", None),
    ("some avg10=invalid", None),
])
def test_cpu_pressure_parser_is_explicit_and_total(text, expected):
    assert _parse_cpu_pressure(text) == expected


def test_100_users_and_1000_requests_per_surface(metrics):
    summary, raw = metrics
    response_body = json.dumps({"model": "fault-normal", "input": "performance"}).encode()
    cases = [
        ("reader-page", f"{READER}/api/page?path=entities/a/apt-smoke", 200,
         None, None, b"SMOKE_BODY_SENTINEL"),
        ("cockpit-dashboard", f"{COCKPIT}/api/dashboards", 200, None, None, b"groups"),
        ("mcp-auth-boundary", f"{MCP}/mcp", 401, None, None, None),
        ("responses", f"{GATEWAY}/v1/responses", 200, response_body,
         {"Content-Type": "application/json", "X-Client-Id": "okengine/performance"}, b"ok"),
    ]
    for name, url, status, data, headers, marker in cases:
        summary[name], raw[name] = _load_case(
            name, url, status, data=data, headers=headers, body_marker=marker)


def test_real_mcp_index_query_and_page_latency(metrics):
    summary, raw = metrics

    async def run() -> dict[str, list[float]]:
        samples = {"mcp-get-page": [], "mcp-search": []}
        async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {MCP_TOKEN}"}, timeout=15) as client:
            async with streamable_http_client(
                    f"{MCP}/mcp", http_client=client) as (read, write, _session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for name, tool, arguments in (
                        ("mcp-get-page", "get_page", {"path": "entities/a/apt-smoke"}),
                        ("mcp-search", "search", {"query": "APT Smoke", "mode": "search",
                                                   "limit": 5}),
                    ):
                        for _ in range(30):
                            started = time.perf_counter()
                            result = await session.call_tool(tool, arguments)
                            samples[name].append((time.perf_counter() - started) * 1000)
                            assert not result.isError
                            text = "".join(getattr(part, "text", "") for part in result.content)
                            assert "SMOKE" in text.upper() or "APT" in text.upper()
        return samples

    samples = asyncio.run(run())
    for name, values in samples.items():
        budget = MCP_SEARCH_P95_MS if name == "mcp-search" else QUERY_P95_MS
        result = _evaluate(values, elapsed_s=sum(values) / 1000,
                           min_samples=30, max_p95_ms=budget)
        summary[name], raw[name] = result, [{"latency_ms": value} for value in values]


def test_sqlite_fts_query_p95_below_100ms(tmp_path, metrics):
    """Apply the global database-query budget to a real deterministic FTS index.

    MCP search additionally records its end-to-end qmd process budget above; conflating process
    startup with the standard's database query threshold would hide which layer regressed.
    """
    summary, raw = metrics
    database = tmp_path / "index.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE VIRTUAL TABLE documents_fts USING fts5(path, title, body)")
        connection.executemany(
            "INSERT INTO documents_fts VALUES (?, ?, ?)",
            ((f"entities/{index}.md", f"Actor {index}",
              f"Deterministic threat intelligence actor campaign evidence {index}")
             for index in range(10_000)))
        connection.commit()
        samples = []
        for _ in range(100):
            started = time.perf_counter()
            rows = connection.execute(
                "SELECT path,title FROM documents_fts WHERE documents_fts MATCH ? "
                "ORDER BY bm25(documents_fts) LIMIT 10", ("deterministic actor",)).fetchall()
            samples.append((time.perf_counter() - started) * 1000)
            assert len(rows) == 10
    result = _evaluate(samples, elapsed_s=sum(samples) / 1000,
                       min_samples=100, max_p95_ms=QUERY_P95_MS)
    summary["sqlite-fts-query"], raw["sqlite-fts-query"] = result, [
        {"latency_ms": value} for value in samples]


def _module(name: str, path: Path):
    source_dir = str(ROOT / "src")
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    sibling_dir = str(path.parent)
    if sibling_dir not in sys.path:
        sys.path.insert(0, sibling_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_ingestion_selection_assembly_and_receipt_critical_paths(tmp_path, metrics):
    summary, raw = metrics
    connector_fixture = tmp_path / "connector.json"
    records = [json.dumps({"event_id": f"evt-{index}", "sequence": index,
                           "value": f"value-{index}"}) for index in range(1_000)]
    connector_fixture.write_text(json.dumps({"fixture_version": 1,
        "pages": [{"body": "\n".join(records) + "\n"}]}), encoding="utf-8")
    command = [sys.executable, str(ROOT / "scripts/cron/source_connector.py"),
               "--manifest", str(ROOT / "tests/fixtures/source_connectors/stream.yaml"),
               "--fixture", str(connector_fixture), "--state-root", str(tmp_path / "state"),
               "--archive-root", str(tmp_path / "archive"), "--health-root", str(tmp_path / "health"),
               "--collection-ledger", str(tmp_path / "ledger"),
               "--observed-at", "2026-08-03T00:00:00Z", "--summary-only"]
    started = time.perf_counter()
    connector = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                               env={**os.environ, "FIXTURE_STREAM_KEY": "performance-fixture"},
                               timeout=30, check=False)
    elapsed = time.perf_counter() - started
    assert connector.returncode == 0, connector.stderr
    result = json.loads(connector.stdout.strip().splitlines()[-1])
    assert result["records"] == 1_000 and result["new_revisions"] == 1_000
    assert elapsed < CONNECTOR_MAX_S
    summary["connector-ingestion"] = {"records": 1_000, "elapsed_s": elapsed,
                                       "records_per_s": 1_000 / elapsed}
    raw["connector-ingestion"] = [{"records": 1_000, "elapsed_s": elapsed}]

    selector = _module("perf_select_entity", ROOT / "scripts/cron/select_entity_candidates.py")
    entities = [{"title": f"Actor {index}", "slug": f"actor-{index}"}
                for index in range(10_000)]
    started = time.perf_counter()
    selected = selector.related_entities(entities, ["Actor 9999 campaign evidence"])
    elapsed = time.perf_counter() - started
    assert selected and selected[0]["slug"] == "actor-9999"
    assert elapsed < SELECTION_MAX_S
    summary["raw-selection"] = {"candidates": 10_000, "elapsed_s": elapsed,
                                "candidates_per_s": 10_000 / elapsed}
    raw["raw-selection"] = [{"candidates": 10_000, "elapsed_s": elapsed}]

    assembler = _module("perf_canonical_assemble", ROOT / "scripts/cron/canonical_assemble.py")
    observations = [{"source": f"source-{index}", "reliability": "A",
                     "observed": "2026-08-03", "fields": {
                         "name": "Bounded Actor", "aliases": [f"Alias {index % 100}"]}}
                    for index in range(10_000)]
    started = time.perf_counter()
    fused = assembler.fuse(observations, {"consensus": ["name"], "union": ["aliases"]})
    elapsed = time.perf_counter() - started
    assert fused["fields"]["name"] == "Bounded Actor"
    assert len(fused["fields"]["aliases"]) == 100
    assert elapsed < ASSEMBLY_MAX_S
    summary["canonical-assembly"] = {"observations": 10_000, "elapsed_s": elapsed,
                                     "observations_per_s": 10_000 / elapsed}
    raw["canonical-assembly"] = [{"observations": 10_000, "elapsed_s": elapsed}]

    receipts = _module("perf_run_receipts", ROOT / "patches/cron-plus/run_receipts.py")
    keys = [f"item-{index}" for index in range(1_000)]
    items = []
    for key in keys:
        path = tmp_path / "wiki" / "sources" / f"{key}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
        items.append({"key": key, "disposition": "accepted", "writes": [{
            "path": f"sources/{key}.md",
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}]})
    selection = {"selected": keys, "input_digest": receipts.digest_items(keys),
                 "lane_id": "performance", "contract_digest": "sha256:performance"}
    receipt = {"api": 1, "run_id": "performance", "lane_id": "performance",
               "contract_digest": "sha256:performance",
               "input_digest": receipts.digest_items(keys), "items": items}
    started = time.perf_counter()
    validated = receipts.validate(receipt, selection, {"id": "performance",
        "output_contract_digest": "sha256:performance"}, tmp_path / "wiki")
    elapsed = time.perf_counter() - started
    assert validated["valid"] and validated["counts"]["accepted"] == 1_000
    assert elapsed < RECEIPT_MAX_S
    summary["terminal-receipts"] = {"items": 1_000, "elapsed_s": elapsed,
                                    "items_per_s": 1_000 / elapsed}
    raw["terminal-receipts"] = [{"items": 1_000, "elapsed_s": elapsed}]


CONTAINERS = [
    "okengine-smoke-reader", "okengine-smoke-cockpit", "okengine-smoke-mcp",
    "okengine-smoke-review-write", "okengine-smoke-responses-gateway",
]


def test_typical_container_memory_below_512mb(metrics):
    summary, raw = metrics
    observations = []
    for container in CONTAINERS:
        result = subprocess.run(
            ["docker", "exec", container, "cat", "/sys/fs/cgroup/memory.current"],
            text=True, capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, f"{container}: {result.stderr}"
        used = int(result.stdout.strip())
        assert used < MAX_TYPICAL_MEMORY_BYTES, (
            f"{container}: typical memory {used} must be below {MAX_TYPICAL_MEMORY_BYTES}")
        observations.append({"container": container, "memory_bytes": used})
    summary["typical-memory"] = {"containers": len(observations),
        "maximum_bytes": max(row["memory_bytes"] for row in observations),
        "limit_bytes_exclusive": MAX_TYPICAL_MEMORY_BYTES}
    raw["typical-memory"] = observations
