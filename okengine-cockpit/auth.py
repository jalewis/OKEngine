"""Authorization leaf for the Cockpit HTTP surface."""
from __future__ import annotations

import base64
import hmac


class BasicAuth:
    """ASGI Basic authentication with an unauthenticated health endpoint."""

    def __init__(self, app, user: str, password: str):
        self.app = app
        self._expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") != "/healthz":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            if not hmac.compare_digest(provided, self._expected):
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"www-authenticate", b'Basic realm="okengine-cockpit"'),
                    (b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def exposure_refusal(trust: str, bind_host: str, password: str) -> str | None:
    """Return a fail-closed startup reason for exposed private deployments."""
    if trust.strip().lower() != "private":
        return None
    if bind_host.strip() in ("", "127.0.0.1", "localhost", "::1") or password:
        return None
    return (
        f"okengine-cockpit REFUSED to start: PRIVATE vault exposed on {bind_host.strip()!r} "
        "with no OKENGINE_READER_PASSWORD. Set a password, bind to loopback "
        "(OKENGINE_BIND=127.0.0.1), or declare the pack `trust: public`. "
        "(okengine#90 P4a)"
    )
