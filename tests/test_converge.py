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


# --- append-only `raw` backlinks (okengine#886) ---------------------------------------------------

def test_a_non_owner_adds_a_capture_to_raw_instead_of_conflicting():
    """REGRESSION: `sources` pages are engine-owned, so a pack lane's second capture was a
    conflict, the page kept one `raw`, and the selector re-offered the other capture forever."""
    prev = {"type": "source", "raw": "raw/a.json"}
    merged, dec = cv.merge_frontmatter(prev, {"raw": ["raw/a.json", "raw/b.json"]},
                                       owner_pack="engine", caller_pack="pack-x")
    assert merged["raw"] == ["raw/a.json", "raw/b.json"]
    assert dec.updated == ["raw"] and dec.conflicts == []


def test_merging_never_drops_a_capture():
    """NEGATIVE: a shorter or different incoming `raw` (even from the owner) only adds."""
    prev = {"raw": ["raw/a.json", "raw/b.json"]}
    merged, dec = cv.merge_frontmatter(prev, {"raw": "raw/c.json"},
                                       owner_pack="engine", caller_pack="engine")
    assert merged["raw"] == ["raw/a.json", "raw/b.json", "raw/c.json"]
    merged, dec = cv.merge_frontmatter(prev, {"raw": ["raw/b.json"]},
                                       owner_pack="engine", caller_pack="pack-x")
    assert merged["raw"] == prev["raw"] and dec.unchanged == ["raw"]


def test_a_single_capture_stays_scalar_and_a_repeat_is_unchanged():
    merged, dec = cv.merge_frontmatter({"raw": "raw/a.json"}, {"raw": "raw/a.json"},
                                       owner_pack="engine", caller_pack="pack-x")
    assert merged["raw"] == "raw/a.json" and dec.unchanged == ["raw"]
    merged, dec = cv.merge_frontmatter({"type": "source"}, {"raw": "raw/a.json"})
    assert merged["raw"] == "raw/a.json" and dec.added == ["raw"]


def test_an_empty_raw_is_replaced_by_the_capture():
    merged, dec = cv.merge_frontmatter({"raw": ""}, {"raw": "raw/a.json"},
                                       owner_pack="engine", caller_pack="pack-x")
    assert merged["raw"] == "raw/a.json" and dec.updated == ["raw"]


def test_other_owned_fields_still_conflict_for_a_non_owner():
    """The exemption is `raw` only: page+field ownership is unchanged for everything else."""
    merged, dec = cv.merge_frontmatter({"title": "A", "raw": "raw/a"},
                                       {"title": "B", "raw": ["raw/a", "raw/b"]},
                                       owner_pack="engine", caller_pack="pack-x")
    assert merged["title"] == "A" and dec.conflicts == [("title", "A", "B")]
    assert merged["raw"] == ["raw/a", "raw/b"]


def test_an_empty_raw_merged_with_nothing_stays_as_it_was():
    merged, dec = cv.merge_frontmatter({"raw": ""}, {"raw": ""},
                                       owner_pack="engine", caller_pack="pack-x")
    assert merged["raw"] == "" and dec.unchanged == ["raw"] and dec.updated == []


def test_keys_after_raw_or_a_server_key_are_still_merged():
    """Order must not matter: a `raw` or server key listed FIRST must not end the merge early
    and silently drop every later field."""
    merged, dec = cv.merge_frontmatter(
        {"raw": "raw/a", "title": "A"},
        {"raw": ["raw/a", "raw/b"], "title": "B", "tier": "1"},
        owner_pack="engine", caller_pack="engine")
    assert merged["title"] == "B" and merged["tier"] == "1"
    merged, dec = cv.merge_frontmatter({"title": "A"}, {"id": "x:1", "title": "B"},
                                       owner_pack="engine", caller_pack="engine")
    assert merged["title"] == "B" and dec.updated == ["title"]


def test_a_scalar_maintained_by_keeps_its_first_maintainer():
    merged, _ = cv.merge_frontmatter({"maintained_by": "pack-a"}, {"title": "x"},
                                     caller_pack="pack-b")
    assert merged["maintained_by"] == ["pack-a", "pack-b"]


def test_ownership_compares_names_by_value_never_by_order_or_identity():
    """Pack names arrive from YAML and env at runtime, never as interned literals; and a caller
    whose name merely sorts before the owner's is not the owner."""
    owner = "".join(["pack-", "zeta"])
    merged, dec = cv.merge_frontmatter({"title": "A"}, {"title": "B"},
                                       owner_pack=owner, caller_pack="pack-zeta")
    assert merged["title"] == "B", "the owner, compared by value"
    merged, dec = cv.merge_frontmatter({"title": "A"}, {"title": "B"},
                                       owner_pack="pack-zeta", caller_pack="pack-alpha")
    assert merged["title"] == "A" and dec.conflicts, "a lexically smaller name is not the owner"
    grant = {"title": "".join(["pack-", "alpha"])}
    merged, dec = cv.merge_frontmatter({"title": "A"}, {"title": "B"}, owner_pack="pack-zeta",
                                       caller_pack="pack-alpha", field_owners=grant)
    assert merged["title"] == "B", "a field grant, compared by value"


def test_an_equal_value_that_is_a_different_object_is_unchanged_not_a_conflict():
    merged, dec = cv.merge_frontmatter({"tags": ["a", "b"]}, {"tags": ["a", "b"]},
                                       owner_pack="engine", caller_pack="pack-x")
    assert dec.unchanged == ["tags"] and dec.conflicts == []


def test_a_skipped_removal_does_not_stop_later_removals():
    """A server key or an absent field in `remove` is skipped; the owner's later removals still
    happen."""
    merged, dec = cv.merge_frontmatter({"id": "x:1", "tier": "1", "title": "A"}, {},
                                       owner_pack="engine", caller_pack="engine",
                                       remove=["id", "absent", "tier"])
    assert "tier" not in merged and merged["id"] == "x:1" and dec.removed == ["tier"]


def test_additive_payload_only_adds():
    """entity-backfill's converge into an existing page (okcti, 2026-09-30)."""
    from converge import ADDITIVE_ACTORS, additive_payload
    prev = {"id": "a:1", "type": "entity", "sources": ["MITRE ATT&CK", "MISP"],
            "description": "state group", "tags": []}
    incoming = {"id": "a:1", "type": "entity", "sources": ["sources/x", "MISP"],
                "description": "cybercriminal", "tags": ["crypto"], "new_key": 1}
    assert additive_payload(prev, incoming) == {
        "sources": ["MITRE ATT&CK", "MISP", "sources/x"],
        "tags": ["crypto"], "new_key": 1}
    assert additive_payload(prev, {"sources": "sources/x"}) == {}, \
        "a scalar never replaces an existing list"
    assert "cron:entity-backfill" in ADDITIVE_ACTORS
    assert "cron:page-quality-enrich" not in ADDITIVE_ACTORS
    from converge import additive_gate
    assert additive_gate("cron:entity-backfill", prev, incoming, "description,tags")[1:3] == ([], True)
    assert additive_gate("cron:page-quality-enrich", prev, incoming, " description , tags ") == (
        incoming, ["description", "tags"], False, []), "other callers keep replace + remove"


def test_additive_gate_covers_composed_actor_names_and_concept_backfill():
    """Re-verify of okengine!1210: a composed deployment names the lane `entity-backfill@<pack>`
    (cron_pack_split.merge_packs), so the bound actor is `cron:entity-backfill@<pack>`."""
    from converge import additive_gate
    prev = {"type": "entity", "description": "kept"}
    for actor in ("cron:entity-backfill", "cron:entity-backfill@okpack-a",
                  "cron:concept-backfill", "cron:concept-backfill@okpack-b"):
        assert additive_gate(actor, prev, {"description": "new"}, "description")[1:3] == ([], True), actor
    assert additive_gate("cron:page-quality-enrich@okpack-a", prev, {"description": "new"})[2] is False


def test_additive_payload_never_retypes_but_fills_a_missing_type():
    from converge import additive_payload
    assert additive_payload({"type": "entity"}, {"type": "malware", "id": "x"}) == {}
    assert additive_payload({}, {"type": "malware"}) == {"type": "malware"}


def test_a_list_never_turns_a_held_scalar_into_a_list():
    """Re-verify round 4: unioning onto a scalar added a second attribution to `suspected_origin`."""
    from converge import additive_payload
    assert additive_payload({"suspected_origin": "North Korea"}, {"suspected_origin": ["China"]}) == {}
    assert additive_payload({"tags": ["a"]}, {"tags": ["b"]}) == {"tags": ["a", "b"]}


def test_additive_gate_normalizes_both_sides_and_never_raises():
    from converge import additive_gate
    canon = lambda d: {("suspected_origin" if k in ("country", "origin") else k): v for k, v in d.items()}
    payload, _, additive, _dropped = additive_gate("cron:entity-backfill", {"origin": "Russia"}, {"country": "CN"},
                                         "", normalize=canon)
    assert additive and payload == {}, "the canonical field is already held under another alias"

    def boom(_d):
        raise TypeError("cannot use 'dict' as a dict key")
    assert additive_gate("cron:entity-backfill", {"a": 1}, {"b": 2}, "", normalize=boom)[0] == {}, \
        "fail closed: nothing is added when normalization fails"
    assert additive_gate("cron:page-quality-enrich", {"a": 1}, {"a": 2}, "a", normalize=boom) == (
        {"a": 2}, ["a"], False, []), "a non-additive caller never invokes the normalizer"


def test_additive_gate_fails_closed_when_normalization_raises():
    """Third re-verify round: a lane-supplied unhashable value made normalization raise, and the raw
    fallback let another alias of a held field through. An error now means: add nothing."""
    from converge import additive_gate

    def boom(_d):
        raise TypeError("cannot use 'dict' as a dict key")
    payload, rm, additive, _dropped = additive_gate("cron:entity-backfill", {"origin": "Russia", "id": "a"},
                                          {"id": "a", "country": "CN", "status": {"x": 1}}, "", normalize=boom)
    assert additive and rm == [] and payload == {}



def test_additive_payload_never_adds_identity_or_classification_keys():
    from converge import additive_payload
    prev = {"name": "Lazarus Group"}
    assert additive_payload(prev, {"title": "Hidden Cobra", "link_stub": True, "redirect_to": "x",
                                   "tombstoned": True, "status": "x", "tags": ["a"]}) == {"tags": ["a"]}
    assert additive_payload({"description": "d"}, {"title": "T"}) == {"title": "T"}, \
        "a page with neither title nor name may still gain one"


def test_stub_upgrade_fills_empty_bodies_and_lets_the_fuller_source_capture_win():
    from converge import stub_upgrade
    src = {"type": "source"}
    assert stub_upgrade("", "anything") and stub_upgrade(" \n", "x")
    assert not stub_upgrade("", "  "), "whitespace never fills a page"
    assert stub_upgrade("short", "x" * 6, src) and stub_upgrade("y" * 80, "x" * 80, src)
    assert not stub_upgrade("y" * 500, "x" * 499, src), "a snippet never overwrites a fuller extract"
    assert not stub_upgrade("North Korea per MITRE G0032.", "x" * 500, {"type": "entity"}), \
        "a short curated entity body is never replaced"



def test_additive_gate_reports_dropped_disagreements_for_the_identity_guard():
    """Narrow-round re-verify: the additive filter silently dropped a differing `name`, so the
    okengine#901 identity guard never saw that the write describes a different document."""
    from converge import additive_gate
    payload, _, additive, dropped = additive_gate(
        "cron:entity-backfill", {"name": "Lazarus Group", "description": "d"},
        {"name": "Andariel", "description": "d", "tags": ["x"]})
    assert additive and payload == {"tags": ["x"]}
    assert dropped == [("name", "Lazarus Group", "Andariel")], "agreeing values are not conflicts"

    def boom(_d):
        raise TypeError("x")
    assert additive_gate("cron:entity-backfill", {"name": "A"}, {"name": "B"}, "", normalize=boom)[3] == [
        ("name", "A", "B")], "the fail-closed path still reports raw-key disagreements"


def test_backfill_lanes_never_add_aliases():
    """Narrow-round re-verify: unioning `APT38` into Lazarus's aliases re-routed APT38's later writes."""
    from converge import additive_payload
    assert additive_payload({"aliases": ["Hidden Cobra"]}, {"aliases": ["APT38"]}) == {}
    assert additive_payload({}, {"aliases": ["APT38"]}) == {}


def test_an_identity_disagreement_adds_nothing():
    from converge import additive_gate
    ident = lambda dropped: [k for k, _c, _a in dropped if k in ("name", "title", "url")]  # noqa: E731
    payload, _, additive, dropped = additive_gate(
        "cron:entity-backfill", {"name": "Lazarus Group"},
        {"name": "Andariel", "suspected_origin": "South Korea"}, identity=ident)
    assert additive and payload == {} and dropped == [("name", "Lazarus Group", "Andariel")]
    payload, *_ = additive_gate("cron:entity-backfill", {"name": "Lazarus Group"},
                                {"title": "lazarus group", "tags": ["x"]}, identity=ident)
    assert payload == {"tags": ["x"]}, "the same name under the other display key is not a conflict"


# --- mutation floor for the v0.15.1 additive rule (pipeline 11751 survivors) ---------------------

def test_empty_recognises_every_empty_shape_and_nothing_else():
    from converge import _empty
    for value in (None, "", [], {}):
        assert _empty(value) is True, repr(value)
    for value in ("x", [0], {"k": 1}, 0, False):
        assert _empty(value) is False, repr(value)


def test_only_the_exact_id_key_is_withheld():
    from converge import additive_payload
    dynamic_id = "".join(["i", "d"])                   # equal to "id" but a distinct object
    assert additive_payload({}, {dynamic_id: "x", "description": "d", "aaa": 1}) == {
        "description": "d", "aaa": 1}, "keys sorting before 'id' still pass; only `id` is withheld"


def test_a_display_disagreement_records_the_held_name_and_ignores_an_empty_value():
    from converge import additive_payload
    dropped = []
    assert additive_payload({"name": "A"}, {"title": "Z"}, dropped) == {}
    assert dropped == [("title", "A", "Z")], "the held name stands in when the key itself is empty"
    dropped = []
    additive_payload({"name": "A"}, {"title": ""}, dropped)
    assert dropped == [], "an empty incoming display value is not a disagreement"
    dropped = []
    additive_payload({"name": "A", "title": "T"}, {"title": "a"}, dropped)
    assert dropped == [], "a value matching any held display name or alias agrees"


def test_append_only_and_empty_held_keys_pass_through():
    from converge import additive_payload
    assert additive_payload({"raw": "raw/a.md"}, {"raw": ["raw/a.md", "raw/b.md"]}) == {
        "raw": ["raw/a.md", "raw/b.md"]}, "`raw` passes even onto a scalar (merge unions it)"
    assert additive_payload({"description": ""}, {"description": "x"}) == {"description": "x"}, \
        "a key the page holds EMPTY is filled"


def test_every_disagreement_is_recorded_in_both_orders_and_agreement_by_value_is_not():
    from converge import additive_payload
    for current, value in (("b", "a"), ("a", "b")):
        dropped = []
        additive_payload({"description": current}, {"description": value}, dropped)
        assert dropped == [("description", current, value)], (current, value)
    dropped = []
    additive_payload({"meta": {"k": 1}}, {"meta": {"k": 1}}, dropped)   # equal, distinct objects
    assert dropped == [], "equal values are agreement, whatever their identity"


def test_962_create_redirect_marks_only_genuine_creates():
    from converge import additive_gate, create_redirect
    with create_redirect(True):
        assert additive_gate("cron:broken-wikilinks-drain", {"d": "x"}, {"d": "y"}, "d")[2] is True
    with create_redirect(False):
        assert additive_gate("cron:broken-wikilinks-drain", {"d": "x"}, {"d": "y"}, "d")[2] is False
    assert additive_gate("cron:broken-wikilinks-drain", {"d": "x"}, {"d": "y"}, "d")[2] is False


def test_962_list_valued_identity_fields_are_compared_not_unioned():
    from converge import additive_gate
    ident = lambda dropped: [k for k, _c, _a in dropped if k == "url"]  # noqa: E731
    payload, _, _, dropped = additive_gate("cron:entity-backfill", {"url": ["https://a"]},
                                           {"url": ["https://b"], "first_seen": "2015"}, identity=ident)
    assert payload == {} and ("url", ["https://a"], ["https://b"]) in dropped


def test_962_aliases_need_the_hijack_check():
    from converge import additive_payload
    ok = lambda a: a != "APT38"  # noqa: E731
    assert additive_payload({"aliases": ["Hidden Cobra"]}, {"aliases": ["APT38", "Guardians"]},
                            alias_ok=ok) == {"aliases": ["Hidden Cobra", "Guardians"]}
    assert additive_payload({"aliases": ["Hidden Cobra"]}, {"aliases": ["Guardians"]}) == {}, \
        "no predicate: fail closed"
    assert additive_payload({"aliases": "A, B"}, {"aliases": ["b", "C"]}, alias_ok=lambda a: True) == {
        "aliases": ["A", "B", "C"]}, \
        "a legacy comma string is stored as the parts the identity index reads (okengine#1028)"


def test_962_an_identity_conflict_still_records_the_capture_backlink():
    from converge import additive_gate
    ident = lambda dropped: [k for k, _c, _a in dropped if k in ("name", "title")]  # noqa: E731
    assert additive_gate("cron:raw-backfill", {"title": "A", "raw": "raw/a.md"},
                         {"title": "B", "raw": ["raw/a.md", "raw/b.md"], "x": 1}, identity=ident)[2] is False, \
        "outside a create redirect raw-backfill is not additive"
    from converge import create_redirect
    with create_redirect():
        payload, *_ = additive_gate("cron:raw-backfill", {"title": "A", "raw": "raw/a.md"},
                                    {"title": "B", "raw": ["raw/a.md", "raw/b.md"], "x": 1}, identity=ident)
    assert payload == {"raw": ["raw/a.md", "raw/b.md"]}


def test_962_record_identity_is_best_effort():
    from converge import record_identity

    class Boom:
        def _add_identity(self, *_a):
            raise RuntimeError("x")
    record_identity(Boom(), "entities/a.md", {"name": "A"})          # never raises
    seen = []

    class Reg:
        def _add_identity(self, rel, fm):
            seen.append(rel)
    record_identity(Reg(), "entities/a.md", {"name": "A"})
    record_identity(Reg(), "entities/b.md", {"name": "B", "status": "tombstoned"})
    assert seen == ["entities/a.md"]


def test_962_only_a_positive_same_url_match_unlocks_the_fuller_capture_rule():
    from converge import additive_gate
    ident = lambda u: str(u or "").strip() if str(u or "").startswith("https://") else ""  # noqa: E731
    src = {"type": "source", "url": "https://x.test/a", "title": "A"}
    assert additive_gate("cron:raw-backfill@p", src, {"url": "https://x.test/a"}, url_identity=ident)[2] is False
    from converge import create_redirect
    with create_redirect():
        assert additive_gate("x", src, {"url": "https://x.test/a"}, url_identity=ident)[2] == "same-url"
        assert additive_gate("x", src, {"id": "sources:url-0"}, url_identity=ident)[2] is True
        assert additive_gate("x", {**src, "url": "UNKNOWN"}, {"url": "UNKNOWN"}, url_identity=ident)[2] is True


def _fresh(text):
    """The same characters as `text` in a NEW str object (as a YAML parse produces), so an `is`
    comparison against an interned literal cannot pass by accident."""
    return "".join(list(text))


def test_962_record_identity_skips_only_tombstoned_pages():
    from converge import record_identity
    seen = []
    reg = type("Reg", (), {"_add_identity": lambda self, rel, fm: seen.append(rel)})()
    record_identity(reg, "entities/a", {"status": "active"})
    record_identity(reg, "entities/w", {"status": "withdrawn"})   # sorts after "tombstoned"
    record_identity(reg, "entities/t", {"status": "Tombstoned"})
    assert seen == ["entities/a", "entities/w"]


def test_962_only_a_source_page_takes_a_fuller_body():
    from converge import stub_upgrade
    assert stub_upgrade("short", "a much longer body", {"type": "topic"}) is False   # sorts after "source"
    assert stub_upgrade("short", "a much longer body", {"type": "entity"}) is False  # sorts before it
    assert stub_upgrade("short", "a much longer body", {"type": _fresh("source")}) is True


def test_962_same_url_needs_a_source_page_and_an_equal_url():
    from converge import additive_gate, create_redirect
    ident = lambda u: _fresh(str(u or "")) if str(u or "").startswith("https://") else ""  # noqa: E731
    with create_redirect():
        for kind in ("entity", "topic"):
            prev = {"type": kind, "url": "https://x.test/a"}
            assert additive_gate("x", prev, {"url": "https://x.test/a"}, url_identity=ident)[2] is True, kind
        prev = {"type": _fresh("source"), "url": "https://x.test/a"}
        assert additive_gate("x", prev, {"url": "https://x.test/a"}, url_identity=ident)[2] == "same-url"
        assert additive_gate("x", prev, {"url": "https://x.test/b"}, url_identity=ident)[2] is True
        prev_b = {"type": "source", "url": "https://x.test/b"}
        assert additive_gate("x", prev_b, {"url": "https://x.test/a"}, url_identity=ident)[2] is True


def test_962_a_parsed_aliases_key_still_gets_the_hijack_check():
    from converge import additive_payload
    incoming = {_fresh("aliases"): ["APT38", "Guardians"]}
    assert additive_payload({"aliases": ["Hidden Cobra"]}, incoming, alias_ok=lambda a: a != "APT38") == {
        "aliases": ["Hidden Cobra", "Guardians"]}


def test_962_an_incoming_comma_string_of_aliases_is_split_and_stripped():
    from converge import additive_payload
    assert additive_payload({"aliases": ["Hidden Cobra"]}, {"aliases": "Andariel, Guardians"},
                            alias_ok=lambda a: True) == {"aliases": ["Hidden Cobra", "Andariel", "Guardians"]}


def test_962_fields_after_aliases_are_still_added():
    from converge import additive_payload
    out = additive_payload({"name": "Lazarus Group"}, {"aliases": ["Andariel"], "first_seen": "2009"},
                           alias_ok=lambda a: True)
    assert out == {"aliases": ["Andariel"], "first_seen": "2009"}


def test_962_a_pack_suffixed_backfill_actor_is_still_additive():
    from converge import additive_gate
    assert additive_gate("cron:entity-backfill@okpack-x", {"name": "A"}, {"name": "B"})[2] is True
    assert additive_gate("cron:entity-backfill", {"name": "A"}, {"name": "B"})[2] is True


# --- okengine#1028: a merge never drops a citation --------------------------------------------


def test_a_shorter_citation_list_keeps_every_stored_citation_for_the_owner():
    prev = {"type": "actor", "sources": ["sources/a", "[[sources/b|B]]"], "basis": ["concepts/x"]}
    merged, dec = cv.merge_frontmatter(prev, {"sources": ["sources/b.md", "sources/c"],
                                              "basis": ["concepts/y"]},
                                       owner_pack="atk", caller_pack="atk")
    assert merged["sources"] == ["sources/a", "[[sources/b|B]]", "sources/c"]
    assert merged["basis"] == ["concepts/x", "concepts/y"]
    assert dec.updated == ["sources", "basis"] and not dec.conflicts


def test_a_non_owner_may_add_a_citation_but_the_list_is_never_replaced():
    prev = {"type": "actor", "sources": ["sources/a"]}
    merged, dec = cv.merge_frontmatter(prev, {"sources": "sources/c"},
                                       owner_pack="atk", caller_pack="hunt")
    assert merged["sources"] == ["sources/a", "sources/c"] and not dec.conflicts


def test_a_citation_list_with_nothing_new_is_unchanged():
    prev = {"sources": ["sources/a", "sources/b"]}
    merged, dec = cv.merge_frontmatter(prev, {"sources": ["[[sources/a]]"]},
                                       owner_pack="atk", caller_pack="atk")
    assert merged["sources"] == ["sources/a", "sources/b"] and dec.unchanged == ["sources"]


def test_a_repeated_new_citation_is_added_once():
    merged, _ = cv.merge_frontmatter({"sources": ["sources/a"]},
                                     {"sources": ["sources/c", "[[sources/c]]", "sources/c#x"]},
                                     owner_pack="atk", caller_pack="atk")
    assert merged["sources"] == ["sources/a", "sources/c"]


def test_a_scalar_legacy_citation_field_keeps_ordinary_ownership():
    prev = {"sources": "sources/a"}       # a legacy scalar is judged as before; the guard sees drops
    merged, dec = cv.merge_frontmatter(prev, {"sources": ["sources/c"]},
                                       owner_pack="atk", caller_pack="hunt")
    assert merged["sources"] == "sources/a" and dec.conflicts


def test_an_explicit_citation_removal_still_reaches_the_owner_removal_path():
    merged, dec = cv.merge_frontmatter({"sources": ["sources/a"]}, {}, owner_pack="atk",
                                       caller_pack="atk", remove=["sources"])
    assert "sources" not in merged and dec.removed == ["sources"]   # the write path's guard holds it


def test_only_a_fully_bracketed_citation_is_unwrapped():
    assert cv._citation_target("[[sources/x|X]]") == "sources/x"
    assert cv._citation_target("[[sources/x") == "[[sources/x"
    assert cv._citation_target("sources/x]]") == "sources/x]]"
