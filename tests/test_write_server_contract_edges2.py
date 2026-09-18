"""Remaining defensive contracts for the governed MCP write boundary."""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "okengine-mcp" / "write_server.py"


def _load(tmp_path, monkeypatch, actor=""):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {}\npartitioning: {namespaces: {entities: {}, concepts: {}}}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    if actor:
        monkeypatch.setenv("OKENGINE_WRITE_ACTOR", actor)
    else:
        monkeypatch.delenv("OKENGINE_WRITE_ACTOR", raising=False)
    name = f"write_server_contract2_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_clock_policy_and_capability_outcomes(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.delenv("OKENGINE_MCP_WRITE_DATE", raising=False)
    assert m._today() == datetime.date.today().isoformat()
    page = tmp_path / "wiki/entities/e/example.md"

    policy = {"capabilities": {}}
    monkeypatch.setattr(m, "_effective_policy", lambda: policy)
    monkeypatch.setattr(m, "_rel", lambda _p: "entities/e/example.md")
    evaluated = []
    monkeypatch.setattr(m.policy_plane, "evaluate_capability", lambda *a: evaluated.append(a[0]))
    token = m._caller_var.set({"kind": "extension", "actor": "ext:x", "ext_id": "x",
                               "write_capability": {"operations": ["create"]}})
    try:
        assert m._capability_reject(page, "create") is None
    finally:
        m._caller_var.reset(token)
    assert "ext:x" in evaluated[0]["capabilities"]

    token = m._caller_var.set({"kind": "job", "actor": "cron:lane"})
    monkeypatch.setattr(m._output_contract, "resolve", lambda _c: ({"_missing": True}, None))
    finding = {"code": "denied"}
    monkeypatch.setattr(m.policy_plane, "evaluate_capability", lambda *_a: finding)
    monkeypatch.setattr(m.policy_plane, "finding_message", lambda _r: "refused")
    monkeypatch.setattr(m.policy_plane, "append_event", lambda *_a: (_ for _ in ()).throw(OSError()))
    try:
        assert m._capability_reject(page, "update") == "refused"
    finally:
        m._caller_var.reset(token)


def test_review_projection_entity_shard_and_safe_oserror(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    fm = {"reviewed_by": "old", "needs_review": False}
    flags = m._apply_review_governance(fm, {"reviewed_on": "2026-01-01"})
    assert fm["needs_review"] is True and flags

    (tmp_path / "wiki/entities/a").mkdir(parents=True)
    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda _p: (_ for _ in ()).throw(OSError()))
    assert m._normalize_entity_shard("entities/alpha") == "entities/a/alpha.md"
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    original_resolve = Path.resolve
    wiki = tmp_path / "wiki"
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == wiki else original_resolve(p, *a, **k))
    assert m._safe("concepts/x") is None


def test_partition_and_schema_shape_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    monkeypatch.setattr(m, "_CONVERGE_OK", True)
    monkeypatch.setattr(m, "_qualified_namespace", lambda _p: "concepts")
    monkeypatch.setattr(m.okf_migrate, "is_partitioned", lambda *_a: True)
    monkeypatch.setattr(m.okf_migrate, "write_key", lambda *_a: (_ for _ in ()).throw(ValueError()))
    assert m._partitioned_create_path(page, {}) == page

    m._base_list_fields_cache = None
    monkeypatch.setattr(m.schema_lib, "base_schema", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert m._base_list_fields() == m._FALLBACK_LIST_FIELDS
    monkeypatch.setattr(m, "_base_list_fields", lambda: frozenset({"base"}))
    monkeypatch.setattr(m.schema_lib, "list_fields", lambda _s: (_ for _ in ()).throw(RuntimeError()))
    assert m._list_fields_for(page) == {"base"}


def test_item_contract_passthrough_and_native_date(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    rules = {"items": {"_item": {}, "choice": {"enum": {"A"}},
                        "when": {"shape": "date"}}}
    monkeypatch.setattr(m, "_item_rules_for", lambda _p: rules)
    assert m._item_shape_reject(None, {"items": "not-list"}) is None
    assert m._item_shape_reject(None, {"items": ["legacy"]}) is None
    assert m._item_shape_reject(None, {"items": [{"choice": "A"}]}) is None
    assert m._item_shape_reject(None, {"items": [{"when": datetime.date(2026, 1, 1)}]}) is None


def test_review_request_existing_scalar_and_scalar_evidence(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/example.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: entity\nversion: 1\nsource: one\nneeds_review: true\n---\nbody")
    first = m._ensure_review_request(page)
    assert first["evidence"] == ["one"]
    record = m._review_record_path(first["review_id"])
    record.write_text("- scalar\n")
    assert m._ensure_review_request(page) == {}


def test_review_capability_rejections(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/example.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: entity\nversion: 1\nneeds_review: true\n---\nbody")
    monkeypatch.setattr(m, "_safe", lambda _p: page)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    digest = "0" * 64
    assert m._assign_review("x", "me", 1, digest)["status"] == 403
    assert m._resolve_review("x", "approve", "me", "", 1, digest)["status"] == 403
    assert m._record_machine_review("x", "bot", "supported")["status"] == 403


def test_namespace_schema_drift_and_reserved_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/x.md"
    monkeypatch.setattr(m, "_governing", lambda _p: (_ for _ in ()).throw(RuntimeError()))
    assert m._reserved_refuse(page) is None
    assert m._namespace(tmp_path / "outside.md") == ""
    assert m._namespace(tmp_path / "wiki") == ""
    assert m._qualified_namespace(tmp_path / "wiki") == ""

    m._okf_always_cache = None
    monkeypatch.setattr(m.schema_lib, "base_schema", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert m._okf_always() == m._OKF_ALWAYS
    monkeypatch.setattr(m, "drift_policy", lambda _p: {})
    out, flags = m._normalize_drift({"type": "x"}, page)
    assert out == {"type": "x"} and flags == []


def test_alias_candidate_races_and_tombstones(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    incoming = tmp_path / "wiki/entities/i/incoming.md"
    incoming.parent.mkdir(parents=True)
    same = incoming
    unreadable = tmp_path / "wiki/entities/u/unreadable.md"
    unreadable.parent.mkdir(parents=True)
    unreadable.write_text("body")
    tomb = tmp_path / "wiki/entities/t/tomb.md"
    tomb.parent.mkdir(parents=True)
    tomb.write_text("---\nstatus: tombstoned\n---\n")
    m._registry_cache = None
    monkeypatch.setattr(m, "_registry", lambda: SimpleNamespace(name_to_rels={}, alias_to_rels={}))
    original_rglob = Path.rglob
    monkeypatch.setattr(Path, "rglob", lambda _p, _pat: iter([same, unreadable, tomb]))
    original_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **k:
                        (_ for _ in ()).throw(OSError()) if p == unreadable else original_resolve(p, *a, **k))
    assert m._alias_hits(incoming, "incoming", set()) == []
    monkeypatch.setattr(Path, "rglob", original_rglob)


def test_dedup_disabled_alias_shapes_and_identity_edges(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/example.md"
    monkeypatch.setattr(m, "_CONVERGE_OK", False)
    assert m._dedup_on_create("entities/example", page, {}, "") is None
    monkeypatch.setattr(m, "_CONVERGE_OK", True)
    monkeypatch.setattr(m, "_namespace", lambda _p: "entities")
    monkeypatch.setattr(m, "_alias_hits", lambda *_a: [])
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("", "minted"))
    assert m._dedup_on_create("entities/example", page, {"aliases": 42}, "") is None
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("id:x", "minted"))
    monkeypatch.setattr(m, "_registry", lambda: SimpleNamespace(resolve=lambda _id: "entities/old.md"))
    assert m._dedup_on_create("entities/example", page, {"aliases": "a, b"}, "") is None


def test_maintainer_future_date_and_link_edges(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setenv("OKENGINE_PACK", "pack")
    fm = {"maintained_by": "old"}
    m._stamp_maintainer(fm, creation=True)
    assert fm == {"maintained_by": ["old", "pack"], "discovered_by": "pack"}
    m._stamp_maintainer(fm, creation=False)
    assert fm["maintained_by"].count("pack") == 1

    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-01-01")
    assert m._future_date_reject({"published": "2026-99-99"}) is None

    first = tmp_path / "wiki/entities/a/alpha.md"
    first.parent.mkdir(parents=True)
    first.write_text("a")
    second = tmp_path / "wiki/entities/b/e/beta.md"
    second.parent.mkdir(parents=True)
    second.write_text("b")
    assert m._wikilink_resolves("entities/alpha")
    assert m._wikilink_resolves("entities/beta")
    monkeypatch.setattr(m, "_namespace", lambda _p: "entities")
    flags = m._unresolvable_link_flags(first, "[[entities/missing.md]] [[]] [[entities/missing.md]]")
    assert len(flags) == 1
    monkeypatch.setattr(m, "_namespace", lambda _p: "briefings")
    assert "did you mean" in m._briefing_link_reject(first, "[[entities/alpha.md]]")


def test_create_redirect_capability_and_shape_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/c/x.md"
    monkeypatch.setattr(m, "_safe", lambda _p: page)
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "scope" if isinstance(p, Path) else None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    monkeypatch.setattr(m, "_partitioned_create_path", lambda *_a: page.with_name("redirect.md"))
    assert m._create("concepts/x", {}) == "scope"

    monkeypatch.setattr(m, "_partitioned_create_path", lambda p, _fm: p)
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._create("concepts/x", {}) == "rejected: denied"

    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_governing", lambda _p: (_ for _ in ()).throw(RuntimeError()))
    assert "not declared" in m._create("concepts/x", {"type": "bad_type"})

    monkeypatch.setattr(m, "_namespace_reject", lambda _p: None)
    monkeypatch.setattr(m, "_type_namespace_reject", lambda *_a: None)
    monkeypatch.setattr(m, "_fabricated_source_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_missing_source_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_normalize_drift", lambda fm, _p: (fm, []))
    monkeypatch.setattr(m, "_item_shape_reject", lambda *_a: "bad items")
    assert m._create("concepts/x", {"type": "concept"}) == "rejected: bad items"


def test_update_early_capability_body_version_and_item_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: concept\nversion: bad\n---\nbody")
    monkeypatch.setattr(m, "_safe", lambda p: None if p == "unsafe" else page)
    assert m._update("unsafe") == "refused: path outside the vault wiki/"
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "scope" if p == "scoped" else None)
    assert m._update("scoped") == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    assert "valid YAML mapping" in m._update("x", "- scalar")
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._update("x", {}) == "rejected: denied"
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_body_integrity_reject", lambda *_a: "bad body")
    assert m._update("x", {}, body="new") == "rejected: bad body"
    monkeypatch.setattr(m, "_body_integrity_reject", lambda *_a: None)
    monkeypatch.setattr(m, "_item_shape_reject", lambda *_a: "bad items")
    assert m._update("x", {}) == "rejected: bad items"


def test_tombstone_flag_and_stamp_failure_paths(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: entity\nversion: bad\n---\nbody")
    monkeypatch.setattr(m, "_safe", lambda p: None if p == "unsafe" else page)
    assert m._tombstone("unsafe", "reason").startswith("refused")
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "scope" if p == "scoped" else None)
    assert m._tombstone("scoped", "reason") == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._tombstone("x", "reason") == "rejected: denied"
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: "bad schema")
    assert m._tombstone("x", "reason") == "rejected: bad schema"

    assert m._flag("unsafe", "note").startswith("refused")
    monkeypatch.setattr(m, "_safe", lambda _p: page)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._flag("x", "note") == "rejected: denied"
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: "scope")
    assert m._flag("x", "note") == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    page.write_text("---\na: [bad\n---\n")
    assert "frontmatter" in m._flag("x", "note")

    fm = {"version": "bad"}
    m._stamp(fm, {})
    assert fm["version"] == 2


def test_patch_safety_auth_parse_and_gate_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: concept\nversion: 1\n---\nbody\n")
    monkeypatch.setattr(m, "_safe", lambda p: None if p == "unsafe" else page)
    assert m._patch("unsafe", "a", "b").startswith("refused")
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "scope" if p == "scoped" else None)
    assert m._patch("scoped", "a", "b") == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    assert "identical" in m._patch("x", "body", "body")
    assert "corrupt" in m._patch("x", "---\ntype: concept\nversion: 1\n---\n", "")

    page.write_text("---\ntype: concept\nversion: 1\n---\nbody\n")
    assert "invalid frontmatter" in m._patch("x", "type: concept", "type: [bad")
    page.write_text("---\ntype: concept\nversion: 1\n---\nbody\n")
    assert "non-mapping" in m._patch("x", "type: concept\nversion: 1", "- scalar")

    page.write_text("---\ntype: concept\nversion: 1\n---\nbody\n")
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._patch("x", "body", "new") == "rejected: denied"
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_int_shape_reject", lambda *_a: "bad int")
    assert m._patch("x", "body", "new") == "rejected: bad int"
    monkeypatch.setattr(m, "_int_shape_reject", lambda *_a: None)
    monkeypatch.setattr(m, "_item_shape_reject", lambda *_a: "bad item")
    assert m._patch("x", "body", "new") == "rejected: bad item"


def _review_subject(m, tmp_path):
    page = tmp_path / "wiki/entities/e/example.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: entity\nversion: 1\nneeds_review: true\nreviewed_by: old\n---\nbody")
    _fm, _body, _subject, version, digest = m._review_page_state(page)
    return page, version, digest


def test_review_assignment_conflict_stale_and_closed(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page, version, digest = _review_subject(m, tmp_path)
    assert m._assign_review("entities/e/example", "me", version + 1, digest)["status"] == 409
    rec = m._ensure_review_request(page)
    assert m._assign_review("entities/e/example", "me", version, digest,
                            review_id="stale")["status"] == 409
    monkeypatch.setattr(m, "_ensure_review_request", lambda _p: {**rec, "state": "approved"})
    assert m._assign_review("entities/e/example", "me", version, digest)["status"] == 409


def test_review_resolution_conflicts_schema_and_atomic_rollback(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page, version, digest = _review_subject(m, tmp_path)
    assert m._resolve_review("entities/e/example", "approve", "me", "", version + 1, digest)["status"] == 409
    rec = m._ensure_review_request(page)
    assert m._resolve_review("entities/e/example", "approve", "me", "", version, digest,
                             review_id="stale")["status"] == 409
    monkeypatch.setattr(m, "_ensure_review_request", lambda _p: {**rec, "state": "dismissed"})
    assert m._resolve_review("entities/e/example", "approve", "me", "", version, digest)["status"] == 409

    monkeypatch.setattr(m, "_ensure_review_request", lambda _p: dict(rec))
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: "bad")
    assert m._resolve_review("entities/e/example", "reject", "me", "why", version, digest)["status"] == 422

    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    monkeypatch.setattr(m.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk")))
    out = m._resolve_review("entities/e/example", "reject", "me", "why", version, digest)
    assert out["status"] == 500 and "atomic" in out["error"]


def test_machine_review_success(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page, _version, _digest = _review_subject(m, tmp_path)
    out = m._record_machine_review("entities/e/example", "bot", "supported", "ok")
    assert out["ok"] is True and out["machine_check"]["evaluator"] == "bot"


def test_section_append_early_and_gate_paths(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: concept\nversion: 1\n---\n# X\n\n## One\nold\n\n## Two\ntail\n")
    inserted, where = m._insert_into_section("## One\nold\n## Two\ntail", "One", "new")
    assert inserted.index("new") < inserted.index("## Two") and where.startswith("appended")

    monkeypatch.setattr(m, "_safe", lambda p: None if p == "unsafe" else page)
    assert m._append_section("unsafe", "One", "x").startswith("refused")
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "scope" if p == "scoped" else None)
    assert m._append_section("scoped", "One", "x") == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    assert "empty" in m._append_section("x", "One", " ")
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._append_section("x", "One", "new") == "rejected: denied"
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_contract_reject", lambda *_a, **_k: "contract")
    assert m._append_section("x", "One", "new") == "rejected: contract"
    monkeypatch.setattr(m, "_contract_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: "bad schema")
    assert m._append_section("x", "One", "new") == "rejected: bad schema"


def test_network_write_auth_fail_closed_and_overrides(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="requires"):
        m._resolve_write_auth({}, "127.0.0.1")
    with pytest.raises(SystemExit, match="DEFAULT"):
        m._resolve_write_auth({"OKENGINE_WRITE_TOKEN": m.DEFAULT_LOCAL_TOKEN}, "0.0.0.0")
    assert m._resolve_write_auth({"OKENGINE_MCP_TOKEN": "secret"}, "0.0.0.0") == "secret"
    assert m._resolve_write_auth({"OKENGINE_WRITE_TOKEN": m.DEFAULT_LOCAL_TOKEN,
                                  "OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN": "1"}, "0.0.0.0") == m.DEFAULT_LOCAL_TOKEN
    # Pin every side of the fail-closed boolean expression. A default token is
    # acceptable only on an exact loopback host, and WRITE_TOKEN wins over the
    # compatibility MCP token when both are present.
    for host in m._LOOPBACK:
        assert m._resolve_write_auth(
            {"OKENGINE_WRITE_TOKEN": m.DEFAULT_LOCAL_TOKEN}, host
        ) == m.DEFAULT_LOCAL_TOKEN
    assert m._resolve_write_auth(
        {"OKENGINE_WRITE_TOKEN": "write-secret", "OKENGINE_MCP_TOKEN": "mcp-secret"},
        "0.0.0.0",
    ) == "write-secret"
    assert m._resolve_write_auth(
        {"OKENGINE_WRITE_TOKEN": "custom", "OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN": "0"},
        "198.51.100.8",
    ) == "custom"
    with pytest.raises(SystemExit, match="DEFAULT"):
        m._resolve_write_auth(
            {"OKENGINE_WRITE_TOKEN": m.DEFAULT_LOCAL_TOKEN,
             "OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN": "true"},
            "198.51.100.8",
        )
    # Equality, not object identity, defines both configuration values.
    dynamic_default = "".join(["okengine", "-local"])
    dynamic_override = "".join(["", "1"])
    with pytest.raises(SystemExit, match="DEFAULT"):
        m._resolve_write_auth({"OKENGINE_WRITE_TOKEN": dynamic_default}, "0.0.0.0")
    assert m._resolve_write_auth(
        {"OKENGINE_WRITE_TOKEN": dynamic_default,
         "OKENGINE_WRITE_ALLOW_DEFAULT_TOKEN": dynamic_override},
        "0.0.0.0",
    ) == dynamic_default


def test_extracted_write_state_constants_are_behavioral_contracts(tmp_path, monkeypatch):
    """Pin constants whose mutations alter review, identity, and degeneration behavior."""
    m = _load(tmp_path, monkeypatch)
    assert m._MAX_ENTITY_SLUG_LEN == 80
    assert m._DEGEN_MAX_RUN == 250
    assert m._REVIEW_DECISIONS == {
        "approve": ("approved", False),
        "request-changes": ("changes-requested", True),
        "reject": ("rejected", True),
        "dismiss": ("dismissed", False),
        "defer": ("open", True),
    }
    # Both flags matter: actor-type prose can span lines and arrive in mixed case.
    assert m._NON_ACTOR_DEFINITION.search("SLEEPWALKER IS\na backdoor")
    assert not m._NON_ACTOR_DEFINITION.search("SLEEPWALKER is an intrusion set")


def test_converge_early_redirect_and_capability_failures(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    redirected = tmp_path / "wiki/concepts/r/x.md"
    monkeypatch.setattr(m, "_CONVERGE_OK", False)
    assert "unavailable" in m._converge("x", {})
    monkeypatch.setattr(m, "_CONVERGE_OK", True)
    monkeypatch.setattr(m, "_safe", lambda p: None if p == "unsafe" else page)
    assert m._converge("unsafe", {}).startswith("refused")
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "scope" if p == "scoped" else None)
    assert m._converge("scoped", {}) == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda p: "reserved" if p == page else None)
    assert m._converge("x", {}) == "reserved"
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    assert "valid YAML" in m._converge("x", "- scalar")

    monkeypatch.setattr(m, "_partitioned_create_path", lambda *_a: redirected)
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "redirect scope" if isinstance(p, Path) else None)
    assert m._converge("x", {}) == "redirect scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda p: "redirect reserved" if p == redirected else None)
    assert m._converge("x", {}) == "redirect reserved"
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: "denied")
    assert m._converge("x", {}) == "rejected: denied"
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("", "slug"))
    assert "cannot determine" in m._converge("x", {})


def _configure_existing_converge(m, tmp_path, monkeypatch):
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: concept\nid: concepts:x\nversion: 1\n---\nold")
    monkeypatch.setattr(m, "_safe", lambda _p: page)
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    monkeypatch.setattr(m, "_partitioned_create_path", lambda p, _fm: p)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_namespace", lambda _p: "concepts")
    monkeypatch.setattr(m, "_governing", lambda _p: {})
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("concepts:x", "authority"))
    decision = SimpleNamespace(added=[], updated=[], removed=[], conflicts=[])
    monkeypatch.setattr(m.converge, "merge_frontmatter", lambda cur, incoming, **_k: ({**cur, **incoming}, decision))
    monkeypatch.setattr(m.schema_lib, "type_owner", lambda *_a: None)
    monkeypatch.setattr(m.schema_lib, "field_owners", lambda *_a: {})
    registry = SimpleNamespace(resolve=lambda _id: "concepts/x.md", is_tombstoned=lambda _id: False,
                               by_id={}, tombstoned=set())
    monkeypatch.setattr(m, "_registry", lambda: registry)
    return page


def test_converge_existing_redirect_precondition_and_deep_gates(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = _configure_existing_converge(m, tmp_path, monkeypatch)
    other = tmp_path / "wiki/concepts/other.md"
    other.write_text(page.read_text())
    registry = m._registry()
    registry.resolve = lambda _id: "concepts/other.md"
    monkeypatch.setattr(m, "_wauth_refusal", lambda p: "redirect denied" if p == other else None)
    assert m._converge("x", {}) == "redirect denied"

    _configure_existing_converge(m, tmp_path, monkeypatch)
    registry = m._registry()
    registry.resolve = lambda _id: "concepts/other.md"
    monkeypatch.setattr(
        m, "_capability_reject",
        lambda p, *_a, **_k: "redirect capability denied" if p == other else None,
    )
    assert m._converge("x", {}) == "rejected: redirect capability denied"

    _configure_existing_converge(m, tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_write_precondition", lambda *_a: "changed")
    assert m._converge("x", {}) == "changed"

    for attr, value, expected, kwargs in [
        ("_body_integrity_reject", "body bad", "body bad", {"body": "new"}),
        ("_item_shape_reject", "items bad", "items bad", {}),
        ("_type_ns_reject_on_change", "type bad", "type bad", {}),
        ("_missing_source_reject", "missing source", "missing source", {}),
        ("_contract_reject", "contract bad", "contract bad", {}),
    ]:
        m = _load(tmp_path / attr, monkeypatch)
        _configure_existing_converge(m, tmp_path / attr, monkeypatch)
        monkeypatch.setattr(m, attr, lambda *_a, _v=value, **_k: _v)
        assert expected in m._converge("x", {}, **kwargs)

    m = _load(tmp_path / "schema", monkeypatch)
    _configure_existing_converge(m, tmp_path / "schema", monkeypatch)
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: "schema bad")
    assert "schema bad" in m._converge("x", {})

    m = _load(tmp_path / "race", monkeypatch)
    _configure_existing_converge(m, tmp_path / "race", monkeypatch)
    checks = iter(["", "changed late"])
    monkeypatch.setattr(m, "_write_precondition", lambda *_a: next(checks))
    assert m._converge("x", {}) == "changed late"


def test_final_statement_error_paths(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\nversion: 1\n---\nbody")

    # Alias candidates that resolve but disappear on read are ignored.
    monkeypatch.setattr(m, "_registry", lambda: SimpleNamespace(name_to_rels={}, alias_to_rels={}))
    monkeypatch.setattr(Path, "rglob", lambda *_a: iter([page]))
    monkeypatch.setattr(m, "_read_page", lambda _p: (_ for _ in ()).throw(OSError()))
    assert m._alias_hits(tmp_path / "wiki/concepts/incoming.md", "x", set()) == []

    # First-path authorization and best-effort identity stamping errors.
    monkeypatch.setattr(m, "_safe", lambda _p: page.with_name("new.md"))
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: "scope")
    assert m._create("x", {}) == "scope"
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    monkeypatch.setattr(m, "_partitioned_create_path", lambda p, _fm: p)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_namespace_reject", lambda _p: None)
    monkeypatch.setattr(m, "_type_namespace_reject", lambda *_a: None)
    monkeypatch.setattr(m, "_fabricated_source_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_missing_source_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    monkeypatch.setattr(m, "_dedup_on_create", lambda *_a: None)
    assert m._create("x", {"type": "concept"}).startswith("created")

    # The second schema validation after dedup can reject a dedup-mutated candidate.
    new_page = page.with_name("second.md")
    monkeypatch.setattr(m, "_safe", lambda _p: new_page)
    monkeypatch.setattr(m, "_CONVERGE_OK", False)
    decisions = iter([None, "late schema"])
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: next(decisions))
    assert m._create("x", {"type": "concept"}) == "rejected: late schema"

    # Late optimistic-concurrency failures leave updates untouched.
    monkeypatch.setattr(m, "_safe", lambda _p: page)
    monkeypatch.setattr(m, "_read_page", lambda _p: ({"type": "concept", "version": 1}, "body"))
    checks = iter(["", "changed late"])
    monkeypatch.setattr(m, "_write_precondition", lambda *_a: next(checks))
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    assert m._update("x", {}) == "changed late"

    # Tombstone registry maintenance is explicitly best-effort.
    monkeypatch.setattr(m, "_write_precondition", lambda *_a: "")
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: (_ for _ in ()).throw(RuntimeError()))
    assert m._tombstone("x", "done").startswith("tombstoned")


def test_patch_remaining_gate_rejections(tmp_path, monkeypatch):
    gates = [
        ("_type_ns_reject_on_change", "type gate"),
        ("_body_integrity_reject", "body gate"),
        ("_contract_reject", "contract gate"),
        ("schema_reject_reason", "schema gate"),
    ]
    for attr, reason in gates:
        root = tmp_path / attr
        m = _load(root, monkeypatch)
        page = root / "wiki/concepts/x.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntype: concept\nversion: 1\n---\nbody")
        monkeypatch.setattr(m, attr, lambda *_a, _r=reason, **_k: _r)
        assert reason in m._patch("concepts/x", "body", "new")


def test_raw_backfill_manifest_error_and_selected_url(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:raw-backfill")
    tool = m.mcp._tool_manager._tools["converge_source"].fn
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(tmp_path / "missing.json"))
    monkeypatch.setattr(m, "_raw_backfill_frontmatter", lambda value: (dict(value), None))
    monkeypatch.setattr(m, "_selected_raw_url", lambda _selected: "https://example.test/raw")
    captured = {}
    monkeypatch.setattr(m, "_converge", lambda path, fm, *_a: captured.update(path=path, fm=fm) or "wrote")
    assert "requires exactly one existing raw/ capture" in tool("sources/report", {})
    raw = tmp_path / "raw" / "item.md"
    raw.parent.mkdir()
    raw.write_text("raw evidence", encoding="utf-8")
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps({"selected": ["raw/item.md"]}), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest))
    assert "SUCCESS" in tool("sources/report", {})
    assert "https://example.test/raw" in captured["fm"]


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"selected": "raw/item.md"},
        {"selected": ["raw/one.md", "raw/two.md"]},
        {"selected": ["sources/item.md"]},
        {"selected": [{"path": "raw/item.md"}]},
    ],
)
def test_raw_backfill_rejects_non_single_raw_selection(
    tmp_path, monkeypatch, manifest
):
    m = _load(tmp_path, monkeypatch, "cron:raw-backfill")
    tool = m.mcp._tool_manager._tools["converge_source"].fn
    manifest_path = tmp_path / "selection.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest_path))
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(exist_ok=True)
    for name in ("item.md", "one.md", "two.md"):
        (raw_dir / name).write_text("evidence", encoding="utf-8")
    monkeypatch.setattr(m, "_converge", lambda *_args: "wrote")

    assert "requires exactly one existing raw/ capture" in tool(
        "sources/report", {"url": "https://example.test/report"}
    )


def test_raw_backfill_rejects_missing_and_escaping_raw_capture(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, "cron:raw-backfill")
    tool = m.mcp._tool_manager._tools["converge_source"].fn
    manifest_path = tmp_path / "selection.json"
    monkeypatch.setenv("OKENGINE_SELECTION_MANIFEST", str(manifest_path))

    manifest_path.write_text(json.dumps({"selected": ["raw/missing.md"]}))
    assert "requires exactly one existing raw/ capture" in tool(
        "sources/report", {"url": "https://example.test/report"}
    )

    outside = tmp_path.parent / "outside-raw.md"
    outside.write_text("evidence", encoding="utf-8")
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "escape.md").symlink_to(outside)
    manifest_path.write_text(json.dumps({"selected": ["raw/escape.md"]}))
    assert "requires exactly one existing raw/ capture" in tool(
        "sources/report", {"url": "https://example.test/report"}
    )


def test_review_rollback_without_prior_record(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page, version, digest = _review_subject(m, tmp_path)
    rec = m._ensure_review_request(page)
    missing = tmp_path / ".okengine/reviews/missing.yaml"
    monkeypatch.setattr(m, "_ensure_review_request", lambda _p: dict(rec))
    monkeypatch.setattr(m, "_review_record_path", lambda _id: missing)
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    monkeypatch.setattr(m.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk")))
    assert m._resolve_review("entities/e/example", "reject", "me", "why", version, digest)["status"] == 500
    assert not missing.exists()


def test_review_output_rollback_restores_both_record_states(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/example.md"
    record = tmp_path / ".okengine/reviews/review.yaml"
    page.parent.mkdir(parents=True)
    record.parent.mkdir(parents=True)
    page.write_text("new page", encoding="utf-8")
    record.write_text("new record", encoding="utf-8")
    touched = []
    monkeypatch.setattr(m, "_corpus_touch", touched.append)

    m._restore_review_outputs(page, "old page", record, "old record", True, True)
    assert page.read_text() == "old page"
    assert record.read_text() == "old record"
    assert touched == [page]

    record.write_text("new record", encoding="utf-8")
    m._restore_review_outputs(page, "ignored", record, None, False, True)
    assert not record.exists()


def test_remaining_helper_branch_outcomes(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\n---body")  # body does not begin with newline
    assert m._read_page(page)[1] == "body"

    # Existing actor capability takes the policy evaluation path.
    policy = {"capabilities": {"cron:x": {"operations": ["update"]}}}
    token = m._caller_var.set({"kind": "job", "actor": "cron:x"})
    monkeypatch.setattr(m, "_effective_policy", lambda: policy)
    monkeypatch.setattr(m.policy_plane, "evaluate_capability", lambda *_a: None)
    try:
        assert m._capability_reject(page, "update") is None
    finally:
        m._caller_var.reset(token)

    # Multiple unknown-field drift records exercise loop continuation.
    monkeypatch.setattr(m._output_contract, "evaluate", lambda *_a, **_k: [])
    assert m._contract_reject(page, "update", {}, "", [
        "benign", "unknown field(s): a", "unknown field(s): b"]) is None
    assert m._int_fields_for(None) == set(m._base_int_fields())
    assert m._item_rules_for(None) == m._base_item_rules()

    rules = {"items": {"_item": {}, "n": {"shape": "number"}, "d": {"shape": "dict"},
                        "unknown": {"shape": "other"}, "tail": {"shape": "str"}}}
    monkeypatch.setattr(m, "_item_rules_for", lambda _p: rules)
    assert m._item_shape_reject(None, {"items": [{"n": 2}, {"n": True}]}) is not None
    assert m._item_shape_reject(None, {"items": [{"d": {}, "unknown": 1, "tail": "x"}]}) is None

    monkeypatch.setattr(m, "drift_policy", lambda _p: {"field_aliases": {"old": "new"}})
    out, _ = m._normalize_drift({"type": "concept", "old": "lose", "new": "keep"}, page)
    assert out["new"] == "keep" and "old" not in out

    # A nonmatching readable alias candidate continues the scan.
    candidate = tmp_path / "wiki/entities/a/alpha.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("---\nname: Alpha\n---\n")
    monkeypatch.setattr(m, "_registry", lambda: SimpleNamespace(name_to_rels={}, alias_to_rels={}))
    assert m._alias_hits(tmp_path / "wiki/entities/i/incoming.md", "other", set()) == []
    assert not m._wikilink_resolves("bare")
    monkeypatch.setattr(m, "_namespace", lambda _p: "briefings")
    assert m._briefing_link_reject(page, "[[   ]]") is None


def test_create_existing_fields_and_identity_branch_outcomes(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/new.md"
    monkeypatch.setattr(m, "_safe", lambda _p: page)
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    monkeypatch.setattr(m, "_partitioned_create_path", lambda p, _fm: p)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_governing", lambda _p: {"types": {"good_type": {}}})
    monkeypatch.setattr(m.schema_lib, "canonical_types", lambda _s: {"good_type"})
    monkeypatch.setattr(m.schema_lib, "type_aliases", lambda _s: {})
    monkeypatch.setattr(m, "_namespace_reject", lambda _p: None)
    monkeypatch.setattr(m, "_type_namespace_reject", lambda *_a: None)
    monkeypatch.setattr(m, "_fabricated_source_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_missing_source_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("", "slug"))
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    monkeypatch.setattr(m, "_dedup_on_create", lambda *_a: None)
    out = m._create("x", {"type": "good_type", "version": 7,
                           "created": "2026-01-01", "last_updated": "2026-01-01"})
    assert out.startswith("created")


def test_tombstone_successor_and_empty_registry_id(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/entities/e/x.md"
    successor = tmp_path / "wiki/entities/s/successor.md"
    for p in (page, successor):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\ntype: entity\nversion: 1\n---\nbody")
    original_safe = m._safe
    monkeypatch.setattr(m, "_safe", lambda p: successor if p == "successor" else page)
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("", "slug"))
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    assert m._tombstone("x", "done", "successor").startswith("tombstoned")


def test_entity_and_concept_backfill_remaining_branches(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m.schema_lib, "canonical_types", lambda _s: set())
    cleaned, error = m._entity_backfill_frontmatter("", "type: unsupported")
    assert error is None and "type: unsupported" in cleaned
    cleaned, error = m._entity_backfill_frontmatter("", "type: publisher\nname: ''\ntitle: ''")
    assert error is None and __import__("yaml").safe_load(cleaned)["name"] == ""

    c = _load(tmp_path / "concept", monkeypatch, "cron:concept-backfill")
    tool = c.mcp._tool_manager._tools["converge_concept"].fn
    monkeypatch.setattr(c, "_converge", lambda *_a: "wrote")
    assert "SUCCESS" in tool("concepts/x", "id: concepts:x")


def test_patch_body_without_delimiter_newline(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    page = tmp_path / "wiki/concepts/x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: concept\nversion: 1\n---body")
    monkeypatch.setattr(m, "schema_reject_reason", lambda *_a: None)
    assert m._patch("concepts/x", "body", "new").startswith("patched")


def test_converge_missing_existing_path_falls_through_to_create(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    requested = tmp_path / "wiki/concepts/requested.md"
    missing = tmp_path / "wiki/concepts/missing.md"
    monkeypatch.setattr(m, "_safe", lambda _p: requested)
    monkeypatch.setattr(m, "_wauth_refusal", lambda _p: None)
    monkeypatch.setattr(m, "_reserved_refuse", lambda _p: None)
    monkeypatch.setattr(m, "_partitioned_create_path", lambda p, _fm: p)
    monkeypatch.setattr(m, "_capability_reject", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_namespace", lambda _p: "concepts")
    monkeypatch.setattr(m, "_governing", lambda _p: {})
    monkeypatch.setattr(m, "_page_id_and_kind", lambda *_a: ("concepts:x", "authority"))
    reg = SimpleNamespace(resolve=lambda _id: "concepts/missing.md", is_tombstoned=lambda _id: False,
                          by_id={})
    monkeypatch.setattr(m, "_registry", lambda: reg)
    monkeypatch.setattr(m, "_create", lambda *_a, **_k: "created concepts/requested.md v1")
    assert m._converge("x", {}).startswith("created")
    assert reg.by_id["concepts:x"] == "concepts/requested.md"

    # Creation can succeed even if a subsequent canonical-path resolution races away.
    calls = iter([requested, None])
    monkeypatch.setattr(m, "_safe", lambda _p: next(calls))
    reg.resolve = lambda _id: None
    reg.by_id.clear()
    assert m._converge("x", {}).startswith("created")
    assert reg.by_id == {}
