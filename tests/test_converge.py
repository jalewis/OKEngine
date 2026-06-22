"""P2 regression: page+field-scoped merge arbitration (RFC §5a)."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "okengine-mcp" / "converge.py"


def _load():
    spec = importlib.util.spec_from_file_location("converge", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["converge"] = m
    spec.loader.exec_module(m)
    return m


cv = _load()


def test_new_keys_added_and_same_value_noop():
    prev = {"type": "attack-pattern", "tactic": "execution"}
    inc = {"tactic": "execution", "detection": "edr-rule"}
    merged, dec = cv.merge_frontmatter(prev, inc, owner_pack="atk", caller_pack="atk")
    assert merged["detection"] == "edr-rule"
    assert dec.added == ["detection"] and dec.unchanged == ["tactic"]


def test_owner_may_change_any_field():
    prev = {"type": "entity", "name": "Acme", "tier": "1"}
    merged, dec = cv.merge_frontmatter(prev, {"tier": "2"}, owner_pack="ent", caller_pack="ent")
    assert merged["tier"] == "2" and dec.updated == ["tier"]
    assert not dec.has_conflicts


def test_nonowner_may_add_but_not_change_unowned_field():
    prev = {"type": "attack-pattern", "tactic": "execution"}
    # hunt pack (non-owner) adds `detection` (fine) but tries to change `tactic` (not its field)
    inc = {"detection": "sigma-rule", "tactic": "defense-evasion"}
    merged, dec = cv.merge_frontmatter(prev, inc, owner_pack="atk", caller_pack="hunt",
                                       field_owners={"detection": "hunt"})
    assert merged["detection"] == "sigma-rule"           # added (attributed)
    assert merged["tactic"] == "execution"               # unchanged — conflict, not clobbered
    assert dec.added == ["detection"]
    assert dec.conflicts == [("tactic", "execution", "defense-evasion")]
    assert dec.has_conflicts


def test_nonowner_may_change_its_granted_field():
    prev = {"type": "attack-pattern", "detection": "v1"}
    merged, dec = cv.merge_frontmatter(prev, {"detection": "v2"}, owner_pack="atk",
                                       caller_pack="hunt", field_owners={"detection": "hunt"})
    assert merged["detection"] == "v2" and dec.updated == ["detection"]


def test_provenance_unions_caller():
    prev = {"type": "entity", "maintained_by": ["a"]}
    merged, _ = cv.merge_frontmatter(prev, {"x": 1}, owner_pack="a", caller_pack="b")
    assert merged["maintained_by"] == ["a", "b"]
    # idempotent — re-merging the same caller doesn't duplicate
    merged2, _ = cv.merge_frontmatter(merged, {"x": 1}, owner_pack="a", caller_pack="b")
    assert merged2["maintained_by"] == ["a", "b"]


def test_server_keys_pass_through_without_conflict():
    prev = {"type": "entity", "id": "entities:acme", "version": 1}
    merged, dec = cv.merge_frontmatter(prev, {"id": "entities:acme", "version": 2},
                                       owner_pack="x", caller_pack="other")
    assert merged["version"] == 2 and not dec.has_conflicts   # server-managed, not a pack clash


def test_owner_authorized_removal():
    prev = {"type": "attack-pattern", "tactic": "execution", "stale": "x", "detection": "v1"}
    # owner removes a field
    merged, dec = cv.merge_frontmatter(prev, {}, owner_pack="atk", caller_pack="atk",
                                       remove=["stale"])
    assert "stale" not in merged and dec.removed == ["stale"]
    # non-owner removing an unowned field is a conflict (kept)
    merged, dec = cv.merge_frontmatter(prev, {}, owner_pack="atk", caller_pack="hunt",
                                       field_owners={"detection": "hunt"}, remove=["tactic"])
    assert merged["tactic"] == "execution" and dec.has_conflicts and not dec.removed
    # non-owner removing a field it OWNS is allowed
    merged, dec = cv.merge_frontmatter(prev, {}, owner_pack="atk", caller_pack="hunt",
                                       field_owners={"detection": "hunt"}, remove=["detection"])
    assert "detection" not in merged and dec.removed == ["detection"]
    # server keys are never removed
    merged, dec = cv.merge_frontmatter({"type": "x", "id": "x:y"}, {}, owner_pack=None,
                                       remove=["id"])
    assert merged["id"] == "x:y" and not dec.removed


def test_no_owner_means_no_enforcement_backcompat():
    # single-pack / no composition: a change applies (today's behaviour)
    prev = {"type": "entity", "tier": "1"}
    merged, dec = cv.merge_frontmatter(prev, {"tier": "2"})
    assert merged["tier"] == "2" and dec.updated == ["tier"] and not dec.has_conflicts
    assert "maintained_by" not in merged                      # no caller -> no provenance
