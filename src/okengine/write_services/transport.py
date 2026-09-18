from __future__ import annotations
# ruff: noqa: F821

import hmac
from typing import Mapping
from starlette.requests import Request as StarletteRequest


def _fence_tool_registrations(mcp_tool, caller_context, wiki_path):
    """Wrap every registered write tool in the corpus-wide mutation fence."""
    import functools

    def transactional_tool(*decorator_args, **decorator_kwargs):
        register = mcp_tool(*decorator_args, **decorator_kwargs)

        def decorate(function):
            @functools.wraps(function)
            def fenced(*args, **kwargs):
                caller = caller_context()
                writer = str(caller.get("actor") or caller.get("ext_id") or "mcp-write")
                # touched-mode fence (okengine#666): the write services declare every page they
                # mutate via corpus_transaction.touch(), so the receipt costs O(touched), not O(vault).
                with _corpus_mutation(
                    wiki_path().parent, writer=writer, operation=function.__name__,
                    tracking="touched",
                ):
                    return function(*args, **kwargs)

            return register(fenced)

        return decorate

    return transactional_tool


class _ScopedWriteAuth:
    """ASGI middleware for the networked write surface (okengine#132): resolve
    `Bearer <token>` -> caller, 401 if unknown. The configured admin token
    (OKENGINE_MCP_TOKEN / OKENGINE_WRITE_TOKEN) keeps FULL write; an extension token
    from the vault store is limited to its write scopes. This surface is what lets an
    out-of-process sidecar reach okengine-write at all — stdio cannot."""

    def __init__(self, app, admin_token: str):
        self.app, self.admin_token = app, admin_token

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") != "/healthz":
            headers = dict(scope.get("headers") or [])
            provided = headers.get(b"authorization", b"").decode()
            token = provided[7:] if provided.startswith("Bearer ") else ""
            caller = None
            if self.admin_token and hmac.compare_digest(token, self.admin_token):
                caller = {"kind": "admin", "actor": "admin", "write_scopes": None, "ext_id": None}
            else:
                rec = _scope.resolve(token)
                if rec is not None:
                    ext_id = rec.get("ext_id")
                    caller = {
                        "kind": "extension",
                        "ext_id": ext_id,
                        "actor": rec.get("actor") or f"extension:{ext_id}",
                        "write_scopes": rec.get("write_scopes") or [],
                        "write_capability": rec.get("write_capability") or {},
                    }
            if caller is None:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            _caller_var.set(caller)
        await self.app(scope, receive, send)


def _review_http_app():
    """Small review-only REST surface for a protected operator UI.

    It deliberately does not expose generic entity mutation. `_ScopedWriteAuth` wraps the whole
    service, while the operation itself enforces version/hash locking and the review state machine.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    review_app = FastAPI(title="OKEngine review write", docs_url=None, redoc_url=None)

    @review_app.get("/healthz")
    def healthz():
        return {"ok": True}

    @review_app.post("/review/resolve")
    async def review_resolve(request: StarletteRequest):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = _resolve_review(
            str(data.get("path") or ""),
            str(data.get("decision") or ""),
            str(data.get("reviewer") or ""),
            str(data.get("note") or ""),
            data.get("expected_version"),
            str(data.get("expected_hash") or ""),
            str(data.get("review_id") or "") or None,
            service=str(data.get("service") or "cockpit"),
        )
        return JSONResponse(result, status_code=int(result.get("status") or 500))

    @review_app.post("/review/assign")
    async def review_assign(request: StarletteRequest):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = _assign_review(
            str(data.get("path") or ""),
            str(data.get("reviewer") or ""),
            data.get("expected_version"),
            str(data.get("expected_hash") or ""),
            str(data.get("review_id") or "") or None,
            service=str(data.get("service") or "cockpit"),
        )
        return JSONResponse(result, status_code=int(result.get("status") or 500))

    @review_app.post("/review/machine")
    async def review_machine(request: StarletteRequest):
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        result = _record_machine_review(
            str(data.get("path") or ""),
            str(data.get("evaluator") or "machine"),
            str(data.get("outcome") or ""),
            str(data.get("note") or ""),
        )
        return JSONResponse(result, status_code=int(result.get("status") or 500))

    return review_app


def _resolve_write_auth(env: Mapping[str, str], host: str) -> str:
    """The admin bearer token for the networked write transport, or raise SystemExit (fail CLOSED).

    - empty (no WRITE_TOKEN and no MCP_TOKEN) → refuse: writes must be authenticated.
    - the built-in DEFAULT token while bound beyond loopback → refuse unless
      OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN=1 (the public token can't guard a networked WRITE surface).
      On loopback the default is painless, same as the read server."""
    admin = env.get("OKENGINE_WRITE_TOKEN") or env.get("OKENGINE_MCP_TOKEN") or ""
    if not admin:
        raise SystemExit(
            "okengine-write: networked transport requires OKENGINE_WRITE_TOKEN "
            "(or OKENGINE_MCP_TOKEN) — refusing to serve writes unauthenticated."
        )
    exposed = host not in _LOOPBACK
    if (
        admin == DEFAULT_LOCAL_TOKEN
        and exposed
        and env.get("OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN", "") != "1"
    ):
        raise SystemExit(
            f"okengine-write: refusing to bind {host} with the built-in DEFAULT token — it is "
            "public, and this is the ENFORCED WRITE path. Set OKENGINE_WRITE_TOKEN (or "
            "OKENGINE_MCP_TOKEN) to a secret, or OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN=1 to override."
        )
    return admin
