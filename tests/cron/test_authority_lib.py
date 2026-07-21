import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "cron"))
import authority_lib  # noqa: E402


POLICY = {
    "id": "mitre-attck-direct/v1",
    "authority": "MITRE ATT&CK",
    "eligible_types": ["actor", "technique"],
    "source_names": ["MITRE ATT&CK"],
    "url_hosts": ["attack.mitre.org"],
    "url_path_pattern": r"/(groups/G\d{4}|techniques/T\d{4}(?:/\d{3})?)/?",
    "id_field": "attack_id",
    "id_pattern": r"(?:G|T)\d{4}(?:\.\d{3})?",
    "verified_fields": ["title", "attack_id", "aliases", "url"],
    "required_values": {"authority_import": "mitre-attack-stix"},
}


def record(**updates):
    value = {
        "type": "actor", "sources": ["MITRE ATT&CK"], "attack_id": "G0016",
        "url": "https://attack.mitre.org/groups/G0016/", "authority_import": "mitre-attack-stix",
    }
    value.update(updates)
    return value


def test_direct_authority_record_is_dispositioned_with_audit_scope():
    out = authority_lib.disposition(record(), POLICY, reviewed_at="2026-07-19T18:00:00Z")
    assert out["needs_review"] is False and out["review_state"] == "approved"
    assert out["reviewed_by"] == "policy:mitre-attck-direct/v1"
    assert out["authority_verified_fields"] == ["title", "attack_id", "aliases", "url"]


@pytest.mark.parametrize("updates,reason", [
    ({"sources": ["News Site"]}, "source identity"),
    ({"url": "https://attack.mitre.example/groups/G0016/"}, "approved host"),
    ({"url": "http://attack.mitre.org/groups/G0016/"}, "non-HTTPS"),
    ({"url": "https://attack.mitre.org/news/claim"}, "path"),
    ({"attack_id": "actor-16"}, "identifier"),
    ({"type": "source"}, "type"),
    ({"conflicts": [{"field": "aliases"}]}, "conflicts"),
    ({"authority_import": "agent-summary"}, "provenance marker"),
])
def test_authority_policy_fails_closed(updates, reason):
    result = authority_lib.evaluate(record(**updates), POLICY)
    assert result["eligible"] is False
    assert any(reason in item for item in result["reasons"])
    with pytest.raises(ValueError, match="authority disposition refused"):
        authority_lib.disposition(record(**updates), POLICY)


def test_timestamp_requires_utc_seconds():
    with pytest.raises(ValueError, match="second precision"):
        authority_lib.disposition(record(), POLICY, reviewed_at="2026-07-19")
