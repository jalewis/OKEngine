"""Mutation-strength contracts for the extracted write authorization service."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"


@pytest.fixture
def auth(tmp_path, monkeypatch):
    (tmp_path / "wiki/entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {entities: {}}}\n", encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    name = f"write_server_auth_mutation_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, tmp_path


def dynamic(value: str) -> str:
    """Equal to a literal without sharing its identity."""
    return bytes(value, "utf-8").decode("utf-8")


def test_caller_and_capability_kind_checks_use_value_equality(auth, monkeypatch):
    m, root = auth
    page = root / "wiki/entities/a/acme.md"
    called = []
    monkeypatch.setattr(m, "_effective_policy", lambda: {"capabilities": {}})
    monkeypatch.setattr(m.policy_plane, "evaluate_capability", lambda *_a: called.append(True))

    token = m._caller_var.set({"kind": dynamic("admin"), "actor": "admin"})
    try:
        assert m._capability_reject(page, "update") is None
    finally:
        m._caller_var.reset(token)
    assert called == [], "an equal non-interned admin kind must bypass policy evaluation"

    token = m._caller_var.set({
        "kind": dynamic("extension"), "actor": "extension:x", "ext_id": "x",
        "write_capability": {"operations": ["create"]},
    })
    try:
        assert m._capability_reject(page, "create") is None
    finally:
        m._caller_var.reset(token)
    assert called == [True]


def test_effective_policy_cache_invalidates_only_when_document_key_changes(auth, monkeypatch):
    m, root = auth
    document = root / "policy.yaml"
    document.write_text("version: 1\n", encoding="utf-8")
    composed = []
    monkeypatch.setattr(m.policy_plane, "discover_documents", lambda _root: [document])
    monkeypatch.setattr(
        m.policy_plane, "compose_documents",
        lambda paths: composed.append(tuple(paths)) or {"generation": len(composed)},
    )
    m._policy_cache.update(key=None, value=None)

    assert m._effective_policy() == {"generation": 1}
    assert m._effective_policy() == {"generation": 1}
    stat = document.stat()
    os.utime(document, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert m._effective_policy() == {"generation": 2}
    assert len(composed) == 2


def test_contract_mode_uses_value_equality_and_preserves_unknown_fields(auth, monkeypatch):
    m, root = auth
    page = root / "wiki/entities/a/acme.md"
    observed = {}
    logs = []
    monkeypatch.setattr(
        m._output_contract, "evaluate",
        lambda caller, **kwargs: observed.update(caller=caller, **kwargs) or [
            {"code": "unknown", "message": "field refused"}
        ],
    )
    monkeypatch.setattr(m, "_append_log", logs.append)
    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", dynamic("enforce"))

    rejection = m._contract_reject(
        page, "update", {"type": "actor"}, "body",
        ["ignored", "unknown field(s): first, second"], body_links_changed=False,
    )
    assert rejection == "output_contract.unknown: field refused"
    assert observed["unknown_fields"] == ["first", "second"]
    assert observed["caller"]["body_links_changed"] is False
    assert " reject " in logs[0]

    monkeypatch.setenv("OKENGINE_OUTPUT_CONTRACT_MODE", "report")
    assert m._contract_reject(page, "update", {}, "", []) is None
    assert " report " in logs[-1]


def test_job_actor_contract_defaults_to_enforce(auth, monkeypatch):
    m, root = auth
    page = root / "wiki/entities/a/acme.md"
    logs = []
    monkeypatch.delenv("OKENGINE_OUTPUT_CONTRACT_MODE", raising=False)
    monkeypatch.setattr(m, "_append_log", logs.append)
    monkeypatch.setattr(m._output_contract, "evaluate", lambda *args, **kwargs: [
        {"code": "contract_not_resolved", "message": "actor contract is missing"},
    ])
    token = m._caller_var.set({"kind": "job", "actor": "cron:unconfigured"})
    try:
        rejection = m._contract_reject(page, "update", {"type": "actor"}, "body", [])
    finally:
        m._caller_var.reset(token)
    assert rejection == "output_contract.contract_not_resolved: actor contract is missing"
    assert " output-contract reject " in logs[0]


def test_provenance_and_review_flags_distinguish_exact_lifecycle_values(auth):
    m, _root = auth
    token = m._caller_var.set({
        "kind": dynamic("extension"), "ext_id": "owner", "actor": "extension:owner"
    })
    try:
        created = {"extension_id": "forged"}
        m._apply_extension_provenance(created, creating=True)
        assert created == {"extension_id": "owner"}
    finally:
        m._caller_var.reset(token)

    updated = {"extension_id": "forged"}
    m._apply_extension_provenance(updated, creating=False, existing_ext_id="original")
    assert updated == {"extension_id": "original"}

    token = m._caller_var.set({"kind": "job", "actor": "cron:source-quality-backfill"})
    try:
        created = {"producer_lane": "forged"}
        m._apply_extension_provenance(created, creating=True)
        assert created == {"producer_lane": "source-quality-backfill"}

        updated = {"producer_lane": "forged"}
        m._apply_extension_provenance(
            updated, creating=False, existing_producer_lane="prior-lane"
        )
        assert updated == {"producer_lane": "prior-lane"}
    finally:
        m._caller_var.reset(token)

    updated = {"producer_lane": "forged"}
    m._apply_extension_provenance(
        updated, creating=False, existing_producer_lane="source-quality-backfill"
    )
    assert updated == {"producer_lane": "source-quality-backfill"}

    fm = {"reviewed_by": "forged", "needs_review": False}
    assert m._apply_review_governance(fm, {"needs_review": 1}) == []
    assert fm["needs_review"] is False, "integer 1 is not the server-owned boolean verdict True"
    assert "reviewed_by" not in fm

    fm = {}
    assert m._apply_review_governance(fm, {"needs_review": True}) == []
    assert fm["needs_review"] is True


def test_empty_or_noncron_job_actor_does_not_gain_producer_provenance(auth):
    m, _root = auth
    for actor in ("cron:", "interactive"):
        token = m._caller_var.set({"kind": "job", "actor": actor})
        try:
            fm = {"producer_lane": "forged"}
            m._apply_extension_provenance(fm, creating=True)
            assert fm == {}
        finally:
            m._caller_var.reset(token)


def test_entity_shard_boundary_conditions_are_exact(auth):
    m, root = auth
    wiki = root / "wiki"
    assert m._normalize_entity_shard("entities") == "entities"
    assert m._normalize_entity_shard("entities/.md") == "entities/.md"
    assert m._normalize_entity_shard("entities/a/b/acme.md") == "entities/a/acme.md"

    leaf = wiki / "entities/a"
    (leaf / "c").mkdir(parents=True)
    assert m._normalize_entity_shard("entities/ab.md") == "entities/a/b/ab.md"
    (leaf / "ab.md").write_text("canonical", encoding="utf-8")
    assert m._normalize_entity_shard("entities/ab.md") == "entities/a/ab.md"

    (leaf / "b/ab.md").parent.mkdir(parents=True, exist_ok=True)
    (leaf / "b/ab.md").write_text("resharded", encoding="utf-8")
    assert m._normalize_entity_shard("entities/z/ab.md") == "entities/a/b/ab.md"


def test_safe_slug_length_suffix_and_prefix_boundaries_are_exact(auth):
    m, root = auth
    limit = m._MAX_ENTITY_SLUG_LEN
    assert m._safe("entities/" + "a" * limit) is not None
    assert m._safe("entities/" + "a" * (limit + 1)) is None
    assert m._safe("entities/has space") is None

    dotted = m._safe("concepts/release.1")
    assert dotted is not None and dotted.name == "release.1.md"
    markdown = m._safe("concepts/release.md")
    assert markdown is not None and markdown.name == "release.md"
    other_suffix = m._safe("concepts/release.txt")
    assert other_suffix is not None and other_suffix.name == "release.txt.md"

    assert m._safe(dynamic("wiki")) is None

    absolute_wiki = str((root / "wiki").resolve())
    assert m._safe(absolute_wiki) is None
    assert m._safe(absolute_wiki + "-shadow/x") != m._safe("x")


def test_scope_refusal_authorizes_the_normalized_path(auth, monkeypatch):
    m, root = auth
    normalized = (root / "wiki/entities/a/acme.md").resolve()
    observed = []
    monkeypatch.setattr(m, "_safe", lambda _path: normalized)
    monkeypatch.setattr(m, "_authorize_write", lambda path: observed.append(path) or True)
    assert m._wauth_refusal("entities/../entities/a/acme") is None
    assert observed == ["entities/a/acme.md"]
