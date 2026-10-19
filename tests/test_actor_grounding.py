"""Unit contract for ``okengine.actor_grounding`` (okengine#1012).

The predicate decides whether a cited source names a subject AND, in its own words, affirms it
as an adversary. The fixtures quote the kinds of sentences the predicate met on a live CTI vault:
the vendor that reported a phishkit, the hosting provider attackers rented, the campaign "dubbed"
a name, the ransomware group that claimed an attack.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okengine import actor_grounding as g
from okengine.actor_grounding import GroundingFailure, SourceDocument


def _doc(body: str, title: str = "", *, curated: bool = False, ref: str = "sources/r") -> SourceDocument:
    return SourceDocument(ref=ref, title=title, body=body, curated=curated)


def _verdict(names, *bodies, **kwargs):
    return g.ground_actor_identity(names, [_doc(body) for body in bodies], **kwargs)


# --- what affirms an adversary ---------------------------------------------------------------------

@pytest.mark.parametrize("name, body", [
    ("LockBit", "LockBit claimed the attack on its leak site."),
    ("Scattered Spider", "Scattered Spider targeted retail help desks with voice phishing."),
    ("ShinyHunters", "ShinyHunters is a cybercriminal group associated with data theft."),
    ("Kimsuky", "A threat actor tracked as Kimsuky targets research organizations."),
    ("TA488", "Russia-aligned TA488 is exploiting an Outlook Web Access flaw."),
    ("Smishing Triad", "A large SMS phishing campaign linked to the Smishing Triad uses a kit."),
    ("Cl0p", "Cleo Vulnerabilities Targeted by Cl0p Ransomware."),
    ("Vect", "The Vect ransomware group formed a partnership."),
    ("Tren de Aragua", "Treasury sanctioned members of the Venezuelan Tren de Aragua gang."),
    ("Yanluowang", "Ransomware groups like Monti, Cuba and Yanluowang hit businesses."),
    ("TeamPCP", "Ransomware groups Vect and TeamPCP have formed a partnership."),
    ("Azazel", "Researchers found an operator calling himself Azazel."),
    ("Milk Dragon", "They uncovered a scam campaign run by a group they are calling Milk Dragon."),
    ("Connor Moucka", "Connor Moucka pleaded guilty to computer fraud."),
    ("Cephalus", "The breach was linked to a threat actor named [[entities/c/e/cephalus Cephalus]]."),
    ("Cl0p", "## Attribution\n\n- Reported actor: Cl0p\n"),
    ("Gentlemen", "A Gentlemen affiliate has also exploited exposed servers, then Gentlemen "
                  "has encrypted hosts at the firm."),
])
def test_a_source_that_affirms_the_adversary_grounds_it(name, body):
    assert _verdict([name], body) is None, (name, body)


@pytest.mark.parametrize("name, body, reason", [
    ("ANY.RUN", "ANY.RUN has identified Wazza, a new phishkit targeting banks.",
     "reporting organization"),
    ("Omegatech", "The emergence of Omegatech, a paid hosting provider based in the Seychelles.",
     "hosting provider"),
    ("Canto Incognito", "The financially motivated campaign, dubbed Canto Incognito, mines coins.",
     "campaign"),
    ("Iran-nexus", "Analysis of Blinder Tunnel, an Iran-nexus campaign using recruitment lures.",
     "campaign"),
    ("TerminalFix", "A new ClickFix variant, dubbed TerminalFix, tricks users.", "variant"),
    ("Talos", "Researchers at Talos tracked the botnet.", "researcher"),
    ("npm", "Attackers poisoned npm packages used by developers.", "packages"),
])
def test_a_source_that_calls_the_subject_something_else_names_that_kind(name, body, reason):
    failure = _verdict([name], body)
    assert failure is not None and failure.code == g.SOURCE_DEFINES_NON_ACTOR, failure
    assert reason in failure.detail, failure


@pytest.mark.parametrize("name, body", [
    # a victim in a headline, or after a copula, is never the agent
    ("Okta", "Okta Breached in a support-system intrusion."),
    ("Siinqee Bank", "Siinqee Bank has been targeted by a ransomware group."),
    ("Acme", "Acme hacked by unknown attackers last week."),
    # a name used as a modifier after an article is not the actor
    ("China-nexus", "A new China-nexus cyber espionage group known as TA419 phished experts."),
    # hedged and negated claims do not affirm
    ("Northwind", "It is not clear whether Northwind targeted the bank."),
    ("Contoso", "Contoso never claimed the attack."),
    # a group noun that names an ordinary organization needs an adversarial cue
    ("Volkswagen", "The Volkswagen Group reported a breach."),
    # a list heading without an adversarial cue
    ("Microsoft", "Industry groups like Microsoft and Google joined the alliance."),
    # a hedged name-then-kind, and a list the name is not in
    ("Vect", "It is not known whether the Vect ransomware group was involved."),
    ("Acme", "Ransomware groups like Monti and Cuba hit Acme hospitals."),
    # a lowercase occurrence of a capitalized name is a common word, not the subject
    ("Attack", "The attack targeted hospitals in the region."),
])
def test_text_that_does_not_affirm_an_adversary_does_not_ground_it(name, body):
    failure = _verdict([name], body)
    assert failure is not None, (name, body)


def test_a_name_only_in_the_headline_is_a_headline_word():
    """"Cryptominers Exploiting WebLogic RCE": every headline word is capitalized."""
    failure = g.ground_actor_identity(
        ["Cryptominers"], [_doc("Attackers took advantage of a WebLogic RCE.",
                                title="Cryptominers Exploiting WebLogic RCE")])
    assert failure is not None and failure.code == g.NOT_AFFIRMED


def test_a_headline_counts_when_the_body_repeats_the_name():
    failure = g.ground_actor_identity(
        ["LockBit 5.0"], [_doc("The LockBit 5.0 operators posted the bank.",
                               title="LockBit 5.0 Targets Ethiopian Banking Leader")])
    assert failure is None


# --- refusal reasons ---------------------------------------------------------------------------------

def test_a_page_with_no_usable_name_is_refused():
    assert _verdict(["---", ""], "x").code == g.NAME_ABSENT
    assert _verdict([], "x").code == g.NAME_ABSENT


def test_no_source_at_all_is_unsourced():
    assert g.ground_actor_identity(["APT Zero"], []) == GroundingFailure(
        g.UNSOURCED, "the page cites no source")


def test_unresolved_references_and_labels_are_not_evidence_for_a_model():
    failure = g.ground_actor_identity(["APT Zero"], [], unresolved=["sources/x"],
                                      labels=["MITRE ATT&CK"])
    assert failure.code == g.NO_RESOLVABLE_SOURCE
    assert "sources/x" in failure.detail and "MITRE ATT&CK" in failure.detail


def test_a_label_vouches_only_when_the_caller_is_the_curated_importer():
    assert g.ground_actor_identity(["APT Zero"], [], labels=["MISP galaxy"],
                                   labels_vouch=True) is None


def test_a_name_no_source_mentions_is_absent():
    failure = _verdict(["Anonymous Dog"], "Researchers found 16 malicious Firefox extensions.")
    assert failure.code == g.NAME_ABSENT and "Anonymous Dog" in failure.detail


def test_a_named_but_unaffirmed_subject_is_not_affirmed():
    failure = _verdict(["Pink"], "Pink predominantly registers its domains through a registrar.")
    assert failure.code == g.NOT_AFFIRMED
    assert str(failure) == f"{g.NOT_AFFIRMED}: {failure.detail}"


def test_a_failure_without_detail_prints_its_code():
    assert str(GroundingFailure("x")) == "x"


def test_an_alias_can_carry_the_grounding():
    assert _verdict(["Laundry Bear", "TA488"], "TA488 abuses a Zimbra flaw.") is None


# --- a byline is not an adversary --------------------------------------------------------------------

def test_a_name_that_is_its_own_publisher_needs_an_affirming_source():
    failure = g.ground_actor_identity(["npm"], [], publisher="npm", labels=["x"],
                                      labels_vouch=True)
    assert failure.code == g.NAME_EQUALS_PUBLISHER


def test_a_curated_record_cannot_vouch_for_a_byline():
    record = _doc("No description.", title="Npm — actor record", curated=True)
    failure = g.ground_actor_identity(["npm"], [record], publisher="NPM")
    assert failure.code == g.NAME_EQUALS_PUBLISHER


def test_a_byline_the_source_affirms_as_an_adversary_is_grounded():
    assert _verdict(["Lapsus"], "Lapsus claimed the breach.", publisher="Lapsus") is None


def test_a_non_string_publisher_is_no_byline():
    assert not g.bylined(["npm"], ["npm"])
    assert g.bylined(["npm"], "NPM")


# --- curated records ---------------------------------------------------------------------------------

def test_a_curated_actor_record_vouches_for_its_subject():
    record = _doc("No description supplied.", title="Ghost Jackal — actor record", curated=True)
    assert g.ground_actor_identity(["Ghost Jackal"], [record]) is None


@pytest.mark.parametrize("title", [
    "CARROTBALL — catalog record",                 # a malware family record
    "Campaign record naming CARROTBALL",           # names it, but is not its actor record
])
def test_a_curated_record_that_is_not_the_subjects_actor_record_does_not_vouch(title):
    record = _doc("CARROTBALL is a simple FTP downloader.", title=title, curated=True)
    assert g.ground_actor_identity(["CARROTBALL"], [record]) is not None


def test_a_curated_record_titled_for_someone_else_does_not_vouch():
    record = _doc("Ghost Jackal named here.", title="Worok — actor record", curated=True)
    assert g.ground_actor_identity(["Ghost Jackal"], [record]) is not None


# --- names, references, documents --------------------------------------------------------------------

def test_actor_names_are_title_then_name_and_aliases_are_kept_apart():
    fm = {"title": "Storm-2603", "name": "storm 2603", "aliases": ["Longlegs", "", 7, "STORM 2603"],
          "alias": "Warlock Group"}
    assert g.actor_names(fm) == ["Storm-2603"]
    assert g.actor_aliases(fm) == ["Longlegs", "Warlock Group"]


def test_a_page_with_no_title_or_name_is_called_by_its_aliases_then_its_slug():
    """okengine#1020: 998 actor pages on one live vault carry no title or name. Red before: the
    guard read no name at all and refused every one as `name_absent_from_sources`."""
    fm = {"aliases": ["Lazarus Group", "", "lazarus group", "Hidden Cobra"]}
    assert g.actor_names(fm) == ["Lazarus Group", "Hidden Cobra"]
    assert g.actor_names(fm, "ignored") == ["Lazarus Group", "Hidden Cobra"]
    assert g.actor_aliases(fm) == [], "aliases standing in for the name are not ALSO aliases"
    assert g.actor_names({"alias": "Night Crew"}) == ["Night Crew"]
    assert g.actor_names({}, "lazarus-group_2") == ["lazarus group 2"]
    assert g.actor_names({"title": "", "aliases": []}, "--") == []
    assert g.actor_names({}) == []
    assert g.actor_names({"name": "Real"}, "slug") == ["Real"]


def test_source_entries_split_references_from_labels():
    fm = {"sources": ["[[sources/2026/a.md]]", "wiki/sources/b", "source:x:1", "MITRE ATT&CK",
                      "https://example.org/r", "", 3]}
    refs, labels = g.source_entries(fm)
    assert refs == ["sources/2026/a", "sources/b", "source:x:1", "https://example.org/r"]
    assert labels == ["MITRE ATT&CK"]


def test_the_legacy_singular_source_field_is_read():
    assert g.source_entries({"source": "sources/a"}) == (["sources/a"], [])
    assert g.source_entries({}) == ([], [])


@pytest.mark.parametrize("fm, curated", [
    ({"evidence_role": "curated-authority"}, True),
    ({"evidence_role": "Curated-Authority"}, True),
    # not operator-owned: a model could set it with one update (QA B3)
    ({"source_kind": "reference-data"}, False),
    ({"source_kind": "news"}, False),
])
def test_source_document_marks_curated_records(fm, curated):
    assert g.source_document("sources/r", fm, "x").curated is curated


def test_source_document_drops_only_the_title_heading():
    doc = g.source_document("sources/r", {"title": "Report"}, "# Report\nLockBit claimed it.")
    assert doc.body == "LockBit claimed it." and doc.title == "Report"
    kept = g.source_document("sources/r", {"title": "Report"}, "# Other\nText")
    assert kept.body == "# Other\nText"


def test_mentions_tolerate_separators_but_not_partial_words():
    text = "ANYRUN and Any Run and ANY.RUN, never accordance."
    assert [surface for surface, _ in g.mentions("ANY.RUN", text)] == ["ANYRUN", "Any Run",
                                                                      "ANY.RUN"]
    assert g.mentions("Accord", text) == []
    assert g.mentions("---", text) == []


def test_resolve_sources_reports_what_did_not_resolve():
    docs = {"sources/a": _doc("A")}
    documents, labels, unresolved = g.resolve_sources(
        {"sources": ["sources/a", "sources/b", "Microsoft"]}, docs.get)
    assert [d.body for d in documents] == ["A"]
    assert labels == ["Microsoft"] and unresolved == ["sources/b"]


def test_ground_page_falls_back_to_the_slug_only_when_the_page_is_nameless():
    docs = {"sources/a": _doc("Night Crew claimed the intrusion.")}
    assert g.ground_page({"sources": ["sources/a"]}, docs.get, fallback_name="Night Crew") is None
    assert g.ground_page({"sources": ["sources/a"]}, docs.get).code == g.NAME_ABSENT


_APT1 = "APT1, also known as Comment Crew, is a threat group that targeted defense contractors."


def test_a_legacy_scalar_alias_list_grounds_like_the_list_it_stands_for():
    """okengine#1032 [23]: the raw-page graders (review_autoverify, corpus_audit) read stored
    frontmatter, where the aliasing import left comma-joined scalars. Read as one name,
    'APT1, Comment Crew' was never mentioned and a grounded actor stayed held forever."""
    docs = {"sources/x": _doc(_APT1)}
    as_list = {"aliases": ["APT1", "Comment Crew"], "sources": ["sources/x"]}
    as_scalar = {"aliases": "APT1, Comment Crew", "sources": ["sources/x"]}
    assert g.ground_page(as_list, docs.get) is None
    assert g.ground_page(as_scalar, docs.get) is None
    assert g.actor_names(as_scalar) == ["APT1", "Comment Crew"]
    assert g.actor_aliases({"title": "APT1", "alias": "APT1, Comment Crew"}) == ["Comment Crew"]


def test_a_legacy_scalar_source_list_resolves_every_reference():
    docs = {"sources/x": _doc(_APT1), "sources/y": _doc("APT1 struck again.")}
    refs, labels = g.source_entries({"sources": "sources/x, sources/y, MITRE ATT&CK"})
    assert refs == ["sources/x", "sources/y"] and labels == ["MITRE ATT&CK"]
    assert g.source_entries({"source": "sources/x,sources/y"}) == (["sources/x", "sources/y"], [])
    assert g.ground_page({"title": "APT1", "sources": "sources/x, sources/y"}, docs.get) is None


def test_a_list_entry_a_title_or_a_name_is_never_split_on_its_commas():
    """Only a SCALAR in a list field is a delimited list (the write path's coercion and the
    id-index agree); a list entry, a title and a name keep their commas."""
    fm = {"title": "Acme, Inc", "name": "Acme, Inc", "aliases": ["Adobe Inc., Adobe Systems"],
          "sources": ["sources/a, b"]}
    assert g.actor_names(fm) == ["Acme, Inc"]
    assert g.actor_aliases(fm) == ["Adobe Inc., Adobe Systems"]
    assert g.source_entries(fm) == (["sources/a, b"], [])
    # a scalar keeps a comma inside brackets, as the write path does
    assert g.actor_names({"aliases": "Turla (Snake, Uroburos), Venomous Bear"}) == [
        "Turla (Snake, Uroburos)", "Venomous Bear"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_wiki_sources_reads_paths_and_ids_under_sources_only(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki / "sources" / "a.md", "---\ntitle: A\nid: src:a\n---\n# A\nLockBit claimed it.\n")
    _write(wiki / "entities" / "x.md", "---\ntype: actor\n---\nbody\n")
    _write(wiki / "sources" / "bad.md", "---\n: [\n---\nbody\n")
    _write(wiki / "sources" / "list.md", "---\n- a\n---\nbody\n")
    _write(wiki / "sources" / "bare.md", "no frontmatter\n")
    read = g.WikiSources(wiki, {"src:a": "sources/a", "src:x": "entities/x"})
    assert read("sources/a").body.strip() == "LockBit claimed it."
    assert read("src:a").ref == "sources/a"
    assert read("src:a") is read("src:a"), "cached per reference"
    for ref in ("entities/x", "src:x", "sources/missing", "sources/bad", "sources/list",
                "sources/bare"):
        assert read(ref) is None, ref


def test_wiki_sources_treats_an_unreadable_page_as_unresolved(tmp_path):
    """An id that maps onto a directory (a shard) cannot be read as a page."""
    wiki = tmp_path / "wiki"
    (wiki / "sources" / "shard.md").mkdir(parents=True)
    assert g.WikiSources(wiki, {"src:shard": "sources/shard"})("src:shard") is None


# --- corpus signals ----------------------------------------------------------------------------------

@pytest.mark.parametrize("title, slug, expected", [
    ("Canadian Pleads Guilty to Snowflake Cloud Data Theft", "", True),
    ("Hackers Breach Polish Power Plant Controls", "", True),
    ("Deadlock", "deadlock-ransomware-campaign-2026", True),
    ("Smishing Triad", "smishing-triad", False),
    ("Cyber Fighters of Izz ad Din", "", True),
])
def test_headline_title(title, slug, expected):
    assert g.headline_title(title, slug) is expected


def test_nexus_label_and_reserved_titles():
    assert g.nexus_label("China-nexus")
    assert g.nexus_label("China PRC Mainland", ["China PRC Mainland"])
    assert not g.nexus_label("Volt Typhoon", ["China"])
    assert not g.nexus_label("")


@pytest.mark.parametrize("schema, expected", [
    ({"identity_admission": {"actor": {"excluded_exact_titles": ["China", " ", "Iran"]}}},
     ["China", "Iran"]),
    ({"identity_admission": {"actor": {"excluded_exact_titles": {"CN": "China"}}}}, ["China"]),
    ({"identity_admission": {"actor": {"excluded_exact_titles": "China"}}}, []),
    ({"identity_admission": {"actor": {"excluded_exact_titles": 7}}}, []),
    ({"identity_admission": "broken"}, []),
    ({}, []),
])
def test_reserved_titles_read_the_pack_declaration(schema, expected):
    assert g.reserved_titles(schema) == expected


# --- exact contracts (each pins a behavior a mutation campaign showed was unasserted) -----------

def test_documents_and_failures_are_immutable_values():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        _doc("x").body = "y"
    with pytest.raises(dataclasses.FrozenInstanceError):
        GroundingFailure("x").code = "y"
    assert SourceDocument(ref="r", title="t", body="b").curated is False


@pytest.mark.parametrize("fm", [{"evidence_role": "zzz"}, {"source_kind": "zzz"},
                                {"evidence_role": "aaa"}, {"source_kind": "aaa"}])
def test_only_the_exact_curated_markers_make_a_record_curated(fm):
    assert g.source_document("sources/r", fm, "x").curated is False


def test_the_predicates_take_their_options_by_keyword_only():
    with pytest.raises(TypeError):
        g.ground_page({}, {}.get, "Name")
    with pytest.raises(TypeError):
        g.ground_actor_identity(["Name"], [], "publisher")


def test_ground_page_does_not_let_a_label_vouch_by_default():
    assert g.ground_page({"title": "APT Zero", "sources": ["MITRE ATT&CK"]}, {}.get).code == (
        g.NO_RESOLVABLE_SOURCE)


def test_wiki_sources_will_not_read_a_page_outside_sources(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki / "topics" / "x.md", "---\ntitle: t\n---\nLockBit claimed it.\n")
    assert g.WikiSources(wiki, {"id:x": "topics/x"})("id:x") is None


def test_a_wikilink_reads_as_its_label():
    assert g._plain("[[entities/c/e/cephalus Cephalus]] and [[sources/x|Report]] and "
                    "[[sources/bare]]") == "Cephalus and Report and sources/bare"


def test_a_hedge_in_an_earlier_sentence_does_not_reach_the_claim():
    assert _verdict(["LockBit"], "It may be early. LockBit claimed the attack.") is None


def test_a_name_then_kind_reads_at_most_three_words_ahead():
    assert _verdict(["Vect"], "The Vect apt ransomware group struck.") is None
    assert _verdict(["Vect"], "The Vect ransomware apt extortion group struck.") is not None
    assert _verdict(["Vect"], "The Vect hospital group was hit.") is not None


def test_a_list_head_reads_its_cue_at_most_six_words_back():
    six = "Ransomware alpha beta gamma delta epsilon groups like Vect and TeamPCP."
    seven = "Ransomware alpha beta gamma delta epsilon zeta groups like Vect and TeamPCP."
    assert _verdict(["TeamPCP"], six) is None
    assert _verdict(["TeamPCP"], seven) is not None


def test_a_list_without_a_cue_does_not_end_the_search():
    body = "Industry groups like Microsoft joined; ransomware groups like LockBit and Zeta struck."
    assert _verdict(["Zeta"], body) is None


@pytest.mark.parametrize("name, body, grounded", [
    ("TeamPCP", "Ransomware groups Vect and TeamPCP.", True),       # the list ends on the name
    # named elsewhere, but not in the list (sorting before or after every item)
    ("Zeta", "Ransomware groups like Alpha and Beta. Zeta was mentioned.", False),
    ("Alpha", "Ransomware groups like Zeta and Omega. Alpha was mentioned.", False),
])
def test_list_membership_is_exact(name, body, grounded):
    assert (_verdict([name], body) is None) is grounded


@pytest.mark.parametrize("cue", ["ransomware", "state-sponsored", "cyber"])
def test_each_cue_family_marks_an_adversary_kind(cue):
    assert _verdict(["Zeta"], f"The Zeta {cue} group struck.") is None


def test_the_mention_window_is_three_hundred_characters_each_side():
    """The window bounds what the role checks read around ONE mention: a hedge exactly 300
    characters back is read, one 301 back is not, and a definition well past 300 characters
    ahead belongs to another mention."""
    claim = "Zeta claimed the attack."
    hedge = "not " + "w " * ((g._WINDOW - len("not ")) // 2)
    assert (hedge + claim).index("Zeta") == g._WINDOW
    assert _verdict(["Zeta"], hedge + claim) is not None
    one_further = hedge.removesuffix("w ") + "wx " + claim
    assert one_further.index("Zeta") == g._WINDOW + 1
    assert _verdict(["Zeta"], one_further) is None
    far = "Zeta, active since 2019, is a group. " + "w " * 160 + "A malware dubbed Zeta spread."
    assert _verdict(["Zeta"], far) is None


def test_failure_details_name_the_page_and_the_source():
    assert _verdict(["Anonymous Dog", "Dog"], "Nothing here.").detail == (
        "no cited source names 'Anonymous Dog'")
    assert _verdict(["Pink", "Rose"], "Pink registers domains.").detail == (
        "the cited sources name 'Pink' but none says it is an adversary")
    defined = g.ground_actor_identity(["Omegatech"], [_doc(
        "Omegatech, a paid hosting provider, rents servers.", ref="sources/omega")])
    assert defined.detail == "sources/omega describes it as a hosting provider"
    byline = g.ground_actor_identity(["npm", "Npm Inc"], [], publisher="npm")
    assert byline.detail.startswith("'npm' is the page's own publisher")
    cited = g.ground_actor_identity(["X"], [], unresolved=["a", "b", "c", "d"])
    assert cited.detail == "cited: a, b, c"


@pytest.mark.parametrize("title, slug, expected", [
    ("One Two Three Four Five", "", False),
    ("One Two Three Four Five Six Seven", "", True),
    ("Night Campaign", "night-campaign", True),
])
def test_headline_title_boundaries(title, slug, expected):
    assert g.headline_title(title, slug) is expected


@pytest.mark.parametrize("names, publisher, expected", [
    (["---"], None, False),
    (["zeta"], "alpha", False),
    (["alpha"], "zeta", False),
    (["Alpha"], "ALPHA", True),
])
def test_bylined_is_exact_name_equality(names, publisher, expected):
    assert g.bylined(names, publisher) is expected


@pytest.mark.parametrize("body, grounded", [
    ("A new big Zeta threat actor struck.", False),        # the article is three words back
    ("A big new old Zeta threat actor struck.", True),     # four back: no longer the article
    ("A Zeta threat actor was seen; the Zeta ransomware group struck.", True),
])
def test_an_indefinite_article_makes_only_its_own_mention_a_modifier(body, grounded):
    assert (_verdict(["Zeta"], body) is None) is grounded



# --- QA of !1288 ---------------------------------------------------------------------------------

def test_b1_an_alias_cannot_borrow_an_unrelated_actor_from_the_source():
    """"ANYRUN Labs" with alias Storm-2477: the source names Storm-2477 as an attacker and never
    the page's own name, so the alias is a borrowed identity."""
    body = ("ANY.RUN has identified Wazza, a new phishkit. The Storm-2477 group deployed it "
            "against banks.")
    failure = g.ground_actor_identity(["ANYRUN Labs"], [_doc(body)], aliases=["Storm-2477"])
    assert failure is not None and failure.code == g.NAME_ABSENT


@pytest.mark.parametrize("names, aliases, body", [
    # the alias shares a distinctive word with the name
    (["Scattered Spider"], ["Spider Crew"], "Spider Crew targeted retail help desks."),
    # a sentence ties the alias to the name
    (["Sofacy"], ["Fancy Bear"],
     "Sofacy, also known as Fancy Bear, is tracked. Fancy Bear targeted NATO."),
    (["Octo Tempest"], ["Scattered Spider"],
     "Octo Tempest (a.k.a. Scattered Spider) is tracked. Scattered Spider targeted help desks."),
    (["Storm-0501"], ["Sangria Tempest"], "Storm-0501, formerly Sangria Tempest, struck."),
    (["Rare Werewolf"], ["Librarian Ghouls"],
     "Rare Werewolf / Librarian Ghouls is a known threat cluster in the region."),
    (["Rare Werewolf"], ["Librarian Ghouls"],
     "Librarian Ghouls/Rare Werewolf is a known threat cluster in the region."),
])
def test_b1_an_alias_still_grounds_when_tied_to_the_name(names, aliases, body):
    assert g.ground_actor_identity(names, [_doc(body)], aliases=aliases) is None


@pytest.mark.parametrize("alias", ["Lazarus Group", "Lazarus Team"])
def test_b1_a_shared_generic_word_does_not_tie_an_alias(alias):
    body = f"The {alias} targeted banks."
    assert g.ground_actor_identity(["Hidden Group"], [_doc(body)], aliases=[alias]) is not None


@pytest.mark.parametrize("name, body", [
    ("Acme Health", "A ransomware attack hit Acme Health. Ransomware group Qilin claimed it."),
    ("Acme", "The breach hit Acme. Threat actors stole 2TB."),
    ("Acme", "Victim: Acme\nRansomware group: LockBit"),
    ("Acme", "Acme, ransomware group LockBit said."),
    ("Acme", "Acme - ransomware group LockBit said."),
    ("Acme", "Acme \u2014 ransomware group LockBit said."),
])
def test_b2_the_name_then_kind_check_stops_at_its_own_phrase(name, body):
    assert _verdict([name], body) is not None


def test_b3_reference_data_alone_does_not_make_a_curated_record():
    doc = g.source_document("sources/r", {"source_kind": "reference-data",
                                          "title": "ANY.RUN — threat actor record"},
                            "ANY.RUN has identified Wazza.")
    assert g.ground_actor_identity(["ANY.RUN"], [doc]) is not None


@pytest.mark.parametrize("body", [
    "Researchers found an operator \u201ccalled\u201d Azazel. A group known as \u201cAzazel\u201d "
    "ran it.",
    "The crew known as \u2018Azazel\u2019 ran it.",
])
def test_typographic_quotes_read_as_plain_quotes(body):
    assert _verdict(["Azazel"], body) is None


@pytest.mark.parametrize("name, body", [
    ("UNC 4221", "Bounty: Up to $10M reward for info on UNC 5792, UNC 4221."),
    ("UNC5792", "Reward offered for UNC5792 and others."),
    ("UNK_DeadDrop", "This aligns with the UNK DeadDrop pattern of fake recruitment."),
    ("TA488", "Mailbox rules were changed; see TA488."),
    ("Storm-0558", "Signing keys and Storm-0558 were discussed."),
    ("CL-STA-1114", "The cluster CL-STA-1114 overlaps."),
])
def test_a_tracker_designator_cited_in_a_source_names_a_tracked_actor(name, body):
    assert _verdict([name], body) is None


@pytest.mark.parametrize("name", ["UNCLE 42", "TAG Heuer", "APT", "Stormy 2"])
def test_a_word_that_only_starts_like_a_designator_is_not_one(name):
    assert _verdict([name], f"{name} was mentioned in passing.") is not None


def test_a_designator_named_only_in_a_headline_is_still_a_headline_word():
    failure = g.ground_actor_identity(["UNC 4221"], [_doc("A bounty was announced.",
                                                         title="Bounty on UNC 4221")])
    assert failure is not None



def test_b1_naming_the_page_does_not_lend_it_every_alias():
    """The reporter and the adversary in one report: ANY.RUN does not become Storm-2477."""
    body = ("ANY.RUN has identified Wazza, a new phishkit. The Storm-2477 group deployed it "
            "against banks.")
    failure = g.ground_actor_identity(["ANY.RUN"], [_doc(body)], aliases=["Storm-2477"])
    assert failure is not None and failure.code == g.SOURCE_DEFINES_NON_ACTOR


def test_b1_an_alias_cue_must_join_the_name_and_the_alias_in_one_sentence():
    body = "ANY.RUN reported it. Storm-2477, also known as Wazza crew, deployed it."
    assert g.ground_actor_identity(["ANY.RUN"], [_doc(body)], aliases=["Storm-2477"]) is not None


@pytest.mark.parametrize("alias", ["Intelligence Panda", "Security Bear", "Labs Crew",
                                   "Research Kitten", "Lab Spider"])
def test_b1_a_vendor_word_does_not_tie_an_alias(alias):
    body = f"{alias} targeted retail help desks."
    assert g.ground_actor_identity(["Acme Threat Intelligence Labs Security Research Lab"],
                                   [_doc(body)], aliases=[alias]) is not None


@pytest.mark.parametrize("name, body", [
    ("Acme", "| Acme | ransomware group | LockBit |"),
    ("Acme", "Attack on Acme (ransomware gang LockBit claimed)."),
])
def test_a_table_cell_or_parenthesis_ends_the_name_phrase(name, body):
    assert _verdict([name], body) is not None


@pytest.mark.parametrize("name", ["UAC 2", "DEV 2", "TA 24", "TA 2024", "UNK Labs",
                                  "unk deaddrop", "APT 2024"])
def test_a_designator_needs_three_digits_or_a_camelcase_coinage(name):
    assert _verdict([name], f"{name} was mentioned in passing.") is not None


# --- okengine#1014: predicate residuals --------------------------------------------------------------

@pytest.mark.parametrize("name, body", [
    # `no` hedges like `not`
    ("Lazarus", "There is no evidence Lazarus attacked the exchange."),
    # a present-tense claim with no object, or with a reported clause, names no adversary
    ("Acme", "Acme claims the breach was contained."),
    ("Acme", "Acme claims."),
    ("Acme", "As Acme claims, the data is safe."),
    ("Acme", "Acme claimed the outage was routine."),
    # the source itself calls the subject software, so what it does is the software's doing
    ("RedLine", "RedLine is a commodity infostealer. RedLine steals browser passwords."),
    ("Lumma", "Lumma, an infostealer sold as a service, is back. Lumma targets crypto wallets."),
    ("Lazarus", "Nothing suggests Lazarus attacked the exchange."),
    ("Cobalt Strike", "Cobalt Strike, a commercial penetration testing tool, is widely abused. "
                      "Cobalt Strike deploys beacons."),
    ("Wazza", "Researchers found a phishing kit dubbed Wazza. Wazza targets banks."),
])
def test_residual_shapes_do_not_affirm_an_adversary(name, body):
    """Red before okengine#1014: each of these was read as the subject acting as an adversary."""
    assert _verdict([name], body) is not None, body


@pytest.mark.parametrize("name, body", [
    ("Lazarus", "Lazarus attacked the exchange; no funds were recovered."),
    ("LockBit", "LockBit claims responsibility for the attack on the bank."),
    ("LockBit", "LockBit claims the attack on Acme."),
    ("LockBit", "LockBit claimed the attack on its leak site."),
    ("LockBit", "The LockBit ransomware gang claimed the attack. LockBit steals data."),
    ("Scattered Spider", "Scattered Spider targeted retail help desks using a phishing kit."),
    ("Lazarus", "Lazarus deployed a new backdoor against the exchange."),
    # QA of !1297: each grounded on main and must still
    ("Lazarus", "There is no doubt Lazarus attacked the exchange."),
    ("LockBit", "LockBit claims the attack was carried out by its affiliates."),
    ("Play", "Play claims Acme was hacked."),
    ("Ransom Busters", "Ransom Busters claimed it could decrypt the files."),
    ("Sandworm", "Researchers analyzed the Sandworm wiper. Sandworm attacked the grid."),
    ("Lazarus", "The Lazarus loader was found. Lazarus targeted the exchange."),
    ("Kimsuky", "The Kimsuky RAT spread. Kimsuky targeted researchers."),
    ("Storm-0558", "Microsoft found a new backdoor deployed by a group named Storm-0558."),
])
def test_the_matching_legitimate_sentences_still_affirm(name, body):
    assert _verdict([name], body) is None, body


@pytest.mark.parametrize("name, body", [
    ("RedLine", "RedLine steals browser passwords."),
    ("RedLine", "RedLine steals credentials from browsers."),
    ("China", "China targeted the ministry's mail servers."),
    ("Russia", "The intrusion was attributed to Russia."),
])
def test_a_name_the_deployment_holds_as_a_non_actor_is_not_grounded_by_its_actions(name, body):
    """Malware and countries are recognized from the deployment's own evidence (a non-actor page of
    that name, a pack's geopolitical titles), never from an engine list. Without it, the sentence
    alone cannot tell RedLine or China from a threat group, and it still grounds."""
    assert _verdict([name], body, non_actor_names=[name.upper()]) is not None
    assert _verdict([name], body) is None


def test_a_held_name_is_still_grounded_by_what_the_source_says_it_is():
    body = "Volt Typhoon is a state-sponsored threat group. Volt Typhoon targeted utilities."
    assert _verdict(["Volt Typhoon"], body, non_actor_names=["Volt Typhoon"]) is None


@pytest.mark.parametrize("text, expected", [
    ("malware dubbed RedLine", True), ("a loader known as 'RedLine'", True),
    ("RedLine, a commodity information stealer", True), ("RedLine, the new botnet", True),
    ("RedLine is an infostealer sold on forums", True), ("an infostealer, dubbed RedLine", True),
    # a name beside a software noun names what a group made, not a definition (QA of !1297)
    ("the RedLine stealer", False), ("the RedLine malware", False), ("RedLine variant", False),
    ("the RedLine group", False), ("RedLine ransomware", False),
    ("RedLine targeted banks with a new stealer", False),
    ("a new backdoor deployed by a group named RedLine", False),
])
def test_software_named(text, expected):
    assert g.software_named("RedLine", text) is expected


@pytest.mark.parametrize("before, after, expected", [
    ({"title": "LockBit"}, {"title": "ANY.RUN"}, True),
    ({"title": "LockBit"}, {"title": "LockBit", "name": "ANY.RUN"}, True),
    ({"title": "LockBit", "aliases": ["LockBit 3.0"]}, {"title": "LockBit", "aliases": ["ANY.RUN"]}, True),
    ({"title": "LockBit", "aliases": ["LockBit 3.0"]}, {"title": "LockBit", "aliases": []}, False),
    ({"title": "LockBit"}, {"title": "Lockbit"}, False),
    ({"title": "LockBit", "alias": "LB"}, {"title": "LockBit", "alias": "LB"}, False),
    ({}, {}, False),
    # a legacy scalar alias list is the same names as the list it canonicalizes to
    ({"title": "LockBit", "aliases": "LockBit 3.0, Bitwise Spider"},
     {"title": "LockBit", "aliases": ["LockBit 3.0", "Bitwise Spider"]}, False),
    ({"title": "LockBit", "aliases": "LockBit 3.0, Bitwise Spider"},
     {"title": "LockBit", "aliases": ["LockBit 3.0", "Bitwise Spider, ANY.RUN"]}, True),
])
def test_renamed(before, after, expected):
    assert g.renamed(before, after) is expected
    assert bool(g.rename_split(before, after)[0]) is expected


@pytest.mark.parametrize("after, expected", [
    ({"title": "Lazarus Group"}, False), ({"title": "Lazarus-Group", "aliases": ["Hidden Cobra"]}, True),
    ({"title": "ANY.RUN"}, True),
])
def test_a_page_always_answered_to_its_slug(after, expected):
    """Titling a nameless page after its own slug is not a rename (998 such pages on one vault)."""
    assert g.renamed({}, after, "lazarus-group") is expected
    new, kept = g.rename_split({}, after, "lazarus-group")
    assert bool(new) is expected and (kept or new)


def test_name_keys_cover_every_name_field():
    assert g.name_keys({"title": "Lock.Bit", "name": "", "aliases": ["X Y", 3], "alias": "z"}) == {
        "lockbit", "xy", "z"}
    assert g.name_key("ANY.RUN") == "any run"


def test_a_group_whose_sources_name_its_variant_is_still_grounded_by_what_it_does():
    body = "LockBit claimed the attack. A LockBit variant and the LockBit payload were deployed."
    assert _verdict(["LockBit"], body) is None


def test_affirms_actor_reads_actions_unless_told_not_to():
    window = "LockBit claimed the attack on the bank."
    assert g.affirms_actor("LockBit", window)
    assert not g.affirms_actor("LockBit", window, actions=False)


@pytest.mark.parametrize("name, alias, text, expected", [
    ("Earth Preta", "Mustang Panda", "Earth Preta (aka Mustang Panda, Bronze President) struck.", True),
    ("Earth Preta", "Mustang Panda", "Mustang Panda (Earth Preta) struck.", True),
    ("Earth Preta", "Mustang Panda", "Earth Preta, also known as Mustang Panda, struck.", True),
    ("Rare Werewolf", "Librarian Ghouls", "Rare Werewolf / Librarian Ghouls struck.", True),
    ("Night Crew", "RedLine", "Night Crew is a group. RedLine (a stealer) steals.", False),
    ("...", "RedLine", "RedLine struck.", False), ("Night Crew", "--", "Night Crew struck.", False),
])
def test_alias_tied(name, alias, text, expected):
    assert g.alias_tied(name, alias, text) is expected


def test_untied_names_keep_only_names_no_source_ties():
    doc = _doc("Earth Preta (aka Mustang Panda) deployed DOPLUGS. RedLine steals passwords.")
    assert g.untied_names(["Mustang Panda", "RedLine", "Earth Preta Two"], ["Earth Preta"],
                          [doc]) == ["RedLine"], "a distinctive shared word ties"
    assert g.untied_names(["Mustang Panda"], [], [doc]) == ["Mustang Panda"]


def test_a_nameless_page_keeps_its_slug_once_as_a_kept_name():
    assert g.rename_split({}, {"aliases": ["Mustang Panda"]}, "earth-preta") == (
        ["Mustang Panda"], ["earth preta"])
    assert g.rename_split({}, {"title": "Earth Preta"}, "earth-preta") == ([], ["Earth Preta"])
    assert g.rename_split({"title": "Earth Preta"}, {"title": "Earth Preta", "aliases": ["MP"]},
                          "earth-preta") == (["MP"], ["Earth Preta"])
    assert g.rename_split({}, {"aliases": ["MP"]}, "") == (["MP"], [])
    # a page with no title or name keeps its slug beside its aliases
    assert g.rename_split({"aliases": ["HEXANE"]}, {"aliases": ["HEXANE", "Lyceum APT"]},
                          "lyceum") == (["Lyceum APT"], ["HEXANE", "lyceum"])
    assert g.rename_split({"aliases": ["Lyceum"]}, {"aliases": ["Lyceum", "X"]},
                          "lyceum") == (["X"], ["Lyceum"])


@pytest.mark.parametrize("new, kept, text", [
    ("CrowdStrike", "Liminal Panda", "Detailed CrowdStrike analysis of LIMINAL PANDA (formerly "
                                     "LightBasin), a likely China-nexus threat actor."),
    ("CrowdStrike", "Liminal Panda", "CrowdStrike tracks Liminal Panda (formerly LightBasin)."),
    ("Mandiant", "APT41", "Mandiant reported that APT41 (aka Wicked Panda) breached networks."),
    ("Microsoft", "Midnight Blizzard", "Microsoft said Midnight Blizzard (formerly NOBELIUM) struck."),
    ("Trend Micro", "Earth Preta", "Trend Micro found Earth Preta (aka Mustang Panda) deployed it."),
    # a generic kind word alone ties nothing
    ("Group-IB", "Lazarus Group", ""), ("Team Cymru", "Sandworm Team", ""),
    ("Acme Ransomware Gang", "LockBit Ransomware Gang", ""), ("Kraken RaaS", "Medusa RaaS", ""),
    ("Team Cymru", "Sandworm Gang", ""), ("Group-IB", "Lazarus Team", ""),
])
def test_a_reporting_vendor_or_a_generic_word_ties_nothing(new, kept, text):
    """Delta QA of !1297: a tie binds to the glossed name only, and a generic word ties nothing."""
    assert g.untied_names([new], [kept], [_doc(text)] if text else []) == [new]


@pytest.mark.parametrize("new, kept, text", [
    ("Mustang Panda", "Earth Preta", "Earth Preta (aka Mustang Panda, Bronze President) struck."),
    ("Bronze President", "Earth Preta", "Earth Preta (aka Mustang Panda, Bronze President) struck."),
    ("LightBasin", "Liminal Panda", "Detailed CrowdStrike analysis of LIMINAL PANDA (formerly "
                                    "LightBasin), a likely China-nexus threat actor."),
    ("Wicked Panda", "APT41", "Mandiant reported that APT41 (aka Wicked Panda) breached networks."),
    ("Peach Sandstorm", "Curious Serpens", "Curious Serpens, also known as Peach Sandstorm, struck."),
    ("Earth Preta", "Mustang Panda", "Mustang Panda (also known as Earth Preta) struck."),
    ("Primitive Bear", "Gamaredon", "Gamaredon, tracked as Primitive Bear and Shuckworm, struck."),
])
def test_a_gloss_or_cue_on_the_kept_name_ties_its_alias(new, kept, text):
    assert g.untied_names([new], [kept], [_doc(text)]) == []


@pytest.mark.parametrize("new, kept", [
    ("Wekby Group", "Wekby"), ("Krybit ransomware", "Krybit"), ("Hidden Lynx group", "Hidden Lynx"),
    ("Medusa Ransomware Gang", "Medusa Ransomware"), ("Kraken RaaS", "Kraken"),
    ("Tick APT", "Bronze Butler Tick"), ("Lyceum APT", "lyceum"),
    ("Earth Preta Two", "Earth Preta"), ("Sandworm 2", "Sandworm"), ("El Machete", "Machete"),
    ("Storm-0558 Group", "Storm"), ("APT28 Group", "Fancy Bear APT28"),
])
def test_a_distinctive_shared_word_ties_a_name_extension(new, kept):
    """Overblock QA of !1297: 38 real aliases ("Wekby Group", "Krybit ransomware") were refused
    once shared words stopped tying. A kind word, a numeral or a tracker designator extends a name
    without making it another subject (okengine#1030)."""
    assert g.untied_names([new], [kept], []) == []


@pytest.mark.parametrize("new, kept", [
    # okengine#1030: the hostile names QA landed on sourced actor pages of a live vault copy
    ("Liminal Labs", "Liminal Panda"), ("Panda Bank", "Liminal Panda"),
    ("Liminal Security Inc", "Liminal Panda"), ("Preta Bank", "Earth Preta"),
    ("Earth Bank", "Earth Preta"), ("Gypsy Stealer", "Cobalt Gypsy"),
    ("Serpens Labs", "Boggy Serpens"), ("Gamaredon Labs", "Gamaredon"),
    # any other organization or software word, and an unheld tool name that shares a word
    ("Panda Corp", "Mustang Panda"), ("Lazarus Ltd", "Lazarus Group"),
    ("Kimsuky Loader", "Kimsuky"), ("Turla RAT", "Turla"), ("Turla Backdoor", "Turla"),
    ("Cobalt Strike", "Cobalt Group"), ("LockBit Black", "LockBit"),
    ("UNC5337 (Mandiant)", "UNC5337"), ("Wekby Group Labs", "Wekby"),
])
def test_a_shared_word_beside_an_organization_or_software_word_ties_nothing(new, kept):
    """okengine#1030: "Liminal Labs" shares "Liminal" with Liminal Panda but names a company, so
    the shared word does not make it the same actor; it must ground on its own."""
    assert g.untied_names([new], [kept], []) == [new], f"{new!r} must not tie to {kept!r}"


def test_a_hostile_name_still_ties_when_a_cited_source_glosses_it():
    """The gloss tie is unchanged: a source that says "X (aka Y)" binds Y whatever its words."""
    doc = _doc("Cobalt Group (aka Cobalt Strike Crew) targeted banks.")
    assert g.untied_names(["Cobalt Strike Crew"], ["Cobalt Group"], [doc]) == []


@pytest.mark.parametrize("name, kept, expected", [
    ("Wekby Group", "Wekby", True), ("Group", "Wekby Group", False),
    ("Liminal Labs", "Liminal Panda", False), ("Liminal", "Liminal Panda", True),
    ("TA505 Gang", "TA505", True), ("UNC 4221", "UNC 4221 Crew", True),
])
def test_extends_name(name, kept, expected):
    assert g.extends_name(name, kept) is expected


@pytest.mark.parametrize("name, kept", [("The Group", "Lyceum"), ("Threat Actor Group", "Wekby")])
def test_a_name_of_only_kind_words_neither_shares_nor_extends_an_unrelated_actor(name, kept):
    """okengine#1047 (nightly 13260): a kind word the kept name lacks is not a shared distinctive
    word. Computing the distinctive words as a symmetric difference instead of a difference left
    every generic word in the set, so "The Group" tied to "Lyceum" as its extension."""
    assert g.shares_distinctive_word(name, kept) is False
    assert g.extends_name(name, kept) is False
    assert g.untied_names([name], [kept], []) == [name]


@pytest.mark.parametrize("new, kept, text", [
    ("NANHAISHU", "APT-40", "APT-40 (aka Leviathan, TEMP.Periscope, NANHAISHU), a group."),
    ("Origami Elephant", "donot", "The DoNot APT group (India-linked, also tracked as APT-C-35, "
                                  "Origami Elephant) expanded."),
    ("Charming Kitten", "newsbeef", "NewsBeef APT (also known as Charming Kitten / Newscaster)."),
    ("Camaro Dragon", "stately taurus", "Stately Taurus (aka Mustang Panda, Bronze President, Red "
                                        "Delta, Luminous Moth, Earth Preta, Camaro Dragon) struck."),
    ("Primitive Bear", "Shuckworm", "Gamaredon (aka Shuckworm, Armageddon, Primitive Bear) struck."),
    ("\u6697\u77f3\u7ec4\u7ec7", "saaiwc", "\u6697\u77f3\u7ec4\u7ec7\uff08Saaiwc Group\uff09 struck."),
])
def test_real_vendor_glosses_tie_their_aliases(new, kept, text):
    """Overblock QA of !1297: real gloss shapes on a live vault (dots inside names, kind words
    before the gloss, slashes, long lists, siblings in one gloss, full-width parentheses)."""
    assert g.untied_names([new], [kept], [_doc(text)]) == []
