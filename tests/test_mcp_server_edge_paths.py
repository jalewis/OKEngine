"""Read-MCP authentication and process failure-path coverage."""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "okengine-mcp" / "server.py"


def _load(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir(parents=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    name = "mcp_server_edges"
    spec = importlib.util.spec_from_file_location(name, SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_scoped_auth_admin_extension_rejection_and_non_http(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    called = []

    async def app(scope, receive, send):
        called.append((scope["type"], m._caller()))

    middleware = m._ScopedAuth(app, "admin-secret")

    async def invoke(kind="http", authorization=None):
        messages = []
        headers = [] if authorization is None else [(b"authorization", authorization)]

        async def send(message):
            messages.append(message)

        await middleware(
            {"type": kind, "headers": headers},
            lambda: None,
            send,
        )
        return messages

    denied = asyncio.run(invoke())
    assert denied[0]["status"] == 401
    asyncio.run(invoke(authorization=b"Bearer admin-secret"))
    assert called[-1][1]["kind"] == "admin"

    monkeypatch.setattr(
        m._scope,
        "resolve",
        lambda token: {
            "ext_id": "ext.one",
            "read_scopes": ["wiki/entities"],
        } if token == "extension" else None,
    )
    asyncio.run(invoke(authorization=b"Bearer extension"))
    assert called[-1][1]["kind"] == "extension"
    assert called[-1][1]["read_scopes"] == ["wiki/entities"]
    asyncio.run(invoke(kind="lifespan"))
    assert called[-1][0] == "lifespan"

    token = m._caller_var.set(
        {"kind": "extension", "read_scopes": ["wiki/entities"]}
    )
    try:
        monkeypatch.setattr(
            m._scope,
            "path_in_scopes",
            lambda path, scopes: path.startswith("wiki/entities") and bool(scopes),
        )
        assert m._authorize_read("wiki/entities/a") is True
        assert m._authorize_read("wiki/sources/a") is False
    finally:
        m._caller_var.reset(token)


def test_run_success_timeout_and_kill_fallback(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)

    class Proc:
        pid = 123

        def __init__(self, timeout=False, stdout="", stderr=""):
            self.timeout = timeout
            self.stdout, self.stderr = stdout, stderr
            self.calls = 0
            self.killed = False

        def communicate(self, timeout=None):
            self.calls += 1
            if self.timeout and self.calls == 1:
                raise subprocess.TimeoutExpired(["x"], timeout)
            return self.stdout, self.stderr

        def kill(self):
            self.killed = True

    good = Proc(stdout=" output ")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_kw: good)
    assert m._run(["script.py"]) == "output"
    empty = Proc(stderr=" error ")
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_kw: empty)
    assert m._run(["script.py"]) == "error"

    timed = Proc(timeout=True)
    monkeypatch.setattr(m.subprocess, "Popen", lambda *_a, **_kw: timed)
    monkeypatch.setattr(
        m.os,
        "killpg",
        lambda *_a: (_ for _ in ()).throw(PermissionError()),
    )
    assert m._run(["script.py"], timeout=1) == "(query timed out)"
    assert timed.killed is True

    already_gone = Proc()
    monkeypatch.setattr(
        m.os,
        "killpg",
        lambda *_a: (_ for _ in ()).throw(ProcessLookupError()),
    )
    already_gone.kill = lambda: (_ for _ in ()).throw(ProcessLookupError())
    m._kill_process_group(already_gone)


def test_qmd_absent_capacity_and_refresh_registration(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(
        m,
        "_QMD_CAPACITY",
        type(
            "Capacity",
            (),
            {
                "acquire": lambda self, **_kw: False,
                "release": lambda self: None,
            },
        )(),
    )
    assert m._qmd(["update"])[0] == 75

    class Capacity:
        def acquire(self, **_kw):
            return True

        def release(self):
            pass

    monkeypatch.setattr(m, "_QMD_CAPACITY", Capacity())
    monkeypatch.setattr(
        m.subprocess,
        "Popen",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert m._qmd(["update"]) == (127, "qmd not installed")

    calls = []

    def qmd(args, timeout=1800):
        calls.append(args)
        if args == ["collection", "list"]:
            return 0, "no collections"
        return 0, ""

    monkeypatch.setattr(m, "_qmd", qmd)
    assert m._refresh_index_locked() is True
    assert ["collection", "add", str(m.WIKI)] in calls
    assert ["update"] in calls


def test_async_runner_success_timeout_and_cancellation(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)

    class Proc:
        pid = 321

        def __init__(self, result=(b"", b""), error=None):
            self.result, self.error, self.calls = result, error, 0

        async def communicate(self):
            self.calls += 1
            if self.error and self.calls == 1:
                raise self.error
            return self.result

    good = Proc((b" stdout ", b""))

    async def make_good(*_args, **_kwargs):
        return good

    monkeypatch.setattr(m.asyncio, "create_subprocess_exec", make_good)
    assert asyncio.run(m._run_async(["script.py"])) == "stdout"

    stderr = Proc((b"", b" stderr "))

    async def make_stderr(*_args, **_kwargs):
        return stderr

    monkeypatch.setattr(m.asyncio, "create_subprocess_exec", make_stderr)
    assert asyncio.run(m._run_async(["script.py"])) == "stderr"

    timed = Proc(error=TimeoutError())

    async def make_timed(*_args, **_kwargs):
        return timed

    killed = []
    monkeypatch.setattr(m.asyncio, "create_subprocess_exec", make_timed)
    monkeypatch.setattr(m, "_kill_process_group", lambda proc: killed.append(proc))
    try:
        asyncio.run(m._run_async(["script.py"]))
    except TimeoutError:
        pass
    else:
        raise AssertionError("timeout must propagate")
    assert killed == [timed]

    cancelled = Proc(error=asyncio.CancelledError())

    async def make_cancelled(*_args, **_kwargs):
        return cancelled

    killed.clear()
    monkeypatch.setattr(m.asyncio, "create_subprocess_exec", make_cancelled)
    try:
        asyncio.run(m._run_async(["script.py"]))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("cancellation must propagate")
    assert killed == [cancelled]
