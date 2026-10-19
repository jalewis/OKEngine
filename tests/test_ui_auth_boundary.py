"""Security-boundary contract for the reader and cockpit Basic-auth leaf (okengine#612).

Both UIs share the same ``auth.py`` shape: an ASGI Basic-auth middleware that exempts only the
exact ``/healthz`` path, and a fail-closed startup refusal for a private vault exposed without a
password. Each test runs against BOTH modules so neither surface can drift from the other.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

import pytest

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

ROOT = Path(__file__).resolve().parents[1]
USER = "operator"
PASSWORD = "s3cret:with-colon"
REALMS = {"cockpit": b'Basic realm="okengine-cockpit"', "reader": b'Basic realm="okengine-reader"'}


def _load(surface: str):
    spec = importlib.util.spec_from_file_location(
        f"okengine_{surface}_auth_boundary", ROOT / f"okengine-{surface}" / "auth.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULES = {surface: _load(surface) for surface in REALMS}


@pytest.fixture(params=sorted(REALMS))
def surface(request):
    return request.param


@pytest.fixture
def auth(surface):
    return MODULES[surface]


def _basic(user: str = USER, password: str = PASSWORD) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _app(auth, lifecycle: list[str] | None = None):
    events = lifecycle if lifecycle is not None else []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("startup")
        yield
        events.append("shutdown")

    app = fastapi.FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def root():
        return {"page": "root"}

    @app.get("/api/pages")
    def pages():
        return {"page": "api"}

    @app.get("/private")
    def private():
        return {"page": "private"}

    @app.get("/{rest:path}")
    def anything(rest: str):
        return {"page": rest}

    @app.middleware("http")
    async def record(request, call_next):
        # Registered before BasicAuth, so it sits INSIDE it: only admitted requests reach it.
        app.state.reached.append(request.url.path)
        return await call_next(request)

    app.state.reached = []

    app.add_middleware(auth.BasicAuth, user=USER, password=PASSWORD)
    return app


# Paths sorting before, after, and as prefixes/extensions of "/healthz": an ordering comparison
# substituted for the exact exemption match would let one side of that boundary through.
PROTECTED_PATHS = ["/", "/api/pages", "/a", "/private", "/zzz", "/healthz/", "/healthzx",
                   "/healthy", "/HEALTHZ", "/health"]


@pytest.mark.parametrize("path", PROTECTED_PATHS)
def test_every_path_except_the_exact_health_probe_refuses_anonymous_requests(auth, surface, path):
    app = _app(auth)
    with testclient.TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 401, f"{surface}: {path} served without credentials"
    assert app.state.reached == []
    assert response.content == b"unauthorized"
    assert response.headers["www-authenticate"].encode() == REALMS[surface]
    assert response.headers["content-type"] == "text/plain"


@pytest.mark.parametrize("path", ["/", "/api/pages", "/private", "/zzz"])
def test_the_configured_credential_reaches_every_protected_path(auth, path):
    with testclient.TestClient(_app(auth)) as client:
        response = client.get(path, headers={"Authorization": _basic()})

    assert response.status_code == 200


def test_the_health_probe_answers_without_credentials(auth):
    with testclient.TestClient(_app(auth)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200 and response.json() == {"ok": True}


@pytest.mark.parametrize("authorization", [
    _basic(password="wrong"),
    _basic(user="intruder"),
    _basic(password=PASSWORD + "x"),
    _basic(password=PASSWORD[:-1]),
    _basic(user="", password=""),
    "Bearer " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode(),
    "basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode(),
    base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode(),
    _basic() + " ",
    " " + _basic(),
    "Basic " + base64.b64encode(f"{PASSWORD}:{USER}".encode()).decode(),
    "Basic " + f"{USER}:{PASSWORD}",
    "",
], ids=["wrong-password", "wrong-user", "password-suffix", "password-prefix", "empty-pair",
        "bearer-scheme", "lowercase-scheme", "no-scheme", "trailing-space", "leading-space",
        "swapped-pair", "unencoded", "empty-header"])
def test_refused_credentials_never_reach_the_application(auth, authorization):
    app = _app(auth)

    with testclient.TestClient(app) as client:
        response = client.get("/private", headers={"Authorization": authorization})
        admitted = client.get("/private", headers={"Authorization": _basic()})

    assert response.status_code == 401, f"accepted {authorization!r}"
    assert admitted.status_code == 200
    assert app.state.reached == ["/private"]


def test_credentials_are_compared_in_constant_time_against_the_full_header(auth):
    with mock.patch.object(hmac, "compare_digest", autospec=True,
                           side_effect=hmac.compare_digest) as compare:
        with testclient.TestClient(_app(auth)) as client:
            refused = client.get("/private", headers={"Authorization": _basic(password="nope")})
            accepted = client.get("/private", headers={"Authorization": _basic()})

    assert (refused.status_code, accepted.status_code) == (401, 200)
    assert compare.call_args_list == [
        mock.call(_basic(password="nope"), _basic()),
        mock.call(_basic(), _basic()),
    ]


def test_the_application_lifespan_is_forwarded_through_the_middleware(auth):
    lifecycle: list[str] = []

    with testclient.TestClient(_app(auth, lifecycle)) as client:
        assert lifecycle == ["startup"]
        assert client.get("/healthz").status_code == 200

    assert lifecycle == ["startup", "shutdown"]


def test_an_http_scope_type_is_authenticated_by_value_not_by_identity(auth):
    # A server may build the scope type at runtime; an identity check against the interned
    # literal would wave such a request through unauthenticated.
    scope_type = "".join(["ht", "tp"])
    assert scope_type == "http" and scope_type is not "http"  # noqa: F632 - the premise under test
    reached, sent = [], []

    async def downstream(scope, _receive, _send):
        reached.append(scope["path"])

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    middleware = auth.BasicAuth(downstream, USER, PASSWORD)
    asyncio.run(middleware({"type": scope_type, "path": "/private", "headers": []}, receive, send))

    assert reached == []
    assert sent[0]["status"] == 401 and sent[1]["body"] == b"unauthorized"


def test_the_last_authorization_header_decides_when_a_client_repeats_it(auth):
    reached, sent = [], []

    async def downstream(scope, _receive, _send):
        reached.append(scope["path"])

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    middleware = auth.BasicAuth(downstream, USER, PASSWORD)
    headers = [(b"authorization", _basic().encode()), (b"authorization", b"Basic Zm9vOmJhcg==")]
    asyncio.run(middleware({"type": "http", "path": "/private", "headers": headers}, receive, send))

    assert reached == [] and sent[0]["status"] == 401


@pytest.mark.parametrize("trust", ["private", "PRIVATE", "  Private  "])
@pytest.mark.parametrize("bind", ["0.0.0.0", "10.0.0.5", " 0.0.0.0 ", "::"])
def test_a_private_vault_exposed_without_a_password_refuses_to_start(auth, surface, trust, bind):
    reason = auth.exposure_refusal(trust, bind, "")

    assert reason is not None
    assert reason.startswith(f"okengine-{surface} REFUSED to start")
    assert repr(bind.strip()) in reason
    assert "OKENGINE_READER_PASSWORD" in reason and "okengine#90 P4a" in reason


@pytest.mark.parametrize("bind", ["", "127.0.0.1", "localhost", "::1", " 127.0.0.1 ", "  "])
def test_a_private_vault_bound_to_loopback_starts_without_a_password(auth, bind):
    assert auth.exposure_refusal("private", bind, "") is None


def test_a_password_lets_an_exposed_private_vault_start(auth):
    assert auth.exposure_refusal("private", "0.0.0.0", "pw") is None


@pytest.mark.parametrize("trust", ["public", "", "community", "internal", "open", "zzz"])
def test_only_the_private_trust_level_is_refused_when_exposed(auth, trust):
    # Trust levels on both sides of "private" in sort order: the refusal keys on the exact
    # normalized value, never on an ordering.
    assert auth.exposure_refusal(trust, "0.0.0.0", "") is None


@pytest.mark.parametrize("bind", ["127.0.0.2", "localhost.example", "0.0.0.0"])
def test_a_near_loopback_bind_is_still_exposed(auth, bind):
    assert auth.exposure_refusal("private", bind, "") is not None
