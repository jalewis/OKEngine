"""Every okengine HTTP auth middleware answers junk Authorization bytes with 401, not 500 (okengine#966).

Five middlewares used to decode the header with a bare `.decode()`. Bytes that are not UTF-8 raised
UnicodeDecodeError, and valid UTF-8 that is not ASCII raised TypeError at `hmac.compare_digest`:
either way an unauthenticated client got a 500 and the server logged a traceback, on every request.
They now share `okengine.http_auth.authorization_header`, and these tests drive the real middleware
objects the services build. The write server's middleware is taken from the loaded write_server
module, because its methods are rebound into that module's globals: a helper imported only in
transport.py would be a NameError there on every authenticated request.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

from okengine.http_auth import authorization_header

ROOT = Path(__file__).resolve().parents[1]
NOT_UTF8 = b"Basic \xff\xfe"
NOT_ASCII = "Bearer tökén".encode()
USER, PASSWORD, TOKEN = "operator", "s3cret", "admin-token"


# --- the helper ----------------------------------------------------------------------------------

@pytest.mark.parametrize("headers, expected", [
    ([(b"authorization", b"Bearer abc")], "Bearer abc"),
    ([], ""),
    (None, ""),
    ([(b"authorization", NOT_UTF8)], ""),
    ([(b"authorization", NOT_ASCII)], ""),
])
def test_authorization_header_is_ascii_or_empty(headers, expected):
    assert authorization_header({"headers": headers}) == expected


def test_a_scope_without_headers_has_no_credentials():
    assert authorization_header({}) == ""


# --- every middleware ----------------------------------------------------------------------------

def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _basic_auth(surface):
    def build(inner, _tmp_path, _monkeypatch):
        auth = _load(f"auth_header_{surface}", ROOT / f"okengine-{surface}" / "auth.py")
        valid = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        return auth.BasicAuth(inner, USER, PASSWORD), valid
    return build


def _read_mcp(inner, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_PY", sys.executable)
    server = _load("auth_header_read_mcp", ROOT / "okengine-mcp" / "server.py")
    return server._ScopedAuth(inner, TOKEN), f"Bearer {TOKEN}"


def _write_mcp(inner, tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    (tmp_path / "wiki").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {sources: {}}}\n"
        "permissions: {default: {create: true, update: true, delete: false}}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    for name in ("OKENGINE_WRITE_ACTOR", "OKENGINE_WRITE_TOOLS", "OKENGINE_WRITE_TRANSPORT",
                 "OKENGINE_WRITE_TOKEN", "OKENGINE_MCP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    write_server = _load("auth_header_write_mcp", ROOT / "okengine-mcp" / "write_server.py")
    return write_server._ScopedWriteAuth(inner, TOKEN), f"Bearer {TOKEN}"


def _operations(inner, _tmp_path, _monkeypatch):
    pytest.importorskip("fastapi")
    api = _load("auth_header_operations", ROOT / "okengine-operations" / "app.py")
    api.TOKEN = TOKEN
    return api.BearerAuth(inner), f"Bearer {TOKEN}"


MIDDLEWARES = {
    "reader": _basic_auth("reader"),
    "cockpit": _basic_auth("cockpit"),
    "read-mcp": _read_mcp,
    "write-mcp": _write_mcp,
    "operations": _operations,
}


def _call(middleware, authorization: bytes):
    reached, sent = [], []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/x", "method": "GET", "headers": [(b"authorization", authorization)]}
    asyncio.run(middleware(scope, receive, send))
    return reached, sent


@pytest.fixture(params=sorted(MIDDLEWARES))
def middleware(request, tmp_path, monkeypatch):
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    built, valid = MIDDLEWARES[request.param](inner, tmp_path, monkeypatch)
    return built, valid, reached


@pytest.mark.parametrize("junk", [NOT_UTF8, NOT_ASCII], ids=["not-utf8", "utf8-not-ascii"])
def test_an_undecodable_authorization_header_is_a_401(middleware, junk):
    built, _valid, reached = middleware
    _, sent = _call(built, junk)
    assert sent and sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401, f"expected 401 for {junk!r}, got {sent[0]['status']}"
    assert reached == [], "an undecodable header must never reach the application"


def test_the_correct_credential_still_reaches_the_application(middleware):
    built, valid, reached = middleware
    _, sent = _call(built, valid.encode())
    assert sent == [] and reached == ["/x"]


# --- one rule at one boundary --------------------------------------------------------------------

def test_no_service_decodes_the_authorization_header_itself():
    """A sixth middleware with its own `.decode()` would bring the 500 back."""
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, check=True,
                           capture_output=True, text=True).stdout.split()
    pattern = re.compile(r'b"authorization".*\.decode\(')
    offenders = [
        path for path in files
        if not path.startswith("tests/") and path != "src/okengine/http_auth.py"
        and pattern.search((ROOT / path).read_text(errors="replace"))
    ]
    assert offenders == [], (
        f"decode the Authorization header only through okengine.http_auth.authorization_header: {offenders}")
