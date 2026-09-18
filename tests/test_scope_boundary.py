"""Per-extension token scoping — the shared MCP security boundary (okengine#462, T1).

`src/okengine/mcp/scope.py` is loaded by BOTH the read server and the enforced write
server so they agree on what a per-extension token may touch (okengine#132). It
decides two security questions:

  resolve()        does this plaintext token map to an active identity?
  path_in_scopes() may that identity touch this page?

A wrong answer in either direction is a security defect: too permissive grants an
extension the whole vault, too strict breaks a legitimately-scoped extension. The
tests assert both directions, including the back-compat contract that a store
which is absent or unreadable yields NO scoped identities (so the admin token
path is unchanged) rather than failing open.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "src" / "okengine" / "mcp" / "scope.py"


def load_scope():
    spec = importlib.util.spec_from_file_location("scope_boundary", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["scope_boundary"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """Fresh module per test — load_records() memoises in a module-level cache."""
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_EXT_TOKEN_STORE", raising=False)
    return load_scope()


def write_store(path: Path, records, wrap=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": records} if wrap else records), encoding="utf-8")


# ── store_path ────────────────────────────────────────────────────────────────

def test_store_path_defaults_into_the_vault(scope, tmp_path):
    assert scope.store_path() == tmp_path / ".okengine" / "extension-tokens.json"


def test_store_path_honours_an_explicit_override(scope, tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_EXT_TOKEN_STORE", str(tmp_path / "elsewhere.json"))
    assert scope.store_path() == tmp_path / "elsewhere.json"


# ── load_records: fail CLOSED, never open ────────────────────────────────────

def test_absent_store_yields_no_scoped_identities(scope):
    """Back-compat contract: with no store, only the admin token works -- exactly as
    before okengine#132. An absent store must never be read as "everything allowed"."""
    assert scope.load_records() == []


def test_unparseable_store_yields_no_scoped_identities(scope, tmp_path):
    store = tmp_path / ".okengine" / "extension-tokens.json"
    store.parent.mkdir(parents=True)
    store.write_text("{ not json", encoding="utf-8")
    assert scope.load_records() == []


def test_store_accepts_both_wrapped_and_bare_list_shapes(scope, tmp_path):
    store = scope.store_path()
    write_store(store, [{"ext_id": "a", "token_sha256": "x"}], wrap=True)
    assert [r["ext_id"] for r in scope.load_records()] == ["a"]

    fresh = load_scope()
    write_store(store, [{"ext_id": "b", "token_sha256": "y"}], wrap=False)
    assert [r["ext_id"] for r in fresh.load_records()] == ["b"]


def test_non_mapping_entries_are_discarded(scope):
    write_store(scope.store_path(), [{"ext_id": "ok", "token_sha256": "x"}, "junk", 7, None])
    assert [r["ext_id"] for r in scope.load_records()] == ["ok"]


def test_a_non_list_tokens_payload_yields_no_identities(scope):
    store = scope.store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"tokens": {"ext_id": "a"}}), encoding="utf-8")
    assert scope.load_records() == [], "a malformed store must not grant anything"


def test_unchanged_store_is_served_from_cache(scope):
    """Both MCP servers call this on every request; re-reading the store each time
    would put a JSON parse in the hot path."""
    write_store(scope.store_path(), [{"ext_id": "a", "token_sha256": "x"}])
    first = scope.load_records()
    assert scope.load_records() is first, "unchanged store must not be re-parsed"


def test_records_are_reloaded_when_the_store_changes(scope, tmp_path):
    import os
    store = scope.store_path()
    write_store(store, [{"ext_id": "first", "token_sha256": "x"}])
    assert [r["ext_id"] for r in scope.load_records()] == ["first"]

    write_store(store, [{"ext_id": "second", "token_sha256": "y"}])
    os.utime(store, (0, 0))          # force a distinct mtime
    assert [r["ext_id"] for r in scope.load_records()] == ["second"], \
        "a rotated token store must not serve a stale cache"


# ── resolve: token -> identity ───────────────────────────────────────────────

def test_resolve_matches_an_active_token_by_hash(scope):
    token = "s3cret-token"
    write_store(scope.store_path(), [{
        "ext_id": "okengine.demo",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "write_scopes": ["dashboards/**"],
        "status": "active",
    }])
    record = scope.resolve(token)
    assert record is not None and record["ext_id"] == "okengine.demo"


def test_resolve_treats_a_missing_status_as_active(scope):
    token = "t"
    write_store(scope.store_path(),
                [{"ext_id": "e", "token_sha256": hashlib.sha256(token.encode()).hexdigest()}])
    assert scope.resolve(token) is not None


def test_revoked_tokens_do_not_resolve(scope):
    token = "revoked-token"
    write_store(scope.store_path(), [{
        "ext_id": "okengine.old",
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "status": "revoked",
    }])
    assert scope.resolve(token) is None, "a revoked token must lose access immediately"


def test_every_non_active_record_is_skipped_and_later_active_records_are_checked(scope):
    token = "ordered-token"
    digest = hashlib.sha256(token.encode()).hexdigest()
    write_store(scope.store_path(), [
        {"ext_id": "inactive-low-sort", "token_sha256": digest, "status": "aaa"},
        {"ext_id": "active-match", "token_sha256": digest, "status": "active"},
    ])
    record = scope.resolve(token)
    assert record is not None and record["ext_id"] == "active-match"


def test_unknown_and_empty_tokens_do_not_resolve(scope):
    write_store(scope.store_path(), [{"ext_id": "e", "token_sha256": "deadbeef"}])
    assert scope.resolve("not-the-token") is None
    assert scope.resolve("") is None
    assert scope.resolve(None) is None


def test_store_holds_only_hashes_never_plaintext(scope):
    """The store is mounted read-only into the READ container; plaintext there would
    leak write credentials across a trust boundary."""
    token = "plaintext-should-not-appear"
    write_store(scope.store_path(), [{
        "ext_id": "e", "token_sha256": hashlib.sha256(token.encode()).hexdigest()}])
    assert token not in scope.store_path().read_text()
    assert scope.token_sha256(token) == hashlib.sha256(token.encode()).hexdigest()


# ── path_in_scopes / is_full ─────────────────────────────────────────────────

def test_full_vault_scopes_cover_everything(scope):
    for full in (["wiki/**"], ["**"], ["*"], [""]):
        assert scope.path_in_scopes("entities/a/acme", full), full
        assert scope.is_full(full), full


def test_namespace_scope_covers_its_subtree_only(scope):
    scopes = ["dashboards/**"]
    assert scope.path_in_scopes("dashboards/ops", scopes)
    assert scope.path_in_scopes("dashboards/a/b/c", scopes)
    assert scope.path_in_scopes("dashboards", scopes), "the namespace root itself"
    assert not scope.path_in_scopes("entities/a/acme", scopes)
    assert not scope.is_full(scopes)


def test_prefix_matching_does_not_leak_across_a_name_boundary(scope):
    """'dashboards' must not grant 'dashboards-private'."""
    assert not scope.path_in_scopes("dashboards-private/x", ["dashboards/**"])


@pytest.mark.parametrize("path", [
    "dashboards/../entities/a/acme", "dashboards/./private", "dashboards\\..\\private",
    "dashboards/ok\0hidden",
])
def test_unsafe_path_syntax_never_matches_even_a_full_scope(scope, path):
    assert not scope.path_in_scopes(path, ["**"])


def test_scope_qualifiers_and_wiki_prefix_are_normalised(scope):
    for spelling in ("wiki/dashboards/**", "/wiki/dashboards/**", "somevault:wiki/dashboards/**"):
        assert scope.path_in_scopes("dashboards/ops", [spelling]), spelling
    assert not scope.path_in_scopes("dashboards/ops", ["vault:wiki:dashboards/**"])


def test_md_suffix_and_leading_slash_on_the_path_are_tolerated(scope):
    assert scope.path_in_scopes("/dashboards/ops.md", ["dashboards/**"])
    assert scope.path_in_scopes("leaf.md", ["leaf"])


def test_prefix_matching_is_equality_not_lexicographic_order(scope):
    assert not scope.path_in_scopes("aaa", ["zzz/**"])


def test_glob_scopes_are_supported(scope):
    assert scope.path_in_scopes("entities/a/acme", ["entities/*/acme"])
    assert not scope.path_in_scopes("entities/a/other", ["entities/*/acme"])


def test_empty_or_absent_scopes_grant_nothing(scope):
    for empty in ([], None):
        assert not scope.path_in_scopes("entities/a/acme", empty), empty
        assert not scope.is_full(empty), empty
