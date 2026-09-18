import importlib.util
import json
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scale_qualification", ROOT / "scripts/run_scale_qualification.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_source_runner_binds_candidate_package_roots():
    assert str(ROOT) in MODULE.sys.path
    assert str(ROOT / "src") in MODULE.sys.path


class Response:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self): return b"ok ready"


def _deployment(tmp_path, pages=100_000):
    target = tmp_path / ".okengine/qualification-corpus.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"pages": pages, "sha256": "a" * 64}))
    return tmp_path


def test_executor_requires_exact_scale_and_real_endpoint_samples(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: Response())
    report = MODULE.execute({
        "required_pages": 100_000,
        "commands": [{"name": "startup", "command": ["true"], "samples": 2}],
        "overlap": [{"name": "audit", "command": ["true"]},
                    {"name": "projection", "command": ["true"]}],
        "http": [{"name": "reader", "url": "http://reader/healthz",
                  "samples": 3, "contains": "ready"}],
    }, dep)
    assert report["corpus"]["pages"] == 100_000
    assert report["phases"]["startup"]["samples"] == 2
    assert report["endpoints"]["reader"]["samples"] == 3
    assert set(report["phases"]["overlap"]) == {"audit", "projection"}


def test_overlap_dispatches_real_governed_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(MODULE, "governed_write", lambda deployment, path: {
        "seconds": .1, "path": path, "deployment": str(deployment)})
    result = MODULE.run_overlap(
        {"name": "write", "operation": "governed_write", "path": "entities/a.md"},
        tmp_path, 5)
    assert result["path"] == "entities/a.md"
    with pytest.raises(RuntimeError, match="lacks command"):
        MODULE.run_overlap({"name": "broken"}, tmp_path, 5)


def test_governed_write_loads_transaction_service_and_refuses_bad_results(tmp_path, monkeypatch):
    deployment = tmp_path / "deployment"
    target = deployment / "wiki/entities/a.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntype: entity\n---\nbody\n")

    class Loader:
        def __init__(self, result): self.result = result
        def create_module(self, _spec): return None
        def exec_module(self, module): module._update = lambda *_args: self.result

    def fake_spec(_name, _path, result="updated entities/a.md v2"):
        return importlib.util.spec_from_loader("qualification_write_server", Loader(result))

    monkeypatch.setattr(MODULE.importlib.util, "spec_from_file_location", fake_spec)
    monkeypatch.setenv("WIKI_PATH", "original")
    result = MODULE.governed_write(deployment, "entities/a.md")
    assert result["operation"] == "update_entity"
    assert MODULE.os.environ["WIKI_PATH"] == "original"

    monkeypatch.setattr(MODULE.importlib.util, "spec_from_file_location",
                        lambda *_args: fake_spec(None, None, "rejected: policy"))
    with pytest.raises(RuntimeError, match="governed write failed"):
        MODULE.governed_write(deployment, "entities/a.md")
    with pytest.raises(RuntimeError, match="target is missing"):
        MODULE.governed_write(deployment, "entities/missing.md")
    monkeypatch.delenv("WIKI_PATH", raising=False)
    monkeypatch.setattr(MODULE.importlib.util, "spec_from_file_location", lambda *_args: None)
    with pytest.raises(RuntimeError, match="cannot load"):
        MODULE.governed_write(deployment, "entities/a.md")
    assert "WIKI_PATH" not in MODULE.os.environ


def test_command_phase_measures_post_restart_readiness(tmp_path, monkeypatch):
    dep = _deployment(tmp_path)
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: Response())
    report = MODULE.execute({
        "commands": [{"name": "restart", "command": ["true"], "samples": 2,
                      "ready_http": {"url": "http://reader/healthz"}}],
        "http": [{"name": "reader", "url": "http://reader/healthz", "samples": 1}],
    }, dep)
    assert report["phases"]["restart"]["readiness"]["samples"] == 2


def test_reduced_corpus_zero_samples_and_absent_services_fail(tmp_path, monkeypatch):
    dep = _deployment(tmp_path, pages=99_999)
    try:
        MODULE.execute({"required_pages": 100_000}, dep)
    except RuntimeError as exc:
        assert "exactly 100000" in str(exc)
    else:
        raise AssertionError("reduced corpus accepted")
    try:
        MODULE.summarize([])
    except ValueError as exc:
        assert "zero samples" in str(exc)
    else:
        raise AssertionError("zero samples accepted")
    _deployment(tmp_path, pages=100_000)
    try:
        MODULE.execute({"required_pages": 100_000, "http": []}, tmp_path)
    except RuntimeError as exc:
        assert "no required service endpoints" in str(exc)
    else:
        raise AssertionError("absent services accepted")


def test_command_and_http_failure_contracts(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="command failed"):
        MODULE.run_command(["sh", "-c", "echo failed >&2; exit 3"], tmp_path, 5)
    result = MODULE.run_command(
        ["sh", "-c", "printf %s {deployment}; printf %s {engine}"], tmp_path, 5)
    assert str(tmp_path) in result["stdout"] and str(ROOT) in result["stdout"]

    seen = {}
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *_args, **kwargs: (
        seen.update(kwargs) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    MODULE.run_command(["docker", "compose", "ps"], tmp_path, 5)
    assert seen["env"]["COMPOSE_FILE"].split(MODULE.os.pathsep) == [
        str(tmp_path / "docker-compose.yml"),
        str(ROOT / "config/docker-compose.qualification.yml"),
    ]

    bad_status = Response()
    bad_status.status = 503
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: bad_status)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        MODULE.sample_http({"name": "reader", "url": "http://reader", "samples": 1})
    bad_marker = Response()
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: bad_marker)
    with pytest.raises(RuntimeError, match="lacks required marker"):
        MODULE.sample_http({"name": "reader", "url": "http://reader", "samples": 1,
                            "contains": "missing"})


def test_readiness_retry_and_timeout(monkeypatch):
    calls = 0

    def flaky(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("starting")
        return Response()

    monkeypatch.setattr(MODULE.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    assert MODULE.wait_http({"url": "http://reader", "contains": "ready"}, 5) >= 0

    class Starting(Response):
        def read(self): return b"not yet"

    responses = iter((Starting(), Response()))
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: next(responses))
    assert MODULE.wait_http({"url": "http://reader", "contains": "ready"}, 5) >= 0

    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(MODULE.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(
        OSError("down")))
    with pytest.raises(RuntimeError, match="did not become ready.*down"):
        MODULE.wait_http({"url": "http://reader", "interval_seconds": .01}, 1)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.test/data", "", "reader"])
def test_http_probes_reject_non_http_urls(url):
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        MODULE.sample_http({"name": "reader", "url": url, "samples": 1})
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        MODULE.wait_http({"url": url}, 1)


def _install_fake_mcp(monkeypatch, *, is_error=False):
    class AsyncClient:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None

    class Session:
        def __init__(self, *_args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def initialize(self): return None
        async def call_tool(self, _tool, _arguments):
            return SimpleNamespace(isError=is_error)

    @asynccontextmanager
    async def transport(_url, http_client=None):
        yield object(), object(), object()

    httpx = ModuleType("httpx")
    httpx.AsyncClient = AsyncClient
    mcp = ModuleType("mcp")
    mcp.ClientSession = Session
    client = ModuleType("mcp.client")
    stream = ModuleType("mcp.client.streamable_http")
    stream.streamable_http_client = transport
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.client", client)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", stream)


def test_mcp_samples_real_session_contract(monkeypatch):
    spec = {"name": "mcp", "url": "http://mcp", "token": "t", "tool": "get_page",
            "samples": 2}
    _install_fake_mcp(monkeypatch)
    assert MODULE.sample_mcp(spec)["samples"] == 2
    _install_fake_mcp(monkeypatch, is_error=True)
    with pytest.raises(RuntimeError, match="returned an error"):
        MODULE.sample_mcp({**spec, "samples": 1})


def test_backup_restore_requires_one_complete_archive(tmp_path, monkeypatch):
    dep = _deployment(tmp_path / "deployment")
    page = tmp_path / "restored/wiki/entities/a.md"
    page.parent.mkdir(parents=True)
    page.write_text("page")
    assert MODULE._page_count(tmp_path / "restored") == 1
    monkeypatch.setattr(MODULE, "run_command", lambda *_args: {"seconds": 1.0})
    with pytest.raises(RuntimeError, match="produced 0 archives"):
        MODULE.backup_restore(dep, 5)

    def command(args, _deployment, _timeout):
        if "create" in args:
            destination = Path(args[args.index("--dest") + 1])
            destination.mkdir(parents=True)
            (destination / "backup.tar.gz").write_bytes(b"archive")
        return {"seconds": 1.0}

    monkeypatch.setattr(MODULE, "run_command", command)
    monkeypatch.setattr(MODULE, "_page_count", lambda _root: 100_000)
    result = MODULE.backup_restore(dep, 5)
    assert result["source_pages"] == result["restored_pages"] == 100_000
    assert result["archive_bytes"] == 7
    counts = iter((10, 9))
    monkeypatch.setattr(MODULE, "_page_count", lambda _root: next(counts))
    with pytest.raises(RuntimeError, match="source backup had 10"):
        MODULE.backup_restore(dep, 5)


def test_executor_backup_mcp_and_cli_artifact(tmp_path, monkeypatch, capsys):
    dep = _deployment(tmp_path / "deployment")
    monkeypatch.setattr(MODULE, "backup_restore", lambda *_args: {"restored_pages": 100_000})
    monkeypatch.setattr(MODULE, "sample_mcp", lambda _spec: {"samples": 1})
    report = MODULE.execute({"backup_restore": True, "mcp": [{"name": "mcp"}]}, dep)
    assert report["phases"]["backup_restore"]["restored_pages"] == 100_000

    plan = tmp_path / "plan.json"
    artifact = tmp_path / "artifacts/report.json"
    plan.write_text(json.dumps({"http": [{"name": "reader"}]}))
    monkeypatch.setattr(MODULE, "execute", lambda *_args: {"ok": True})
    assert MODULE.main(["--plan", str(plan), "--deployment", str(dep),
                        "--artifact", str(artifact)]) == 0
    assert json.loads(artifact.read_text()) == {"ok": True}
    assert '"ok": true' in capsys.readouterr().out
    plan.write_text("{")
    assert MODULE.main(["--plan", str(plan), "--deployment", str(dep),
                        "--artifact", str(artifact)]) == 1
    assert "QUALIFICATION FAILED" in capsys.readouterr().err


def test_memory_samples_raw_samples_and_budget_enforcement(tmp_path, monkeypatch):
    assert MODULE.memory_bytes('{"MemUsage":"1.5GiB / 8GiB"}\n') == int(1.5 * 1024 ** 3)
    assert MODULE.memory_bytes(
        '{"MemUsage":"1GiB / 8GiB"}\n{"MemUsage":"512MiB / 8GiB"}\n'
    ) == int(1.5 * 1024 ** 3)
    with pytest.raises(RuntimeError, match="no parseable"):
        MODULE.memory_bytes("none")
    dep = _deployment(tmp_path)
    monkeypatch.setattr(MODULE, "run_command", lambda *_args: {
        "seconds": .25, "stdout": '{"MemUsage":"512MiB / 8GiB"}\n'})
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: Response())
    report = MODULE.execute({
        "commands": [{"name": "steady_state_memory", "command": ["stats"],
                      "measure": "memory", "samples": 2}],
        "http": [{"name": "reader", "url": "http://reader", "samples": 1}],
        "budgets": {"steady_state_memory_bytes": 1024 ** 3,
                    "phase_p95_seconds": {"steady_state_memory": 1},
                    "endpoint_p95_seconds": {"reader": 1},
                    "disk_growth_bytes": 1024 ** 4},
    }, dep)
    assert report["phases"]["steady_state_memory"]["raw_memory_bytes"] == [
        512 * 1024 ** 2, 512 * 1024 ** 2]
    assert report["phases"]["steady_state_memory"]["raw_seconds"] == [.25, .25]

    for budget in (
        {"phase_p95_seconds": {"missing": 1}},
        {"endpoint_p95_seconds": {"missing": 1}},
        {"steady_state_memory_bytes": 1},
        {"disk_growth_bytes": -1},
    ):
        with pytest.raises(RuntimeError, match="budget failure"):
            MODULE.enforce_budgets(report, budget)
