"""Disposable real-service integration proof for the OKEngine read plane.

These tests deliberately use real subprocesses, sockets, HTTP, the MCP SDK, and
an on-disk vault. Infrastructure calls are not monkeypatched: a service that
cannot import, bind, authenticate, publish tools, persist, or terminate fails
the blocking integration lane.
"""
from __future__ import annotations

import asyncio
import json
import os
import resource
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import pytest
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
SERVICE_CPU_SECONDS = 60
# Native renderer/parser dependencies reserve substantial virtual address space
# without committing it. Four GiB remains a hard runaway ceiling while leaving
# enough address space for the services' required cache-warming threads.
SERVICE_ADDRESS_SPACE_BYTES = 4 * 1024 * 1024 * 1024
SERVICE_OPEN_FILES = 256
SERVICE_PROCESS_HEADROOM = 256


def _uid_task_count() -> int:
    """RLIMIT_NPROC counts shared-UID threads, so measure the existing floor."""
    uid = os.getuid()
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid == uid:
                total += len(list((entry / "task").iterdir()))
        except (FileNotFoundError, PermissionError):
            continue
    return total


SERVICE_PROCESSES = _uid_task_count() + SERVICE_PROCESS_HEADROOM


def _limit_service_process() -> None:
    """Apply the same bounded-process contract in local and CI integration runs."""
    os.setsid()
    resource.setrlimit(resource.RLIMIT_CPU, (SERVICE_CPU_SECONDS, SERVICE_CPU_SECONDS))
    resource.setrlimit(
        resource.RLIMIT_AS, (SERVICE_ADDRESS_SPACE_BYTES, SERVICE_ADDRESS_SPACE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (SERVICE_OPEN_FILES, SERVICE_OPEN_FILES))
    resource.setrlimit(resource.RLIMIT_NPROC, (SERVICE_PROCESSES, SERVICE_PROCESSES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, *, headers: dict[str, str] | None = None,
             timeout: float = 5) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass
class Service:
    name: str
    url: str
    process: subprocess.Popen
    log_path: Path
    log_handle: object

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.log_handle.close()
        assert self.process.returncode is not None, f"{self.name} did not terminate"

    def diagnostic(self) -> str:
        self.log_handle.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-6000:]


def _start_service(name: str, command: list[str], cwd: Path, env: dict[str, str],
                   readiness_url: str, artifacts: Path,
                   *, expected_status: int = 200) -> Service:
    artifacts.mkdir(parents=True, exist_ok=True)
    log_path = artifacts / f"{name}.log"
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603 - fixed repository command
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=log_handle, stderr=subprocess.STDOUT, text=True,
        preexec_fn=_limit_service_process,
    )
    service = Service(name, readiness_url.rsplit("/", 1)[0], process, log_path, log_handle)
    deadline = time.monotonic() + 20
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"{name} exited during startup with {process.returncode}:\n{service.diagnostic()}")
        try:
            status, _ = _request(readiness_url, timeout=1)
            if status == expected_status:
                return service
            last_error = f"HTTP {status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    service.stop()
    pytest.fail(f"{name} readiness deadline exceeded ({last_error}):\n{service.diagnostic()}")


@pytest.fixture(scope="module")
def real_services(tmp_path_factory):
    root = tmp_path_factory.mktemp("real-service-mesh")
    vault = root / "vault"
    page = vault / "wiki" / "entities" / "a" / "alpha.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: entity\nid: entity:alpha\ntitle: Alpha\n---\n"
        "# Alpha\n\nREAL_SERVICE_SENTINEL\n",
        encoding="utf-8",
    )
    (vault / "schema.yaml").write_text(
        "types:\n  entity: {required: [type, id, title]}\n"
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n",
        encoding="utf-8",
    )
    artifacts = Path(os.environ.get(
        "OKENGINE_INTEGRATION_ARTIFACTS", REPO / "artifacts" / "integration-services"))
    common = {**os.environ, "PYTHONUNBUFFERED": "1", "VAULT_DIR": str(vault),
              "WIKI_PATH": str(vault), "OKENGINE_TRUST": "public",
              "OKENGINE_BIND": "127.0.0.1"}
    services: dict[str, Service] = {}
    with ExitStack() as stack:
        for name, app_dir in (("reader", "okengine-reader"),
                              ("cockpit", "okengine-cockpit")):
            port = _free_port()
            url = f"http://127.0.0.1:{port}"
            service = _start_service(
                name, [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                       "--port", str(port), "--log-level", "warning"],
                REPO / app_dir, {**common, "PORT": str(port)}, f"{url}/healthz", artifacts)
            services[name] = service
            stack.callback(service.stop)

        mcp_port = _free_port()
        mcp_url = f"http://127.0.0.1:{mcp_port}"
        mcp_env = {
            **common, "PORT": str(mcp_port), "OKENGINE_MCP_HOST": "127.0.0.1",
            "OKENGINE_MCP_TRANSPORT": "streamable-http", "OKENGINE_MCP_TOKEN": "integration-token",
            "OKENGINE_MCP_INDEX_REFRESH_HOURS": "0", "OKENGINE_MCP_INDEX_POLL_SECONDS": "0",
            "OKENGINE_MCP_SCRIPTS": str(REPO / "scripts" / "cron"),
            "OKENGINE_MCP_PY": sys.executable,
        }
        mcp = _start_service(
            "mcp", [sys.executable, "server.py"], REPO / "okengine-mcp", mcp_env,
            f"{mcp_url}/mcp", artifacts, expected_status=401)
        mcp.url = mcp_url
        services["mcp"] = mcp
        stack.callback(mcp.stop)
        yield {"vault": vault, **services}


def test_reader_and_cockpit_retrieve_the_same_persisted_page(real_services):
    encoded = urllib.parse.quote("entities/a/alpha")
    reader_status, reader_body = _request(f"{real_services['reader'].url}/api/page?path={encoded}")
    cockpit_status, cockpit_body = _request(f"{real_services['cockpit'].url}/api/page?path={encoded}")
    assert reader_status == cockpit_status == 200
    assert "REAL_SERVICE_SENTINEL" in json.loads(reader_body)["html"]
    assert "REAL_SERVICE_SENTINEL" in json.loads(cockpit_body)["html"]

    missing_status, _ = _request(f"{real_services['reader'].url}/api/page?path=missing/page")
    traversal_status, _ = _request(
        f"{real_services['reader'].url}/api/page?path={urllib.parse.quote('../schema')}")
    assert missing_status == 404
    assert traversal_status in {400, 404}


def test_mcp_authenticates_and_publishes_real_tools(real_services):
    mcp_url = f"{real_services['mcp'].url}/mcp"
    assert _request(mcp_url)[0] == 401

    async def inspect_tools():
        async with httpx.AsyncClient(
                headers={"Authorization": "Bearer integration-token"}, timeout=10) as client:
            async with streamable_http_client(
                    mcp_url, http_client=client) as (read, write, _session_id):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    tools = await session.list_tools()
                    return initialized, {tool.name for tool in tools.tools}

    initialized, names = asyncio.run(inspect_tools())
    assert initialized.serverInfo.name == "okengine"
    assert {"search", "get_page", "list_pages"} <= names


def test_service_logs_and_teardown_are_observable(real_services):
    for name in ("reader", "cockpit", "mcp"):
        service = real_services[name]
        assert service.process.poll() is None
        assert service.log_path.is_file()
        assert resource.prlimit(service.process.pid, resource.RLIMIT_CPU)[0] == SERVICE_CPU_SECONDS
        assert resource.prlimit(service.process.pid, resource.RLIMIT_AS)[0] == \
            SERVICE_ADDRESS_SPACE_BYTES
        assert resource.prlimit(service.process.pid, resource.RLIMIT_NOFILE)[0] == SERVICE_OPEN_FILES
        assert resource.prlimit(service.process.pid, resource.RLIMIT_NPROC)[0] == SERVICE_PROCESSES
        assert resource.prlimit(service.process.pid, resource.RLIMIT_CORE)[0] == 0


def test_service_start_dependency_failure_is_loud_and_bounded(tmp_path):
    port = _free_port()
    with pytest.raises(pytest.fail.Exception, match="exited during startup"):
        _start_service(
            "broken-dependency", [sys.executable, "-m", "module_that_does_not_exist_okengine"],
            REPO, {**os.environ, "PYTHONUNBUFFERED": "1"},
            f"http://127.0.0.1:{port}/healthz", tmp_path,
        )
    log = (tmp_path / "broken-dependency.log").read_text(encoding="utf-8")
    assert "No module named module_that_does_not_exist_okengine" in log
