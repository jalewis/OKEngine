"""Enforcement coverage for the model-write output contract (okengine#462, tranche T1).

`output_contract_enforce.evaluate()` is the synchronous guard the ENFORCED write path
runs on every model-authored write. It is what rejects a lane that writes outside its
declared namespace/type, omits required fields, ships an empty or stub body, invents
unknown fields, leaves placeholder or unresolved links, or fails to ground a required
relationship.

Despite being that guard, it had no dedicated test file and sat at 71% — the weakest
module in the write path. Every test here asserts a REJECTION actually fires (or is
correctly withheld), not merely that the function executes.
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "okengine-mcp" / "output_contract_enforce.py"

spec = importlib.util.spec_from_file_location("output_contract_enforce", MOD)
oce = importlib.util.module_from_spec(spec)
sys.modules["output_contract_enforce"] = oce
spec.loader.exec_module(oce)


def contract(**overrides):
    value = {
        "api": 1,
        "allowed_namespaces": ["entities"],
        "allowed_types": ["actor"],
        "operations": ["create", "update"],
        "required_fields": ["type"],
        "required_relationships": [],
        "body": {"required": True, "min_non_whitespace": 10},
        "unknown_fields": "review",
        "unresolved_links": "review",
        "placeholder_links": "review",
    }
    value.update(overrides)
    return value


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """Point the module at a synthetic cron-jobs store; return a caller builder."""
    counter = {"n": 0}

    def build(name="demo-lane", **job_fields):
        counter["n"] += 1
        job = {"name": name}
        job.update(job_fields)
        path = tmp_path / f"jobs-{counter['n']}.json"
        path.write_text(json.dumps({"jobs": [job]}))
        monkeypatch.setenv("OKENGINE_CRON_JOBS", str(path))
        # the module memoises the jobs store process-wide
        oce._cache.update(key=None, jobs={})
        return {"kind": "job", "actor": f"cron:{name}"}

    return build


def ev(caller, wiki, *, operation="create", namespace="entities", page_type="actor",
       frontmatter=None, body="a body long enough to pass", unknown_fields=None):
    return oce.evaluate(
        caller,
        operation=operation,
        namespace=namespace,
        page_type=page_type,
        frontmatter={"type": "actor"} if frontmatter is None else frontmatter,
        body=body,
        unknown_fields=unknown_fields or [],
        wiki=wiki,
    )


def codes(findings):
    return {f["code"] for f in findings}


# ── identity resolution ────────────────────────────────────────────────────────

def test_non_job_caller_is_not_contract_enforced(tmp_path):
    """Interactive/human callers carry no lane identity, so no contract applies."""
    assert oce.resolve({"kind": "user", "actor": "jlewis"}) == (None, None)
    assert ev({"kind": "user", "actor": "jlewis"}, tmp_path) == []


def test_unknown_lane_is_refused_not_waved_through(lane, tmp_path):
    """A job identity with no matching job must FAIL CLOSED, not skip enforcement."""
    caller = lane("known-lane", output_contract=contract())
    stranger = {"kind": "job", "actor": "cron:no-such-lane"}
    resolved, name = oce.resolve(stranger)
    assert resolved == {"_missing": True}
    assert name == "no-such-lane"
    assert codes(ev(stranger, tmp_path)) == {"contract_not_resolved"}


def test_lane_without_contract_is_refused_unless_explicitly_exempt(lane, tmp_path):
    caller = lane("no-contract-lane")
    assert codes(ev(caller, tmp_path)) == {"contract_not_resolved"}

    exempt = lane("exempt-lane", output_contract_exempt=True)
    assert oce.resolve(exempt)[0] is None
    assert ev(exempt, tmp_path) == []


def test_stamped_digest_mismatch_is_rejected(lane, tmp_path):
    """A generated contract whose stamped digest no longer matches must not be trusted."""
    body = contract()
    caller = lane("drifted", output_contract=body,
                  output_contract_digest="sha256:" + "0" * 64)
    assert oce.resolve(caller)[0] == {"_invalid_digest": True}
    assert codes(ev(caller, tmp_path)) == {"contract_digest_mismatch"}


def test_capability_is_derived_only_from_resolved_job_contract(lane):
    caller = lane("daily", output_contract=contract(
        allowed_namespaces=["briefings"], allowed_types=["briefing"],
        operations=["create"],
    ))

    assert oce.capability(caller) == {
        "rule_id": "engine-authenticated-writer",
        "operations": ["create"],
        "paths": ["briefings/**"],
        "types": ["briefing"],
        "update_fields": ["*"],
        "body": "allow",
    }
    assert oce.capability({"kind": "job", "actor": "cron:missing"}) is None
    assert oce.capability({"kind": "user", "actor": "operator"}) is None


def test_capability_maps_explicit_all_namespace_to_all_paths(lane):
    caller = lane("all", output_contract=contract(allowed_namespaces=["*"]))

    assert oce.capability(caller)["paths"] == ["**"]


def test_matching_digest_is_accepted(lane, tmp_path):
    body = contract()
    caller = lane("stamped", output_contract=body,
                  output_contract_digest=oce._digest(body))
    assert oce.resolve(caller)[0] == body
    assert ev(caller, tmp_path) == []


def test_unreadable_or_missing_jobs_store_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(tmp_path / "absent.json"))
    oce._cache.update(key=None, jobs={})
    assert oce._jobs() == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(broken))
    oce._cache.update(key=None, jobs={})
    assert oce._jobs() == {}


# ── scope guards ───────────────────────────────────────────────────────────────

def test_namespace_and_type_outside_contract_are_rejected(lane, tmp_path):
    caller = lane("scoped", output_contract=contract())
    found = codes(ev(caller, tmp_path, namespace="sources", page_type="source"))
    assert "namespace_not_allowed" in found
    assert "type_not_allowed" in found


def test_wildcard_contract_permits_any_namespace_and_type(lane, tmp_path):
    caller = lane("wild", output_contract=contract(allowed_namespaces=["*"],
                                                   allowed_types=["*"]))
    assert ev(caller, tmp_path, namespace="anything", page_type="whatever") == []


def test_operation_outside_contract_is_rejected(lane, tmp_path):
    caller = lane("ops", output_contract=contract(operations=["update"]))
    assert "operation_not_allowed" in codes(ev(caller, tmp_path, operation="create"))


# ── field / body guards ────────────────────────────────────────────────────────

def test_required_field_missing_is_rejected_including_empty_values(lane, tmp_path):
    caller = lane("fields", output_contract=contract(required_fields=["type", "origin"]))
    for empty in (None, "", [], {}):
        found = ev(caller, tmp_path, frontmatter={"type": "actor", "origin": empty})
        assert "required_field_missing" in codes(found)
        assert "origin" in [f["message"] for f in found if f["code"] == "required_field_missing"][0]


def test_empty_body_is_rejected_when_contract_requires_one(lane, tmp_path):
    caller = lane("body", output_contract=contract(body={"required": True}))
    assert "body_required" in codes(ev(caller, tmp_path, body="   \n\t  "))


def test_body_below_minimum_meaningful_length_is_rejected(lane, tmp_path):
    caller = lane("short", output_contract=contract(
        body={"required": True, "min_non_whitespace": 50}))
    found = ev(caller, tmp_path, body="too short")
    assert "body_too_short" in codes(found)
    # whitespace must not count toward the minimum
    assert "body_too_short" in codes(ev(caller, tmp_path, body="x" + " " * 200))


def test_frontmatter_only_update_does_not_reject_legacy_body_links(lane, tmp_path):
    caller = lane("metadata-update", output_contract=contract(
        unresolved_links="reject", placeholder_links="reject"))
    caller["body_links_changed"] = False
    assert ev(
        caller,
        tmp_path,
        operation="update",
        body="Existing body with [[entities/legacy-missing]] and [placeholder](#).",
    ) == []


def test_unknown_fields_rejected_only_when_contract_says_reject(lane, tmp_path):
    reviewing = lane("review-unknown", output_contract=contract(unknown_fields="review"))
    assert ev(reviewing, tmp_path, unknown_fields=["vibe"]) == []

    rejecting = lane("reject-unknown", output_contract=contract(unknown_fields="reject"))
    found = ev(rejecting, tmp_path, unknown_fields=["vibe", "mood"])
    assert "unknown_fields" in codes(found)


# ── link / grounding guards ────────────────────────────────────────────────────

def test_placeholder_links_rejected_only_when_contract_says_reject(lane, tmp_path):
    body = "see [the docs](#) for more"
    reviewing = lane("review-ph", output_contract=contract(placeholder_links="review"))
    assert ev(reviewing, tmp_path, body=body) == []

    rejecting = lane("reject-ph", output_contract=contract(placeholder_links="reject"))
    assert "placeholder_link" in codes(ev(rejecting, tmp_path, body=body))


def test_unresolved_wikilinks_are_rejected_and_resolved_ones_pass(lane, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "acme.md").write_text("# Acme")

    caller = lane("links", output_contract=contract(unresolved_links="reject"))

    grounded = ev(caller, wiki, body="refers to [[entities/a/acme]] in full")
    assert grounded == []

    dangling = ev(caller, wiki, body="refers to [[entities/a/ghost]] in full")
    found = [f for f in dangling if f["code"] == "unresolved_link"]
    assert found and "entities/a/ghost" in found[0]["message"]

    # a bare name with no namespace can never resolve to a sharded page
    assert "unresolved_link" in codes(ev(caller, wiki, body="see [[acme]] please"))


def test_canonical_shard_omitting_body_and_relationship_links_are_grounded(lane, tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "entities" / "q" / "qilin.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Qilin\n", encoding="utf-8")
    caller = lane("canonical-links", output_contract=contract(
        unresolved_links="reject", optional_relationships=["entity"],
    ))
    assert ev(caller, wiki, body="See [[entities/qilin]] for the canonical actor.",
              frontmatter={"type": "actor", "entity": "[[entities/qilin]]"}) == []


def test_link_resolver_rejects_escape_symlink_and_malformed_paths(tmp_path):
    wiki = tmp_path / "wiki"
    namespace = wiki / "entities"
    namespace.mkdir(parents=True)
    (wiki / "root.md").write_text("# Not a namespaced page\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (namespace / "outside.md").symlink_to(outside)
    index = oce.link_index(wiki)
    for target in ("entities/outside", "/entities/outside", "entities/../outside",
                   "entities/./outside", "entities//outside", "entities\\outside",
                   "outside", "entities/outside/"):
        assert not oce.link_resolves(wiki, target), target
        assert not oce.link_resolves(wiki, target, index), target


def test_link_resolver_fails_closed_if_namespace_disappears_after_parent_probe(
        tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    parent = wiki / "entities" / "nested"
    parent.mkdir(parents=True)
    namespace = wiki / "entities"
    real_is_dir = Path.is_dir
    probes = {namespace: 0}

    def racing_is_dir(path):
        if path == namespace:
            probes[namespace] += 1
            return False
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", racing_is_dir)
    assert oce.link_resolves(wiki, "entities/nested/missing") is False


def test_unresolved_wikilinks_only_reviewed_when_contract_says_review(lane, tmp_path):
    caller = lane("soft-links", output_contract=contract(unresolved_links="review"))
    assert ev(caller, tmp_path, body="refers to [[entities/a/ghost]] here") == []


def test_required_relationship_absent_is_rejected(lane, tmp_path):
    caller = lane("rel-missing", output_contract=contract(required_relationships=["sources"]))
    for absent in ({}, {"sources": []}, {"sources": None}, {"sources": ""}):
        frontmatter = {"type": "actor", **absent}
        assert "required_relationship_missing" in codes(
            ev(caller, tmp_path, frontmatter=frontmatter))


def test_required_relationship_must_resolve_to_a_real_page(lane, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "sources" / "2026").mkdir(parents=True)
    (wiki / "sources" / "2026" / "real.md").write_text("# Real")

    caller = lane("rel", output_contract=contract(required_relationships=["sources"]))

    # wikilink form and bare-path form both resolve
    for value in ("[[sources/2026/real]]", "sources/2026/real", "sources/2026/real.md"):
        assert ev(caller, wiki, frontmatter={"type": "actor", "sources": [value]}) == []

    # a fabricated reference is rejected — the anti-hallucination grounding check
    found = ev(caller, wiki, frontmatter={"type": "actor", "sources": ["[[sources/2026/invented]]"]})
    assert "relationship_unresolved" in codes(found)

    # a scalar (non-list) value is still checked
    assert "relationship_unresolved" in codes(
        ev(caller, wiki, frontmatter={"type": "actor", "sources": "sources/2026/invented"}))


def test_optional_relationship_may_be_absent_but_present_target_must_resolve(lane, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "predictions").mkdir(parents=True)
    (wiki / "predictions" / "accepted.md").write_text("# Accepted")
    caller = lane("optional-link", output_contract=contract(
        optional_relationships=["prediction_candidate"],
    ))

    assert ev(caller, wiki, frontmatter={"type": "actor"}) == []
    assert ev(caller, wiki, frontmatter={"type": "actor", "prediction_candidate": []}) == []
    assert ev(caller, wiki, frontmatter={"type": "actor", "prediction_candidate":
                                              "predictions/accepted"}) == []
    refused = ev(caller, wiki, frontmatter={"type": "actor", "prediction_candidate":
                                               "predictions/rejected"})
    assert "relationship_unresolved" in codes(refused)
    assert "prediction_candidate" in refused[0]["message"]


def test_relationship_wikilink_alias_and_anchor_forms_resolve(lane, tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "sources" / "2026").mkdir(parents=True)
    (wiki / "sources" / "2026" / "real.md").write_text("# Real")
    caller = lane("rel-alias", output_contract=contract(required_relationships=["sources"]))

    for value in ("[[sources/2026/real|Nice Title]]", "[[sources/2026/real#section]]"):
        assert ev(caller, wiki, frontmatter={"type": "actor", "sources": [value]}) == []


def test_findings_carry_the_lane_name_for_attribution(lane, tmp_path):
    caller = lane("attributed", output_contract=contract())
    found = ev(caller, tmp_path, namespace="sources")
    assert found and all(f["lane"] == "attributed" for f in found)


# ── mutants that survived the critical campaign (2026-09-11/12) ────────────────
#
# This module was classified `critical: true` in mutation/targets.json on 2026-09-10 without
# triaging its survivors. The nightly campaign has failed on shard 1/3 ever since — "survivor
# lacks owner/disposition" — while the push pipeline stayed green, because the critical campaign
# only runs on a schedule. Each test below kills a specific survivor rather than adding coverage
# in general, and names the mutation it rejects so the intent survives a refactor.


def test_digest_is_stable_across_key_order(lane, tmp_path):
    """Kills ReplaceTrueWithFalse at the `sort_keys=True` in _digest.

    The digest is the tamper check on a generated contract. With sort_keys=False two dicts
    carrying identical policy but built in a different order hash differently, so a lane whose
    jobs file was regenerated would be rejected as tampered.
    """
    a = {"api": 1, "operations": ["create"], "allowed_types": ["actor"]}
    b = {"allowed_types": ["actor"], "api": 1, "operations": ["create"]}
    assert a == b and list(a) != list(b), "fixture must differ in insertion order only"
    assert oce._digest(a) == oce._digest(b)


def test_digest_hashes_unescaped_utf8_not_ascii_escapes():
    """Kills ReplaceFalseWithTrue at the `ensure_ascii=False` in _digest.

    Comparing a contract's digest against itself cannot catch this — flipping the flag changes
    both sides equally. The digest must be pinned to the UTF-8 serialisation specifically.

    It matters because the digest is a cross-process tamper check: the writer stamps it and this
    module re-derives it. If the two ever disagree on escaping — different Python, different
    json defaults — every non-ASCII contract is rejected as forged. Pinning the encoding here
    makes that a test failure rather than a fleet-wide write refusal.
    """
    value = {"api": 1, "allowed_types": ["actör"]}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    escaped = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert raw != escaped, "fixture must actually differ between the two encodings"
    expected = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert oce._digest(value) == expected
    assert oce._digest(value) != "sha256:" + hashlib.sha256(escaped.encode()).hexdigest()


def test_the_jobs_store_is_cached_between_calls(lane, tmp_path, monkeypatch):
    """Kills ReplaceComparisonOperator_NotEq_IsNot on the cache-key check.

    `key` is a freshly built tuple on every call, so `is not` is always true and the store is
    re-read and re-parsed on every single write. Identity of the returned mapping is what
    distinguishes a cache hit from a silent re-parse; equality cannot.

    This is the enforced write path — it runs on every model-authored write, and the jobs file
    on a live gateway carries the whole cron fleet.
    """
    caller = lane(output_contract=contract())
    first = oce._jobs()
    second = oce._jobs()
    assert first is second, "unchanged jobs store was re-read instead of served from cache"
    assert caller["actor"].removeprefix("cron:") in first


def test_a_caller_kind_sorting_before_job_is_still_rejected(tmp_path):
    """Kills ReplaceComparisonOperator_NotEq_Gt on `caller.get("kind") != "job"`.

    With `>`, any kind that sorts BEFORE "job" compares False and falls through as if it were an
    authenticated lane. "admin" is the dangerous shape: it would acquire contract resolution it
    must never have.
    """
    assert "admin" < "job", "fixture must sort before 'job' for this mutant to matter"
    assert oce.resolve({"kind": "admin", "actor": "cron:demo-lane"}) == (None, None)
    assert ev({"kind": "admin", "actor": "cron:demo-lane"}, tmp_path) == []


def test_job_kind_is_compared_by_value_not_identity(lane, tmp_path):
    """Kills ReplaceComparisonOperator_NotEq_IsNot on the same line.

    `is not` passes only while the literal happens to be interned. A kind string assembled at
    runtime — from JSON, from a config read — is a distinct object, and identity comparison
    would refuse every real caller while the unit tests kept passing on interned literals.
    """
    computed = "".join(["j", "o", "b"])
    assert computed == "job" and computed is not "job"  # noqa: F632 — the point of the test
    caller = lane(output_contract=contract())
    caller["kind"] = computed
    resolved, lane_name = oce.resolve(caller)
    assert lane_name == "demo-lane" and resolved is not None


def test_a_stamped_digest_greater_than_the_real_one_is_rejected(lane):
    """Kills ReplaceComparisonOperator_NotEq_Lt on the digest comparison.

    With `<` only a stamped digest sorting BELOW the computed one is caught, so a tampered
    contract whose stamp happens to sort higher passes the tamper check. The direction of a
    string comparison must not decide whether forgery is detected.
    """
    value = contract()
    real = oce._digest(value)
    higher = "sha256:" + "f" * 64
    assert higher > real, "fixture must sort above the real digest"
    caller = lane(output_contract=value, output_contract_digest=higher)
    resolved, _lane = oce.resolve(caller)
    assert resolved == {"_invalid_digest": True}


def test_caller_may_be_passed_by_keyword(lane, tmp_path):
    """Kills ReplaceBinaryOperator_Mul_Div on evaluate's keyword-only marker.

    `*` makes everything after it keyword-only; `/` would make `caller` positional-ONLY, so any
    caller passing it by name breaks. Nothing else in the suite exercises that spelling.
    """
    c = lane(output_contract=contract())
    findings = oce.evaluate(
        caller=c, operation="create", namespace="entities", page_type="actor",
        frontmatter={"type": "actor"}, body="a body long enough to pass",
        unknown_fields=[], wiki=tmp_path)
    assert findings == []


def test_a_body_exactly_at_the_minimum_is_accepted(lane, tmp_path):
    """Kills ReplaceComparisonOperator_Lt_LtE on the body-length check.

    With `<=` a body of exactly the declared minimum is rejected, which makes the documented
    bound off by one and fails writes that satisfy the contract as written.
    """
    c = lane(output_contract=contract(body={"required": True, "min_non_whitespace": 10}))
    assert ev(c, tmp_path, body="0123456789") == []
    assert "body_too_short" in codes(ev(c, tmp_path, body="012345678"))


def test_an_empty_body_without_a_declared_minimum_is_accepted(lane, tmp_path):
    """Kills NumberReplacer on the `or 0` default of min_non_whitespace.

    With a default of 1 an empty body fails a contract that never asked for one, so a lane
    writing a legitimately body-less page would be blocked.
    """
    c = lane(output_contract=contract(body={}))
    assert "body_too_short" not in codes(ev(c, tmp_path, body=""))


@pytest.mark.parametrize("field,value,code,kwargs", [
    ("unknown_fields", "allow", "unknown_fields", {"unknown_fields": ["invented"]}),
    ("placeholder_links", "allow", "placeholder_link", {"body": "see [stub](#) here"}),
    ("unresolved_links", "allow", "unresolved_link", {"body": "see [[entities/ghost]] here"}),
])
def test_a_non_reject_disposition_does_not_reject(lane, tmp_path, field, value, code, kwargs):
    """Kills the three ReplaceComparisonOperator_Eq_LtE mutants on `== "reject"`.

    With `<=`, any disposition sorting at or below "reject" — "allow" among them — is treated as
    a rejection. A contract that explicitly permits these would start refusing writes, which is
    the opposite of what it declares.
    """
    assert value < "reject", "fixture must sort below 'reject' for this mutant to matter"
    c = lane(output_contract=contract(**{field: value}))
    assert code not in codes(ev(c, tmp_path, **kwargs))


def test_every_missing_relationship_is_reported_not_just_the_first(lane, tmp_path):
    """Kills ReplaceContinueWithBreak in the required-relationships loop.

    With `break` the loop stops at the first absent relationship, so a page missing two required
    relationships is told about one, fixed, and rejected again on the next write. The author
    cannot see the real cost of the change.
    """
    c = lane(output_contract=contract(required_relationships=["operator", "sponsor"]))
    findings = ev(c, tmp_path, frontmatter={"type": "actor"})
    missing = [f for f in findings if f["code"] == "required_relationship_missing"]
    assert len(missing) == 2, f"expected both relationships reported, got {missing}"
    assert "operator" in missing[0]["message"] and "sponsor" in missing[1]["message"]
