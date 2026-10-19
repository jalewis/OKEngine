"""okengine#1032 [6]: `validate_capability(..., caller=True)` accepts a well-formed `identity_edit` grant
on the capability a networked extension's token presents, and refuses a malformed one. A policy
document's capability never carries one. The end-to-end path through the write server's real
capability gate is tests/test_extension_identity_grant_1032.py."""
from __future__ import annotations

import pytest

from tools import policy_plane

_CAP = {"rule_id": "engine-authenticated-writer", "operations": ["update"],
        "paths": ["entities/**"], "update_fields": ["*"], "body": "allow"}

def _caller_errors(identity_edit):
    return policy_plane.validate_capability("extension:x", {**_CAP, "identity_edit": identity_edit},
                                            caller=True)


@pytest.mark.parametrize("grant", [{"fields": ["title"]},
                                   {"fields": ["type"], "retype_from": ["organization"]},
                                   {"fields": ["type", "title", "name", "aliases", "citations",
                                               "remove"]}])
def test_a_well_formed_grant_is_a_valid_caller_capability(grant):
    assert _caller_errors(grant) == []


@pytest.mark.parametrize("grant", [None, "title", ["title"], {}, {"fields": "title"},
                                   {"fields": []}, {"fields": [""]}, {"fields": [1]},
                                   {"fields": ["title"], "retype_from": "organization"},
                                   {"fields": ["title"], "retype_from": [""]},
                                   {"fields": ["title"], "widen": True}])
def test_a_malformed_grant_is_an_invalid_caller_capability(grant):
    errors = _caller_errors(grant)
    assert errors and all("identity_edit" in error for error in errors), errors


def test_a_policy_document_capability_cannot_carry_a_grant():
    """Only the token store (gated by tier and acceptance, #1032 [12]) carries a grant. A composed
    policy document is not read for one, so declaring it there is an error, not a silent no-op."""
    errors = policy_plane.validate_capability("extension:x",
                                              {**_CAP, "identity_edit": {"fields": ["title"]}})
    assert errors == ["capability extension:x: unknown keys ['identity_edit']"]


def test_evaluate_capability_admits_a_granted_caller_capability():
    """The runtime gate the write path calls validates the caller's capability as a caller's."""
    policy = {"rules": [{"id": "engine-authenticated-writer", "severity": "reject"}],
              "capabilities": {"extension:x": {**_CAP, "identity_edit": {"fields": ["title"]}}}}
    assert policy_plane.evaluate_capability(policy, "extension:x", "update",
                                            "entities/a.md", "actor", ["title"]) is None
    policy["capabilities"]["extension:x"]["identity_edit"] = {"fields": []}
    result = policy_plane.evaluate_capability(policy, "extension:x", "update",
                                              "entities/a.md", "actor", ["title"])
    assert result["evidence"]["capability_errors"] == [
        "capability extension:x.identity_edit.fields: must be a non-empty list of non-empty strings"]
