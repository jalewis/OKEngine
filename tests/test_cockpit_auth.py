import asyncio
import base64
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cockpit_auth", ROOT / "okengine-cockpit/auth.py")
AUTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTH)


def test_exposure_refusal_is_a_leaf_fail_closed_policy():
    assert AUTH.exposure_refusal("private", "0.0.0.0", "")
    assert AUTH.exposure_refusal("private", "127.0.0.1", "") is None
    assert AUTH.exposure_refusal("private", "0.0.0.0", "secret") is None
    assert AUTH.exposure_refusal("public", "0.0.0.0", "") is None


def test_basic_auth_preserves_health_and_challenge_contract():
    downstream = []
    sent = []

    async def app(scope, _receive, _send):
        downstream.append(scope["path"])

    async def send(message):
        sent.append(message)

    async def receive():
        return {}

    auth = AUTH.BasicAuth(app, "operator", "secret")
    asyncio.run(auth({"type": "http", "path": "/healthz"}, receive, send))
    asyncio.run(auth({"type": "http", "path": "/private", "headers": []}, receive, send))
    header = b"Basic " + base64.b64encode(b"operator:secret")
    asyncio.run(auth({"type": "http", "path": "/ok", "headers": [
        (b"authorization", header)]}, receive, send))
    assert downstream == ["/healthz", "/ok"]
    assert sent[0]["status"] == 401 and sent[1]["body"] == b"unauthorized"
