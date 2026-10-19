"""okengine#983: per-operation body authority, and the pack-replaceable chat capability.

The engine ships `chat:api_server` (the Agent Chat writer) as a floor. A pack may REPLACE exactly
an engine-declared `chat:*` capability, once, and only inside the engine ceiling: create/update/
append only, no body replacement on update, every floor-protected field still protected, and no
reach into sources/, assessments/, dashboards/, operational/, system or reserved paths. Every other
duplicate capability remains an ambiguity error (okengine#985 owns the general grant problem).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tools import policy_plane as pp  # noqa: E402

CATALOG = pp.engine_catalog_path()


def _floor() -> dict:
    return dict(pp.load_document(CATALOG)["capabilities"]["chat:api_server"])


def _pack_doc(tmp_path: Path, capabilities: dict, rules=None, name="policy.yaml") -> Path:
    doc = {"schema_version": 1, "rules": rules or [], "capabilities": capabilities}
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _replacement(**overrides) -> dict:
    cap = _floor()
    cap.update(overrides)
    return cap


# --- per-operation body modes --------------------------------------------------------------


def test_a_string_body_applies_to_every_operation_as_before():
    cap = {"body": "append-only"}
    assert {pp.body_mode(cap, op) for op in pp.OPERATIONS} == {"append-only"}
    assert pp.body_mode({}, "create") == "deny"


def test_a_body_mapping_is_per_operation_and_denies_what_it_omits():
    cap = {"body": {"create": "allow", "append": "append-only"}}
    assert pp.body_mode(cap, "create") == "allow"
    assert pp.body_mode(cap, "append") == "append-only"
    assert pp.body_mode(cap, "update") == "deny"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"create": "allow", "rewrite": "allow"}, "unknown operations ['rewrite']"),
        ({"create": "everything"}, "modes must be one of"),
        ("everything", "or a mapping of operation to mode"),
    ],
)
def test_malformed_body_authority_is_rejected(body, fragment):
    errors = pp.validate_capability("x", {"rule_id": "r", "body": body})
    assert any(fragment in e for e in errors), errors


def test_a_valid_body_mapping_validates_clean():
    assert pp.validate_capability("x", {"rule_id": "r", "body": {"create": "allow"}}) == []


def _policy(capability: dict) -> dict:
    return {"rules": [{"id": "r", "severity": "reject"}], "capabilities": {"a": capability}}


def test_evaluation_uses_the_operations_own_body_mode():
    policy = _policy({"rule_id": "r", "operations": ["create", "update", "append"],
                      "paths": ["**"], "update_fields": ["*"],
                      "body": {"create": "allow", "update": "deny", "append": "append-only"}})
    assert pp.evaluate_capability(policy, "a", "create", "x/y.md", body_change="replace") is None
    assert pp.evaluate_capability(policy, "a", "append", "x/y.md", body_change="append") is None
    denied = pp.evaluate_capability(policy, "a", "update", "x/y.md", body_change="replace")
    assert denied["message"] == "body mutation is denied"
    assert denied["evidence"]["body"] == "deny"
    over = pp.evaluate_capability(policy, "a", "append", "x/y.md", body_change="replace")
    assert over["message"] == "body replacement exceeds append-only authority"
    assert pp.evaluate_capability(policy, "a", "update", "x/y.md", body_change="none") is None


def test_check_prompt_reads_the_update_body_mode():
    policy = {"capabilities": {"a": {"body": {"create": "allow"}, "update_fields": []}}}
    assert pp.check_prompt(policy, "a", "") == [
        "prompt does not state NO body for body-denied capability"]
    policy["capabilities"]["a"]["body"] = {"update": "allow"}
    assert pp.check_prompt(policy, "a", "") == []


# --- the engine floor ----------------------------------------------------------------------


def test_the_engine_floor_composes_and_sits_inside_its_own_ceiling():
    policy = pp.compose_documents([CATALOG])
    cap = policy["capabilities"]["chat:api_server"]
    assert set(cap["operations"]) == {"create", "update", "append"}
    assert cap["paths"] == ["entities/**", "concepts/**"]
    assert pp.chat_ceiling_errors("chat:api_server", cap, cap) == []
    # every curated-authority attestation field is protected (okengine#980)
    from okengine import attestation
    assert set(attestation.OPERATOR_FIELDS) <= set(cap["protected_fields"])


def test_an_engine_chat_capability_above_the_ceiling_fails_composition(tmp_path, monkeypatch):
    doc = pp.load_document(CATALOG)
    doc["capabilities"]["chat:api_server"]["operations"].append("tombstone")
    engine = tmp_path / "catalog.yaml"
    engine.write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_POLICY_CATALOG", str(engine))
    with pytest.raises(pp.PolicyError, match="exceed the chat ceiling"):
        pp.compose_documents([engine])


def test_an_engine_chat_capability_naming_an_unknown_rule_fails(tmp_path, monkeypatch):
    doc = pp.load_document(CATALOG)
    doc["capabilities"]["chat:api_server"]["rule_id"] = "no-such-rule"
    engine = tmp_path / "catalog.yaml"
    engine.write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_POLICY_CATALOG", str(engine))
    with pytest.raises(pp.PolicyError, match="unknown rule_id 'no-such-rule'"):
        pp.compose_documents([engine])


# --- pack replacement ----------------------------------------------------------------------


def test_a_pack_may_narrow_the_chat_capability(tmp_path):
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(
        operations=["update", "append"], paths=["entities/**"])})
    policy = pp.compose_documents([CATALOG, pack])
    cap = policy["capabilities"]["chat:api_server"]
    assert cap["operations"] == ["update", "append"]
    assert cap["paths"] == ["entities/**"]


def test_a_pack_may_widen_into_its_own_knowledge_namespace(tmp_path):
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(
        paths=["entities/**", "concepts/**", "cves/**", "wiki/techniques/**"])})
    policy = pp.compose_documents([CATALOG, pack])
    assert "cves/**" in policy["capabilities"]["chat:api_server"]["paths"]
    assert pp.evaluate_capability(policy, "chat:api_server", "create", "cves/c/cve-1.md",
                                  "vulnerability", ["title"], "replace") is None


@pytest.mark.parametrize("operation", ["tombstone", "converge", "review", "flag", "patch", "import"])
def test_a_pack_may_not_add_an_operation_beyond_the_ceiling(tmp_path, operation):
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(
        operations=["create", "update", "append", operation])})
    with pytest.raises(pp.PolicyError, match="exceed the chat ceiling"):
        pp.compose_documents([CATALOG, pack])


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("allow", "body 'allow' on update exceeds"),
        ({"create": "allow", "update": "append-only", "append": "append-only"},
         "body 'append-only' on update exceeds"),
        ({"create": "allow", "append": "allow"}, "body 'allow' on append exceeds"),
    ],
)
def test_a_pack_may_not_widen_body_authority(tmp_path, body, fragment):
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(body=body)})
    with pytest.raises(pp.PolicyError, match=fragment):
        pp.compose_documents([CATALOG, pack])


@pytest.mark.parametrize(
    "pattern",
    ["sources/**", "assessments/**", "dashboards/**", "operational/**", "raw/**",
     "system/**", "reviews/**", "_review-queue/**", ".okengine/**", "**", "*/x/**",
     "log.md", "wiki/sources/**", "/sources/**", "wiki:entities/**"],
)
def test_a_pack_may_not_reach_a_forbidden_or_unbounded_path(tmp_path, pattern):
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(paths=["entities/**", pattern])})
    with pytest.raises(pp.PolicyError, match="capability chat:api_server: path"):
        pp.compose_documents([CATALOG, pack])


def test_a_pack_may_narrow_body_authority_and_protect_more_fields(tmp_path):
    cap = _replacement(body={"append": "append-only"},
                       protected_fields=_floor()["protected_fields"] + ["confidence"])
    pack = _pack_doc(tmp_path, {"chat:api_server": cap})
    policy = pp.compose_documents([CATALOG, pack])
    assert policy["capabilities"]["chat:api_server"]["body"] == {"append": "append-only"}
    assert "confidence" in policy["capabilities"]["chat:api_server"]["protected_fields"]


def test_a_chat_replacement_does_not_end_its_documents_capabilities(tmp_path):
    rule = dict(next(r for r in pp.load_document(CATALOG)["rules"]
                     if r["id"] == "source-quality-fields-only"), id="pack-rule")
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(),
                                "cron:pack-lane": {"rule_id": "pack-rule"}}, rules=[rule])
    policy = pp.compose_documents([CATALOG, pack])
    assert "cron:pack-lane" in policy["capabilities"]


def test_a_pack_may_not_drop_the_path_scope(tmp_path):
    cap = _replacement()
    cap.pop("paths")
    pack = _pack_doc(tmp_path, {"chat:api_server": cap})
    with pytest.raises(pp.PolicyError, match="empty scope is unbounded"):
        pp.compose_documents([CATALOG, pack])


def test_a_pack_must_keep_every_floor_protected_field(tmp_path):
    cap = _replacement(protected_fields=[f for f in _floor()["protected_fields"] if f != "tlp"])
    pack = _pack_doc(tmp_path, {"chat:api_server": cap})
    with pytest.raises(pp.PolicyError, match=r"missing \['tlp'\]"):
        pp.compose_documents([CATALOG, pack])


def test_the_engine_is_the_first_document_wherever_the_paths_sort(tmp_path):
    """Identity is position, not path order: a pack file that sorts before the catalog is still
    a pack, held to the floor it replaces."""
    (tmp_path / "z").mkdir()
    (tmp_path / "a").mkdir()
    engine = tmp_path / "z" / "catalog.yaml"
    engine.write_text(CATALOG.read_text(encoding="utf-8"), encoding="utf-8")
    cap = _replacement(protected_fields=[f for f in _floor()["protected_fields"] if f != "tlp"])
    pack = _pack_doc(tmp_path / "a", {"chat:api_server": cap})
    with pytest.raises(pp.PolicyError, match=r"missing \['tlp'\]"):
        pp.compose_documents([engine, pack])


def test_a_pack_may_not_declare_a_chat_actor_the_engine_never_did(tmp_path):
    pack = _pack_doc(tmp_path, {"chat:telegram": _replacement()})
    with pytest.raises(pp.PolicyError, match="only an engine-declared chat capability"):
        pp.compose_documents([CATALOG, pack])


def test_two_packs_may_not_both_replace_the_chat_capability(tmp_path):
    one = _pack_doc(tmp_path, {"chat:api_server": _replacement()}, name="one.yaml")
    two = _pack_doc(tmp_path, {"chat:api_server": _replacement()}, name="two.yaml")
    with pytest.raises(pp.PolicyError, match="already replaced by"):
        pp.compose_documents([CATALOG, one, two])


def test_a_pack_replacement_naming_an_unknown_rule_fails(tmp_path):
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(rule_id="pack-made-up")})
    with pytest.raises(pp.PolicyError, match="unknown rule_id 'pack-made-up'"):
        pp.compose_documents([CATALOG, pack])


def test_a_pack_may_not_redefine_the_chat_rule(tmp_path):
    rule = dict(next(r for r in pp.load_document(CATALOG)["rules"]
                     if r["id"] == "chat-write-back-floor"))
    pack = _pack_doc(tmp_path, {}, rules=[rule])
    with pytest.raises(pp.PolicyError, match="non-overridable"):
        pp.compose_documents([CATALOG, pack])


def test_every_other_duplicate_capability_is_still_ambiguous(tmp_path):
    cap = dict(pp.load_document(CATALOG)["capabilities"]["cron:source-quality-backfill"])
    rules = [r for r in pp.load_document(CATALOG)["rules"]
             if r["id"] == "source-quality-fields-only"]
    rules = [dict(rules[0], id="pack-sq")]
    pack = _pack_doc(tmp_path, {"cron:source-quality-backfill": dict(cap, rule_id="pack-sq")},
                     rules=rules)
    with pytest.raises(pp.PolicyError, match="ambiguous duplicate capability"):
        pp.compose_documents([CATALOG, pack])


def test_a_non_chat_pack_capability_still_needs_a_rule_in_its_own_document(tmp_path):
    pack = _pack_doc(tmp_path, {"cron:pack-lane": {"rule_id": "chat-write-back-floor"}})
    with pytest.raises(pp.PolicyError, match="unknown rule_id 'chat-write-back-floor'"):
        pp.compose_documents([CATALOG, pack])


# --- the framework validate gate ------------------------------------------------------------


def test_framework_validate_fails_a_pack_exceeding_the_ceiling(tmp_path):
    """The earliest gate: `framework validate` composes the pack's policy and FAILs on it."""
    sys.path.insert(0, str(REPO / "scripts"))
    import framework_validate_policy as fvp

    class Report:
        def __init__(self):
            self.fails, self.oks = [], []

        def fail(self, name, detail):
            self.fails.append((name, detail))

        def ok(self, name, detail):
            self.oks.append((name, detail))

    (tmp_path / "wiki").mkdir()
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "rules": [],
        "capabilities": {"chat:api_server": _replacement(paths=["sources/**"])}}))
    report = Report()
    fvp.check_policy_plane(tmp_path, report, engine_root=REPO)
    assert report.fails and "forbidden namespace 'sources'" in report.fails[0][1], report.fails


# --- okengine#991 [7]: a walk-up sub-domain prefix does not carry chat past the ceiling ------


@pytest.mark.parametrize(
    "pattern",
    ["okcti/sources/**", "okcti/assessments/**", "wiki/okcti/reviews/**", "entities/raw/**"],
)
def test_a_pack_may_not_reach_a_forbidden_namespace_below_the_first_segment(tmp_path, pattern):
    """In a walk-up host the lane-owned namespaces sit one level down, at <slug>/<namespace>/."""
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(paths=["entities/**", pattern])})
    with pytest.raises(pp.PolicyError, match="reaches the forbidden namespace"):
        pp.compose_documents([CATALOG, pack])


@pytest.mark.parametrize("pattern", ["entities/_/**", "entities/t/_/**", "concepts/x/_/**"])
def test_a_shard_directory_in_a_chat_scope_composes(tmp_path, pattern):
    """okf_migrate files a slug with a non-alphanumeric (second) letter under a `_` shard:
    ordinary entity storage, not a reserved namespace (QA of !1252)."""
    pack = _pack_doc(tmp_path, {"chat:api_server": _replacement(paths=["entities/**", pattern])})
    assert pattern in pp.compose_documents([CATALOG, pack])["capabilities"]["chat:api_server"][
        "paths"]


def _chat_policy(paths: list[str]) -> dict:
    return {"rules": [{"id": "chat-write-back-floor", "severity": "reject"}],
            "capabilities": {"chat:api_server": dict(_floor(), paths=paths)}}


def _chat_create(subject: str, namespace=None, paths=("okcti/**", "entities/**", "concepts/**")):
    return pp.evaluate_capability(_chat_policy(list(paths)), "chat:api_server", "create",
                                  subject, page_type="actor", changed_fields=["type"],
                                  body_change="replace", namespace=namespace)


@pytest.mark.parametrize(
    ("subject", "namespace"),
    [("okcti/sources/2026/10/a-post.md", "okcti/sources"),
     ("okcti/assessments/a/claim.md", "okcti/assessments"),
     ("okcti/reviews/x.md", "okcti/reviews"),
     ("okcti/_archive/old.md", "okcti/_archive"),
     ("okcti/.okengine/x.md", "okcti/.okengine"),
     ("okcti/raw/capture.md", "okcti/raw")],
)
def test_a_chat_write_under_a_sub_domain_cannot_reach_a_forbidden_namespace(subject, namespace):
    """`okcti/**` names a sub-domain, not a namespace, so composition cannot tell it from
    `entities/**`. The write path passes the page's namespace, resolved through the governing
    schema walk-up, and the chat ceiling is applied to it whatever scope the capability declares."""
    refused = _chat_create(subject, namespace)
    assert refused and "forbidden namespace" in refused["message"], refused


@pytest.mark.parametrize(
    ("subject", "namespace"),
    [("okcti/entities/a/apt.md", "okcti/entities"), ("okcti/concepts/e/edge.md", "okcti/concepts"),
     ("entities/t/_/t-mobile.md", "entities"), ("entities/_/-dash.md", "entities"),
     ("concepts/x/_/x-ops.md", "concepts"), ("okcti/entities/t/_/t-rex.md", "okcti/entities"),
     ("entities/s/sources.md", "entities")],
)
def test_shard_directories_below_a_knowledge_namespace_are_ordinary_storage(subject, namespace):
    """Only the namespace position counts: `_` shards and a page slug named like a namespace are
    storage, not reserved namespaces (QA of !1252: entities/t/_/t-mobile was refused)."""
    assert _chat_create(subject, namespace) is None


@pytest.mark.parametrize(("subject", "allowed"), [
    ("sources/a.md", False), ("_archive/a.md", False), ("entities/t/_/t-mobile.md", True),
])
def test_without_a_resolved_namespace_the_first_segment_is_the_namespace(subject, allowed):
    """A pure caller that passes no namespace (no vault to walk) is judged on the first segment."""
    result = _chat_create(subject, paths=("**",))
    assert (result is None) is allowed, result


def test_a_non_chat_actor_is_not_held_to_the_chat_namespace_ceiling():
    policy = {"rules": [{"id": "r", "severity": "reject"}],
              "capabilities": {"cron:x": {"rule_id": "r", "operations": ["create"],
                                          "paths": ["okcti/**"], "update_fields": ["*"],
                                          "body": "allow"}}}
    assert pp.evaluate_capability(policy, "cron:x", "create", "okcti/sources/a.md",
                                  changed_fields=["type"], body_change="replace",
                                  namespace="okcti/sources") is None
