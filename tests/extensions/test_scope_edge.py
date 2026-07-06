"""Adversarial edge cases for the scope matcher + token resolution (okengine#132).

The scope check is a security boundary, so it must not be fooled by prefix confusion
(`concepts/**` granting `concepts-evil/`), trailing-slash variation, or malformed
tokens/stores. These guard the matcher directly.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SCOPE = REPO / "okengine-mcp" / "scope.py"

pytestmark = pytest.mark.skipif(not SCOPE.is_file(), reason="scope.py absent")


def _scope():
    spec = importlib.util.spec_from_file_location("scope", SCOPE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["scope"] = m
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("rel,scopes,ok", [
    # prefix confusion: a sibling namespace sharing a prefix must NOT be granted
    ("concepts-evil/x", ["concepts/**"], False),
    ("conceptsx", ["concepts/**"], False),
    # the namespace root itself + nested are granted
    ("concepts", ["concepts/**"], True),
    ("concepts/a/b/c", ["concepts/**"], True),
    # leading slash + .md tolerated
    ("/concepts/a", ["concepts/**"], True),
    ("concepts/a.md", ["concepts/**"], True),
    # the 'wiki/' prefix in the scope is normalized away (capability paths are wiki-relative)
    ("dashboards/x", ["wiki/dashboards/**"], True),
    ("entities/x", ["wiki/dashboards/**"], False),
    # a bare namespace (no /**) still scopes correctly
    ("dashboards/contradictions", ["dashboards"], True),
    ("dashboards-other/x", ["dashboards"], False),
    # vault-qualified scope ("self:") — qualifier stripped
    ("concepts/a", ["self:wiki/concepts/**"], True),
    # empty / no scopes grant nothing
    ("anything", [], False),
    ("anything", None, False),
])
def test_scope_matcher_edges(rel, scopes, ok):
    assert _scope().path_in_scopes(rel, scopes) is ok


def test_full_scope_grants_all():
    s = _scope()
    for full in (["wiki/**"], ["**"], ["*"]):
        assert s.path_in_scopes("entities/deep/page", full)
        assert s.is_full(full)
    assert not s.is_full(["concepts/**"])


def test_resolve_rejects_empty_and_unknown(tmp_path, monkeypatch):
    s = _scope()
    monkeypatch.setenv("OKENGINE_EXT_TOKEN_STORE", str(tmp_path / "nope.json"))
    s._cache["mtime"] = None
    assert s.resolve("") is None
    assert s.resolve("anything") is None     # missing store -> no records -> None


def test_resolve_handles_malformed_store(tmp_path, monkeypatch):
    s = _scope()
    store = tmp_path / "tok.json"
    store.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("OKENGINE_EXT_TOKEN_STORE", str(store))
    s._cache["mtime"] = None
    assert s.resolve("anything") is None     # never raises on a corrupt store
    assert s.load_records() == []


def test_revoked_status_does_not_resolve(tmp_path, monkeypatch):
    s = _scope()
    store = tmp_path / "tok.json"
    tok = "deadbeef" * 8
    import json
    store.write_text(json.dumps({"tokens": [
        {"ext_id": "demo.x", "token_sha256": s.token_sha256(tok),
         "read_scopes": ["wiki/**"], "write_scopes": [], "status": "revoked"}]}), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_EXT_TOKEN_STORE", str(store))
    s._cache["mtime"] = None
    assert s.resolve(tok) is None            # status != active -> not resolved
