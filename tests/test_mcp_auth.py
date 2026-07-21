"""Regression: MCP HTTP auth is local-first — always comes up with a token, never
crashes for missing auth, warns only when exposed with weak/no auth (issue #20)."""
import importlib.util
import sys
from pathlib import Path

import pytest

# server.py imports `mcp` at module level; skip where that runtime dep is absent
# (same pattern as the write_server tests). Runs in CI where deps are installed.
pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parent.parent
SRV = REPO / "okengine-mcp" / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("okengine_server", SRV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["okengine_server"] = m
    spec.loader.exec_module(m)
    return m


def test_loopback_unset_token_uses_default_no_warning():
    s = _load()
    token, warning = s._resolve_http_auth({}, "127.0.0.1")
    assert token == s.DEFAULT_LOCAL_TOKEN   # always comes up authenticated
    assert warning is None                  # local default is fine, no nag


def test_exposed_default_token_fails_closed():
    """okengine#50: the built-in token is public — binding it beyond loopback must REFUSE."""
    s = _load()
    with pytest.raises(SystemExit):
        s._resolve_http_auth({}, "0.0.0.0")


def test_exposed_default_token_opt_in_serves():
    """...unless the operator explicitly accepts it — then it serves with a warning."""
    s = _load()
    token, warning = s._resolve_http_auth({"OKENGINE_MCP_ALLOW_DEFAULT_TOKEN": "1"}, "0.0.0.0")
    assert token == s.DEFAULT_LOCAL_TOKEN
    assert warning and "DEFAULT token" in warning


def test_clamp_limit_coerces_and_bounds():
    """okengine#51: caller `limit` is coerced + clamped to [1, _LIMIT_MAX]."""
    s = _load()
    assert s._clamp_limit("abc", 8) == 8              # non-int -> default
    assert s._clamp_limit(None, 40) == 40             # missing -> default
    assert s._clamp_limit(0, 8) == 1                  # floor
    assert s._clamp_limit(10_000, 8) == s._LIMIT_MAX  # ceiling
    assert s._clamp_limit(5, 8) == 5                  # in-range passthrough


def test_explicit_token_overrides_default():
    s = _load()
    token, warning = s._resolve_http_auth({"OKENGINE_MCP_TOKEN": "s3cret"}, "0.0.0.0")
    assert token == "s3cret"
    assert warning is None


def test_explicit_unauthenticated_optout():
    s = _load()
    token, warning = s._resolve_http_auth({"OKENGINE_MCP_ALLOW_UNAUTHENTICATED": "1"}, "0.0.0.0")
    assert token is None                     # serve with no auth, by explicit choice
    assert warning and "NO authentication" in warning
    # on loopback the opt-out is silent
    assert s._resolve_http_auth({"OKENGINE_MCP_ALLOW_UNAUTHENTICATED": "1"}, "127.0.0.1")[1] is None


# ── write_server networked-transport auth: the ENFORCED write path must fail closed on the public
#    default token off-loopback, mirroring the read server (invariant-audit CRITICAL) ──────────────

WS = REPO / "okengine-mcp" / "write_server.py"


def _load_write():
    spec = importlib.util.spec_from_file_location("okengine_write_server", WS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["okengine_write_server"] = m
    spec.loader.exec_module(m)
    return m


def test_write_auth_empty_token_refuses():
    w = _load_write()
    with pytest.raises(SystemExit):
        w._resolve_write_auth({}, "0.0.0.0")


def test_write_auth_default_token_exposed_fails_closed():
    """The seeded compose default (OKENGINE_MCP_TOKEN=okengine-local) bound off-loopback must be
    REFUSED — the read server guards this; write_server used to only check non-empty, so it served
    unauthenticated full write access on the network (invariant-audit CRITICAL)."""
    w = _load_write()
    with pytest.raises(SystemExit):
        w._resolve_write_auth({"OKENGINE_MCP_TOKEN": w.DEFAULT_LOCAL_TOKEN}, "0.0.0.0")


def test_write_auth_default_token_loopback_ok():
    w = _load_write()
    assert w._resolve_write_auth({"OKENGINE_MCP_TOKEN": w.DEFAULT_LOCAL_TOKEN}, "127.0.0.1") \
        == w.DEFAULT_LOCAL_TOKEN                     # loopback default is painless, like the read server


def test_write_auth_secret_exposed_ok_and_default_optin():
    w = _load_write()
    assert w._resolve_write_auth({"OKENGINE_WRITE_TOKEN": "s3cret"}, "0.0.0.0") == "s3cret"
    # explicit opt-in serves the default off-loopback (parity with OKENGINE_MCP_ALLOW_DEFAULT_TOKEN)
    assert w._resolve_write_auth(
        {"OKENGINE_MCP_TOKEN": w.DEFAULT_LOCAL_TOKEN, "OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN": "1"},
        "0.0.0.0") == w.DEFAULT_LOCAL_TOKEN
