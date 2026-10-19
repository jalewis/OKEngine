"""The one rule every okengine HTTP auth middleware applies to the Authorization header (okengine#966).

ASGI hands header values over as bytes. Credentials are ASCII (RFC 9110 `token68`, Basic's base64,
a Bearer token), and `hmac.compare_digest` refuses a non-ASCII `str` with a TypeError. Decoding
the header as UTF-8 therefore turned two kinds of junk into a 500 and a logged traceback instead of
a 401: bytes that are not UTF-8 at all (UnicodeDecodeError) and valid UTF-8 that is not ASCII
(TypeError at the comparison). Both are simply requests without valid credentials.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def authorization_header(scope: Mapping[str, Any]) -> str:
    """The request's Authorization header as ASCII text; "" when absent or not ASCII.

    "" never matches a configured credential, so an undecodable header takes the caller's normal
    401 path. Never decode the header any other way in a middleware.
    """
    raw = dict(scope.get("headers") or []).get(b"authorization", b"")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return ""
