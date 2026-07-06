"""Regression tests for scoped MCP tokens (okengine#132).

Covers the pure pieces and the load-bearing cross-module contract: a token MINTED
host-side (scripts/extension_tokens.py) must RESOLVE container-side
(okengine-mcp/scope.py) to the right scopes. Auth-wrapper behavior (admin=full,
extension=scoped, provenance stamp) is unit-tested where importable; full ASGI
propagation + the extension_id stamp are verified on a live deploy.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCOPE_PATH = REPO / "okengine-mcp" / "scope.py"
TOKENS_PATH = REPO / "scripts" / "extension_tokens.py"

pytestmark = pytest.mark.skipif(not (SCOPE_PATH.is_file() and TOKENS_PATH.is_file()),
                                reason="scoped-MCP modules not present")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _scope():
    return _load("scope", SCOPE_PATH)


def _tokens():
    return _load("extension_tokens", TOKENS_PATH)


# --- scope.path_in_scopes -------------------------------------------------

@pytest.mark.parametrize("rel,scopes,ok", [
    ("dashboards/x", ["wiki/**"], True),            # full vault
    ("dashboards/contradictions", ["dashboards/**"], True),
    ("dashboards/contradictions.md", ["dashboards/**"], True),   # .md tolerated
    ("entities/a/y", ["dashboards/**"], False),     # out of scope
    ("predictions/p", ["predictions/**", "dashboards/**"], True),
    ("anything", ["**"], True),
    ("x", [], False),                               # no scopes = nothing
])
def test_path_in_scopes(rel, scopes, ok):
    assert _scope().path_in_scopes(rel, scopes) is ok


def test_is_full():
    s = _scope()
    assert s.is_full(["wiki/**"])
    assert s.is_full(["**"])
    assert not s.is_full(["dashboards/**"])


# --- extension_tokens mint/revoke ----------------------------------------

def test_scopes_from_manifest():
    tok = _tokens()
    man = {"capabilities": {"read": ["wiki/**"], "write": ["dashboards/**"]}}
    assert tok.scopes_from_manifest(man) == (["wiki/**"], ["dashboards/**"])
    assert tok.scopes_from_manifest({}) == ([], [])


def test_mint_writes_hashed_store_and_plaintext_secret(tmp_path):
    tok = _tokens()
    plaintext = tok.mint(tmp_path, "demo.alpha", ["wiki/**"], ["dashboards/**"])
    store = json.loads((tmp_path / ".okengine" / "extension-tokens.json").read_text())
    rec = next(r for r in store["tokens"] if r["ext_id"] == "demo.alpha")
    # store holds the HASH, never the plaintext
    assert rec["token_sha256"] != plaintext
    assert plaintext not in json.dumps(store)
    assert rec["read_scopes"] == ["wiki/**"] and rec["write_scopes"] == ["dashboards/**"]
    assert rec["status"] == "active"
    # plaintext lives only in the 0600 secrets file
    sec_path = tmp_path / ".okengine" / "extension-secrets.json"
    assert json.loads(sec_path.read_text())["demo.alpha"] == plaintext
    assert (sec_path.stat().st_mode & 0o777) == 0o600


def test_revoke_removes_from_store_and_secrets(tmp_path):
    tok = _tokens()
    tok.mint(tmp_path, "demo.alpha", ["wiki/**"], ["dashboards/**"])
    tok.mint(tmp_path, "demo.beta", ["wiki/**"], ["predictions/**"])
    tok.revoke(tmp_path, "demo.alpha")
    store = json.loads((tmp_path / ".okengine" / "extension-tokens.json").read_text())
    ids = {r["ext_id"] for r in store["tokens"]}
    assert ids == {"demo.beta"}
    sec = json.loads((tmp_path / ".okengine" / "extension-secrets.json").read_text())
    assert "demo.alpha" not in sec and "demo.beta" in sec


# --- THE cross-module contract: mint host-side, resolve container-side ----

def test_mint_then_resolve_roundtrip(tmp_path, monkeypatch):
    tok = _tokens()
    plaintext = tok.mint(tmp_path, "demo.alpha", ["wiki/news/**"], ["dashboards/**"])
    store_file = tmp_path / ".okengine" / "extension-tokens.json"
    monkeypatch.setenv("OKENGINE_EXT_TOKEN_STORE", str(store_file))

    sc = _scope()
    rec = sc.resolve(plaintext)
    assert rec is not None
    assert rec["ext_id"] == "demo.alpha"
    assert rec["read_scopes"] == ["wiki/news/**"]
    assert rec["write_scopes"] == ["dashboards/**"]
    # wrong token resolves to nothing
    assert sc.resolve("not-a-real-token") is None
    # revoked token no longer resolves
    tok.revoke(tmp_path, "demo.alpha")
    sc2 = _load("scope2", SCOPE_PATH)            # fresh module = fresh mtime cache
    assert sc2.resolve(plaintext) is None


# --- write_server authorize wrapper (importable without the mcp package) ---

def _write_server():
    if importlib.util.find_spec("yaml") is None:
        pytest.skip("yaml not available")
    try:
        return _load("write_server", REPO / "okengine-mcp" / "write_server.py")
    except Exception as e:
        pytest.skip(f"write_server import unavailable: {e}")


def test_write_authorize_admin_full_extension_scoped():
    ws = _write_server()
    # default caller (stdio / None) = admin = full write (back-compat)
    assert ws._authorize_write("entities/a/x") is True
    # an extension caller is limited to its write scopes
    tokn = ws._caller_var.set({"kind": "extension", "ext_id": "demo.alpha",
                               "write_scopes": ["dashboards/**"]})
    try:
        assert ws._authorize_write("dashboards/contradictions") is True
        assert ws._authorize_write("entities/a/x") is False
        assert "outside extension" in (ws._wauth_refusal("entities/a/x") or "")
        assert ws._wauth_refusal("dashboards/x") is None
    finally:
        ws._caller_var.reset(tokn)
