"""okengine#993 [A1]: the policy plane's value-level capability key, `forbidden_values`.

A capability may declare values a changed field may never take. Values compare case- and
whitespace-insensitively, like `okf_migrate.is_tombstoned`. Every chat capability (the engine floor
and any pack replacement) must forbid at least the engine's retirement statuses on `status`, and a
pack replacement may not drop a value rule the engine floor declares. The write path's use of the
key is pinned by tests/test_chat_status_retirement_993.py; this file is pure, so it can sit in the
policy_plane mutation target's bounded scoring suite.
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


def _policy(capability: dict) -> dict:
    return {"rules": [{"id": "r", "severity": "reject"}], "capabilities": {"a": capability}}


_CAP = {"rule_id": "r", "operations": ["create", "update", "append", "tombstone"],
        "paths": ["**"], "update_fields": ["*"], "body": "allow",
        "forbidden_values": {"status": ["retired", "superseded"]}}


# --- the capability key ------------------------------------------------------------------------


def test_a_well_formed_forbidden_values_mapping_validates_clean():
    assert pp.validate_capability("a", dict(_CAP)) == []


@pytest.mark.parametrize("value", [
    ["status"], {"status": "retired"}, {"status": []}, {"status": [""]}, {"": ["retired"]},
    {"status": [3]},
])
def test_a_malformed_forbidden_values_declaration_is_rejected(value):
    errors = pp.validate_capability("a", dict(_CAP, forbidden_values=value))
    assert any("forbidden_values" in e for e in errors), errors


@pytest.mark.parametrize("sent", ["retired", "Superseded", "  RETIRED  ", ["active", "retired"]])
def test_setting_a_forbidden_value_is_rejected_whatever_its_case_or_spacing(sent):
    out = pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                 changed_fields=["status"], values={"status": sent})
    assert out is not None and "field 'status' may not be set to" in out["message"], out
    assert out["evidence"]["offending_fields"] == ["status"]
    assert out["evidence"]["forbidden_values"] == {"status": ["retired", "superseded"]}


@pytest.mark.parametrize("sent", ["active", "draft", "monitoring-active", "deprecated", None])
def test_an_ordinary_value_passes(sent):
    assert pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                  changed_fields=["status"], values={"status": sent}) is None


def test_an_unchanged_field_is_not_judged():
    """An echo of a page's stored status is not a move to it."""
    assert pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                  changed_fields=["title"],
                                  values={"status": "retired", "title": "T"}) is None


def test_removing_the_field_is_not_setting_a_forbidden_value():
    assert pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                  changed_fields=["status"], values={"title": "T"}) is None


def test_a_changed_field_whose_value_the_caller_did_not_supply_fails_closed():
    out = pp.evaluate_capability(_policy(dict(_CAP)), "a", "tombstone", "x/y.md",
                                 changed_fields=["status"])
    assert out is not None and "cannot be checked against its forbidden values" in out["message"]


def test_a_capability_without_the_key_is_unaffected():
    cap = {k: v for k, v in _CAP.items() if k != "forbidden_values"}
    assert pp.evaluate_capability(_policy(cap), "a", "update", "x/y.md",
                                  changed_fields=["status"], values={"status": "retired"}) is None


# --- the engine retirement set and the chat ceiling --------------------------------------------


def test_the_retirement_set_is_the_engines_own_spelling_of_retired():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import okf_migrate
    assert pp.RETIREMENT_STATUSES == frozenset({"tombstoned", "superseded", "retired"})
    assert okf_migrate.is_tombstoned({"status": next(
        s for s in pp.RETIREMENT_STATUSES if s == "tombstoned")})


def test_the_engine_chat_floor_forbids_every_retirement_status():
    floor = _floor()
    forbidden = {str(v).strip().casefold() for v in floor["forbidden_values"]["status"]}
    assert pp.RETIREMENT_STATUSES <= forbidden


def _pack_doc(tmp_path: Path, capability: dict) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "rules": [],
                                    "capabilities": {"chat:api_server": capability}}),
                    encoding="utf-8")
    return path


def test_a_pack_chat_replacement_may_not_drop_the_status_rule(tmp_path):
    cap = {k: v for k, v in _floor().items() if k != "forbidden_values"}
    with pytest.raises(pp.PolicyError, match="must forbid at least"):
        pp.compose_documents([CATALOG, _pack_doc(tmp_path, cap)])


def test_a_pack_chat_replacement_may_not_narrow_the_retirement_set(tmp_path):
    cap = dict(_floor(), forbidden_values={"status": ["tombstoned"]})
    with pytest.raises(pp.PolicyError, match="missing \\['retired', 'superseded'\\]"):
        pp.compose_documents([CATALOG, _pack_doc(tmp_path, cap)])


def test_a_pack_chat_replacement_may_forbid_more_and_respell(tmp_path):
    cap = dict(_floor(), forbidden_values={
        "status": ["Tombstoned", " SUPERSEDED", "retired", "deprecated"],
        "lifecycle": ["archived"]})
    effective = pp.compose_documents([CATALOG, _pack_doc(tmp_path, cap)])
    assert effective["capabilities"]["chat:api_server"]["forbidden_values"]["lifecycle"] == [
        "archived"]


def test_a_pack_chat_replacement_must_keep_a_floor_rule_beyond_the_ceiling(tmp_path):
    floor = dict(_floor())
    floor["forbidden_values"] = dict(floor["forbidden_values"], lifecycle=["archived"])
    errors = pp.chat_ceiling_errors("chat:api_server", _floor(), floor)
    assert any("'lifecycle'" in e and "archived" in e for e in errors), errors


def test_an_engine_chat_floor_without_the_rule_fails_its_own_ceiling():
    floor = {k: v for k, v in _floor().items() if k != "forbidden_values"}
    errors = pp.chat_ceiling_errors("chat:api_server", floor, floor)
    assert any("must forbid at least" in e for e in errors), errors


_TWO = dict(_CAP, forbidden_values={"lifecycle": ["archived"], "status": ["retired"]})


@pytest.mark.parametrize(("changed", "values"), [
    (["status"], {"lifecycle": "archived", "status": "retired"}),  # earlier field unchanged
    (["lifecycle", "status"], {"status": "retired"}),  # earlier field removed
    (["lifecycle", "status"], {"lifecycle": "live", "status": "retired"}),  # earlier field fine
])
def test_every_declared_field_is_judged_whatever_precedes_it(changed, values):
    out = pp.evaluate_capability(_policy(dict(_TWO)), "a", "update", "x/y.md",
                                 changed_fields=changed, values=values)
    assert out is not None and out["evidence"]["offending_fields"] == ["status"], out


def test_every_unverifiable_field_is_named():
    out = pp.evaluate_capability(_policy(dict(_TWO)), "a", "tombstone", "x/y.md",
                                 changed_fields=["lifecycle", "status"])
    assert out["evidence"]["offending_fields"] == ["lifecycle", "status"], out


# --- QA of !1264 N1: what the write stores is judged against the page as stored -----------------


def test_a_value_the_write_maps_onto_a_forbidden_value_is_a_move():
    """`changed_fields` compares keys and raw values; a value alias applied by the write can turn a
    stored `gone` into `retired` without the field counting as changed."""
    out = pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                 changed_fields=["summary"], values={"status": "retired"},
                                 stored={"status": "gone"})
    assert out is not None and out["evidence"]["offending_fields"] == ["status"], out


@pytest.mark.parametrize(("stored", "values"), [
    ({"status": "Retired "}, {"status": "retired"}),  # the same value, respelled
    ({"status": ["retired", "x"]}, {"status": ["X", "Retired"]}),  # the same list, reordered
    ({"status": "gone"}, {"title": "T"}),  # the write stores no status
    ({"status": "gone"}, {"status": "active"}),  # a move to an ordinary value
])
def test_a_field_the_write_does_not_move_to_a_forbidden_value_passes(stored, values):
    assert pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                  changed_fields=["summary"], values=values,
                                  stored=stored) is None


def test_a_page_without_the_field_moved_onto_a_forbidden_value_is_refused():
    out = pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                 changed_fields=[], values={"status": "superseded"}, stored={})
    assert out is not None and "may not be set to 'superseded'" in out["message"], out


def test_without_the_resulting_values_the_stored_page_alone_judges_nothing():
    assert pp.evaluate_capability(_policy(dict(_CAP)), "a", "update", "x/y.md",
                                  changed_fields=["summary"],
                                  stored={"status": "retired"}) is None
