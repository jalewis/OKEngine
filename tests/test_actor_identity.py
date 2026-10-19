import pytest

from okengine.actor_identity import _plain, actor_identity_error, has_positive_actor_identity


def test_actor_identity_error_requires_both_title_and_evidence():
    assert actor_identity_error("", "A malware named TerminalFix targets users.") is None
    assert actor_identity_error("TerminalFix", "") is None
    assert not has_positive_actor_identity("", "A threat actor named Kimsuky targets users.")
    assert not has_positive_actor_identity("Kimsuky", "")


def test_actor_identity_evidence_is_capped_at_twelve_thousand_characters():
    assert _plain("x" * 12_001) == "x" * 12_000


def test_non_actor_definitions_are_deterministic():
    cases = [
        ("TerminalFix", "A new ClickFix variant, dubbed TerminalFix, tricks users."),
        ("VoiceBait", "VoiceBait is a social-engineering technique used in campaigns."),
        ("CVE-2099-1", "A vulnerability named CVE-2099-1 affects the service."),
        ("BridgePay", "BridgePay is ransomware deployed against billing systems."),
        ("CloudForge", "CloudForge is a software product used by development teams."),
        (
            "JFrog Artifactory",
            "Threat actors are exploiting a critical security flaw impacting JFrog Artifactory.",
        ),
    ]
    for title, body in cases:
        assert actor_identity_error(title, body), (title, body)
        assert not has_positive_actor_identity(title, body)


def test_positive_actor_definitions_require_the_named_subject():
    assert has_positive_actor_identity(
        "ShinyHunters", "ShinyHunters is a cybercriminal group associated with data theft."
    )
    assert has_positive_actor_identity(
        "Kimsuky", "A threat actor tracked as Kimsuky targets research organizations."
    )
    assert not has_positive_actor_identity(
        "TerminalFix", "An unrelated threat actor deployed a variant dubbed TerminalFix."
    )


def test_actor_operations_are_not_confused_with_software_words():
    body = "Gunra is a ransomware-as-a-service operation targeting government entities."
    assert actor_identity_error("Gunra", body) is None
    assert has_positive_actor_identity("Gunra", body)


def test_actor_exploiting_a_product_flaw_is_not_the_affected_object():
    body = "TeamPCP exploited a vulnerability in JFrog Artifactory to steal credentials."
    assert actor_identity_error("TeamPCP", body) is None
    assert has_positive_actor_identity("TeamPCP", "TeamPCP is a cybercriminal group. " + body)


CRITICAL_VULNERABILITY_BODY = (
    "A critical Windows driver vulnerability (CVE-2026-68820) that allows privilege "
    "escalation to SYSTEM has been patched by Microsoft. This vulnerability is "
    "actively being exploited in the wild."
)


def test_unnamed_opening_vulnerability_subject_is_not_an_actor():
    assert actor_identity_error("Critical Threat", CRITICAL_VULNERABILITY_BODY) == "vulnerability"
    assert actor_identity_error("Severe Finding", "A high risk rating indicates a severe flaw.") == "risk rating"


def test_vulnerability_mentions_do_not_reject_actor_subjects():
    for title, body in [
        ("Critical Threat", "Critical Threat is a threat group. " + CRITICAL_VULNERABILITY_BODY),
        ("XY", "XY exploited a critical Windows vulnerability."),
        ("Silence", "Silence is a crew exploiting Windows driver vulnerabilities."),
        ("Unknown One", "Imported identity; subtype unknown."),
        ("Gunra", "Gunra is a ransomware-as-a-service operation."),
        ("Vulnerability Researchers", "A vulnerability research group targets mailboxes."),
    ]:
        assert actor_identity_error(title, body) is None, (title, body)



def test_vulnerability_led_intro_preserves_later_affirmative_actor_definition():
    body = "A critical Windows vulnerability was exploited in the wild. Silence is a threat group targeting mailboxes."
    assert actor_identity_error("Silence", body) is None


def test_opening_vulnerability_exemption_requires_same_affirmative_subject():
    prefix = "A critical Windows vulnerability was exploited in the wild. "
    for suffix in [
        "Silence is not a threat actor.",
        "Another Group is a threat group; Silence is a tool.",
        "Silence is a tool used by a threat group.",
        "If Silence is a threat group, investigators will track it.",
    ]:
        assert actor_identity_error("Silence", prefix + suffix) == "vulnerability", suffix



def test_matching_display_heading_does_not_hide_vulnerability_subject():
    body = "# Critical Threat\n\n" + CRITICAL_VULNERABILITY_BODY
    assert actor_identity_error("Critical Threat", body) == "vulnerability"


def test_matching_display_heading_preserves_affirmative_actor_definition():
    body = "# Critical Threat\n\n" + CRITICAL_VULNERABILITY_BODY + " Critical Threat is a threat group."
    assert actor_identity_error("Critical Threat", body) is None


def test_unrelated_heading_is_not_removed_by_opening_rule():
    body = "# Evidence\n\n" + CRITICAL_VULNERABILITY_BODY
    assert actor_identity_error("Critical Threat", body) is None


@pytest.mark.parametrize("body", [
    "Critical Threat is a severity rating.",
    "Critical Threat is a vulnerability.",
    "High or Critical threat-risk ratings were assigned to vulnerabilities.",
    "It is a critical threat to the enterprise.",
    "The vulnerability poses a critical threat to organizations.",
    "A critical Windows driver vulnerability was patched.",
    "A critical Windows driver vulnerability was patched. Critical Threat is not a threat actor.",
    "A critical Windows driver vulnerability was patched. Silence is a threat group.",
])
def test_severity_canonical_name_requires_no_contrary_original_source(body):
    from okengine.actor_identity import severity_source_identity_error
    assert severity_source_identity_error("Critical Threat", [body]) is not None


def test_generated_actor_appendix_does_not_supply_actor_identity():
    from okengine.actor_identity import severity_source_identity_error
    body = "A critical Windows vulnerability was patched.\n\n## Actors named in this report\n- [[Critical Threat is a threat group]]\n"
    assert severity_source_identity_error("Critical Threat", [body]) == "vulnerability"


@pytest.mark.parametrize("body", [
    "Critical Threat exploited the Windows vulnerability.",
    "The group named Critical Threat targeted banks.",
    "A critical Windows vulnerability was patched. Critical Threat is a criminal group.",
])
def test_original_source_same_named_agent_overrides_severity_ambiguity(body):
    from okengine.actor_identity import severity_source_identity_error
    contrary = "A critical Windows vulnerability was patched."
    assert severity_source_identity_error("Critical Threat", [contrary, body]) is None


@pytest.mark.parametrize("body", [
    "If the group named Critical Threat targeted banks, investigators would respond.",
    "The group named Critical Threat did not target banks.",
    "Critical Threat did not exploit the vulnerability.",
])
def test_negated_and_hypothetical_source_roles_do_not_override_contrary_citation(body):
    from okengine.actor_identity import severity_source_identity_error
    assert severity_source_identity_error("Critical Threat", ["A critical Windows vulnerability was patched.", body]) == "vulnerability"


@pytest.mark.parametrize("title,bodies", [
    ("Silence", ["A critical Windows vulnerability was patched."]),
    ("", ["A critical Windows vulnerability was patched."]),
    ("Critical Threat", []),
    ("Critical Threat", [""]),
])
def test_source_identity_unknown_or_nonseverity_is_not_contrary_evidence(title, bodies):
    from okengine.actor_identity import severity_source_identity_error
    assert severity_source_identity_error(title, bodies) is None


@pytest.mark.parametrize("suffix", [
    "Morgana exploited it to target banks.",
    "Morgana targets banks.",
    "The group named Morgana targeted banks.",
])
@pytest.mark.parametrize("heading", ["", "# Morgana\n\n"])
def test_vulnerability_led_intro_preserves_same_named_active_agent(suffix, heading):
    body = heading + "A critical Windows driver vulnerability was patched. " + suffix
    assert actor_identity_error("Morgana", body) is None


@pytest.mark.parametrize("suffix", [
    "", "Another Group exploited it to target banks.",
    "Morgana did not exploit it.", "If Morgana exploited it, banks would respond.",
    "The group named Morgana did not target banks.",
    "If the group named Morgana targeted banks, investigators would respond.",
])
def test_opening_activity_exemption_requires_same_affirmative_sentence_subject(suffix):
    body = "A critical Windows driver vulnerability was patched. " + suffix
    assert actor_identity_error("Morgana", body) == "vulnerability"
    assert actor_identity_error("", body) is None


@pytest.mark.parametrize("prose", [
    "Harbor Hotel Group suffered a data breach. The ransomware group Nightlock claimed responsibility.",
    "Nightlock attacked Harbor Hotel Group. Harbor Hotel Group disclosed a breach.",
    "Harbor Hotel Group was attacked by Nightlock.",
    "Harbor Hotel Group is the victim of a ransomware attack.",
    "Harbor Hotel Group reported a breach affecting its customers.",
    "Harbor Hotel Group disclosed a data breach impacting its customers.",
    "Harbor Hotel Group experienced a breach.",
])
def test_original_source_victim_role_rejects_ordinary_candidate_name(prose):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Harbor Hotel Group", [prose]) == "victim organization"


@pytest.mark.parametrize("addition", [
    "Harbor Hotel Group is a hotel group.",
    "Harbor Hotel Group is an organization.",
    "Harbor Hotel Group targets hospitality customers.",
    "Harbor Hotel Group deploys recovery tooling.",
    "Harbor Hotel Group attacks breach rumors.",
    "Harbor Hotel Group claims responsibility for customer service.",
    "Harbor Hotel Group targets customers. Nightlock deployed malware against banks.",
    "Harbor Hotel Group is not a criminal group.",
    "If Harbor Hotel Group is a criminal group, investigators will respond.",
    "The company denied 'Harbor Hotel Group is a criminal group'.",
    "'Harbor Hotel Group is a criminal group' was denied.",
])
def test_benign_or_denied_source_claim_cannot_override_victim_role(addition):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Harbor Hotel Group", [
        "Harbor Hotel Group suffered a breach. " + addition
    ]) == "victim organization"


@pytest.mark.parametrize("prose", [
    "Harbor Group is a cybercriminal group.",
    "The threat group tracked as Harbor Group stole credentials.",
    "Harbor Group launched a ransomware attack.",
    "Harbor Group claimed responsibility for a ransomware attack.",
    "Harbor Group stole credentials.",
    "Harbor Group exfiltrated private data.",
    "Harbor Group attacked networks using malware.",
    "Harbor Group deployed ransomware against banks.",
])
@pytest.mark.parametrize("positive_first", [False, True])
def test_original_source_adversarial_role_preserves_dual_role_identity(prose, positive_first):
    from okengine.actor_identity import actor_source_identity_error
    victim = "Harbor Group was attacked by a rival."
    bodies = [prose, victim] if positive_first else [victim, prose]
    assert actor_source_identity_error("Harbor Group", bodies) is None


@pytest.mark.parametrize("title,bodies", [
    ("", ["A company suffered a breach."]),
    ("Novel Cluster", []),
    ("Novel Cluster", ["Its identity is unresolved."]),
    ("Harbor Group", ["Harbor Groups suffered a breach."]),
    ("Harbor Group", ["If Harbor Group was attacked by a rival, investigate."]),
    ("Harbor Group", ["A rival never attacked Harbor Group."]),
])
def test_unsupported_or_negated_victim_role_remains_unknown(title, bodies):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error(title, bodies) is None


def test_non_ascii_victim_name_and_generated_links_are_not_identity_proof():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Hôtel Étoile", [
        "# Original report\nHôtel Étoile suffered a breach.\n\n## Actors named in this report\n- [[Hôtel Étoile]]\n"
    ]) == "victim organization"
    assert actor_source_identity_error("Critical Threat", [CRITICAL_VULNERABILITY_BODY]) == "vulnerability"


@pytest.mark.parametrize("presentation", [
    '# Harbor Hotel Group is a criminal group\n',
    'Example: "Harbor Hotel Group is a criminal group". ',
    'Hypothetical: Harbor Hotel Group launched a ransomware attack. ',
])
def test_presentation_or_example_does_not_override_original_victim(presentation):
    from okengine.actor_identity import actor_source_identity_error
    victim = "Harbor Hotel Group suffered a data breach."
    assert actor_source_identity_error("Harbor Hotel Group", [presentation + victim]) == "victim organization"


def test_source_headline_alone_is_not_victim_evidence():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Harbor Hotel Group", [
        "# Harbor Hotel Group suffered a data breach\nThe incident remains unconfirmed."
    ]) is None


@pytest.mark.parametrize("affirmative", [False, True])
def test_complete_source_roles_include_prose_after_old_profile_limit(affirmative):
    from okengine.actor_identity import actor_source_identity_error
    body = "Harbor Hotel Group suffered a data breach. " + "Unrelated background. " * 700
    if affirmative:
        body += "Harbor Hotel Group is a ransomware actor launching unauthorized malware attacks."
    assert actor_source_identity_error("Harbor Hotel Group", [body]) == (None if affirmative else "victim organization")


@pytest.mark.parametrize("possessive", ["'s customers", "’s supplier"])
def test_named_possessive_object_is_not_company_victim(possessive):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Harbor Hotel Group", [
        "Nightlock attacked Harbor Hotel Group" + possessive + "."
    ]) is None


@pytest.mark.parametrize("role", ["ransomware-as-a-service operation", "RaaS operation"])
def test_service_operation_identity_preserves_genuine_dual_role(role):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Ashra", [
        "Ashra is a " + role + ".", "Ashra was attacked by a rival."
    ]) is None


def test_exact_company_name_period_preserves_victim_role():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Acme Inc.", [
        "Acme Inc. suffered a data breach. Nightlock is a ransomware group."
    ]) == "victim organization"


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_plural_possessive_object_is_not_company_patient(apostrophe):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error("Juniper Systems", [
        "Nightlock attacked Juniper Systems" + apostrophe + " customers."
    ]) is None


@pytest.mark.parametrize('title,description', [
    ('Lazarus', 'North Korean state-sponsored hacking group'),
    ('LockBit', 'Russian-speaking ransomware group'),
    ('Novel Cluster', 'APT group'),
    ('Unknown One', 'extortion gang'),
    ('Harbor Hotel Group', 'international independent extortion gang'),
    ('Étoile', 'francophone hacker collective'),
    ('XY', 'ransomware-as-a-service operation'),
    ('Silence', ' '.join(['international'] * 40) + ' hacking group'),
])
@pytest.mark.parametrize('positive_first', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_described_original_source_actor_preserves_dual_role(title, description, positive_first, reverse):
    from okengine.actor_identity import actor_source_identity_error
    positive = (f'The {description} tracked as {title} was observed.' if reverse
                else f'{title} is an {description}.')
    victim = f'{title} was attacked by a rival.'
    bodies = [positive, victim] if positive_first else [victim, positive]
    assert actor_source_identity_error(title, bodies) is None


@pytest.mark.parametrize('description', [
    'international hotel group', 'research group', 'customer service organization',
    'hackingly connected group', 'extortionate business group', 'adapt group',
    'hotel, hacking group', 'hotel, an APT group', 'hotel that employs a hacking group',
    'hotel which was described as a hacking group', 'hotel and unrelated hacking group',
    'hotel or an extortion gang', 'not a hacking group', 'international never hacking group',
    'international might be a hacking group', 'international is a hacking group',
    'international denied hacking group', 'international "hacking" group',
    "international 'hacking' group", 'international hacking, group',
    'international hacking', 'international hacking 2 group',
    'hacking group-like', 'hacking group-therapy', 'APT actor-facing', 'APT groupX',
])
@pytest.mark.parametrize('reverse', [False, True])
def test_descriptive_identity_cannot_cross_phrase_boundaries(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'The {description} named Harbor Group was discussed.' if reverse
             else f'Harbor Group is a {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization'


@pytest.mark.parametrize('embedded_name', ['OtherHarbor Group', 'Harbor Groups', 'Harbor Grouping'])
@pytest.mark.parametrize('reverse', [False, True])
def test_described_actor_identity_requires_complete_named_subject(embedded_name, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'The APT group named {embedded_name} was discussed.' if reverse
             else f'{embedded_name} is an APT group.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization'


@pytest.mark.parametrize('claim', [
    'Harbor Group is not an APT group.',
    'If Harbor Group is a North Korean state-sponsored hacking group, investigate.',
    "The company denied 'Harbor Group is a Russian-speaking ransomware group'.",
    "'Harbor Group is an extortion gang' was denied.",
    '# Harbor Group is an APT group\n',
    '## Actors named in this report\n- [[Harbor Group is an APT group]]\n',
])
def test_described_actor_identity_preserves_denial_and_presentation_guards(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group was attacked by a rival.', claim]) == 'victim organization'


def test_documented_descriptive_actor_regression_preserves_original_source_identity():
    from okengine.actor_identity import actor_source_identity_error
    for title, description in [
        ('Lazarus', 'North Korean state-sponsored hacking group'),
        ('LockBit', 'Russian-speaking ransomware group'),
        ('Novel Cluster', 'APT group'),
        ('Unknown One', 'extortion gang'),
    ]:
        assert actor_source_identity_error(title, [
            f'{title} was attacked by a rival.', f'{title} is an {description}.']) is None


@pytest.mark.parametrize('claim', [
    'Analysts described the international hacking group named Harbor Group.',
    'A report describes the Russian-speaking ransomware group known as Harbor Group.',
    'The report was published by a ransomware group named Harbor Group.',
    'The report was published by an international APT group named Harbor Group.',
])
def test_reverse_descriptive_role_preserves_ordinary_narrative(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None


def test_forward_benign_kind_cannot_borrow_later_hacking_group():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Harbor Group is a hotel group with a hacking group.']) == 'victim organization'


@pytest.mark.parametrize('cue', ['hacking', 'ransomware'])
def test_separately_named_reverse_group_is_original_source_identity(cue):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        f'The hotel group with a {cue} group named Harbor Group was discussed.']) is None


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('padding_words', [0, 3, 17])
def test_complete_descriptive_identity_endpoint_retains_trailing_denial(reverse, padding_words):
    from okengine.actor_identity import actor_source_identity_error
    padding = 'Reporting ' * padding_words
    claim = (padding + 'a hacking group named Harbor Group was denied.' if reverse
             else padding + 'Harbor Group is a hacking group was denied.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization'


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('padding_words', [0, 3, 17])
def test_complete_identity_endpoint_does_not_borrow_prior_wrong_subject_denial(reverse, padding_words):
    from okengine.actor_identity import actor_source_identity_error
    context = 'The identity claim about Rival was false and ' + 'reporting ' * padding_words
    claim = (context + 'published by a hacking group named Harbor Group.' if reverse
             else context + 'Harbor Group is a hacking group.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None


def test_interior_article_cannot_join_an_unrelated_role_phrase():
    from okengine.actor_identity import actor_source_identity_error
    claim = 'Harbor Group is a hotel a hacking group.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization'


def test_long_incomplete_descriptor_ends_without_index_error():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Harbor Group is a ' + 'international ' * 30 + 'hacking']) == 'victim organization'


def test_rejected_intermediate_article_does_not_hide_later_explicit_agent():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'The report was published with a note by a hacking group named Harbor Group.']) is None


def test_first_named_role_is_not_overwritten_by_later_conditional_role():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'A hacking group named Harbor Group was observed and may be assisted by a hacking group named Harbor Group.']) is None


@pytest.mark.parametrize('title', ['May Harbor', 'Identity Claim False'])
@pytest.mark.parametrize('reverse', [False, True])
def test_exact_actor_name_does_not_become_its_own_denial_context(title, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'A hacking group named {title} was observed.' if reverse
             else f'{title} is a hacking group.')
    assert actor_source_identity_error(title, [
        f'{title} suffered a breach.', claim]) is None


def test_long_role_excludes_prior_false_identity_claim_about_another_subject():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'The identity claim about Rival was false and Harbor Group is an '
        'international independent coordinated focused hacking group.']) is None


@pytest.mark.parametrize('claim', [
    'Reporting: XY is a claim-led APT group with false reports.',
    'A public report issued by a claim-led APT group named XY had false reports.',
])
def test_completed_identity_role_does_not_turn_false_reports_into_identity_denial(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('XY', ['XY suffered a breach.', claim]) is None


# okengine#993 / release audit of !1266 (#973): the descriptor grammar must require the
# adversarial cue to modify the actor-kind noun. Victim, target, defender and service
# descriptions that merely contain a cue word do not make the subject an adversary.
_VICTIM_OR_DEFENDER_DESCRIPTIONS = [
    'victim of ransomware group Akira',
    'target of hacking group activity',
    'hotel targeted by hacking group',
    'company hit by ransomware gang',
    'customer of ransomware group Akira',
    'victim of threat actor activity',
    'hospital hit by ransomware group',
    'manufacturer breached by threat actor',
    'client for ransomware gang',
    'firm attacked with hacking crew',
    'ransomware negotiation team',
    'threat intelligence group',
    'threat intelligence team',
    'threat intelligence vendor group',
    'threat research group',
    'threat detection team',
    'criminal defense group',
    'criminal defense law group',
    'ransomware recovery team',
    'ransomware negotiation group',
    'ransomware response team',
    'intrusion response team',
    'hacking prevention team',
    'ransomware victim group',
    'victim ransomware group',
    'targeted ransomware group',
    'victim-support ransomware group',
    'security company hacking group',
]


@pytest.mark.parametrize('description', _VICTIM_OR_DEFENDER_DESCRIPTIONS)
@pytest.mark.parametrize('reverse', [False, True])
def test_cue_that_does_not_modify_an_adversary_noun_keeps_victim_rejection(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'The {description} named Harbor Group was formed.' if reverse
             else f'Harbor Group is a {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


def test_definite_victim_descriptor_keeps_victim_rejection():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Harbor Group is the ransomware victim group.']) == 'victim organization'


def test_victim_hit_by_ransomware_group_in_same_source_keeps_victim_rejection():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Acme Health', [
        'Acme Health is a hospital hit by ransomware group. Acme Health was breached by LockBit.'
    ]) == 'victim organization'


@pytest.mark.parametrize('description', [
    'ransomware group', 'cybercriminal group', 'hacking group', 'threat actor',
    'nation-state threat group', 'advanced persistent threat group', 'data extortion group',
    'criminal organization', 'ransomware actor',
])
@pytest.mark.parametrize('reverse', [False, True])
def test_cue_modifying_the_actor_noun_preserves_dual_role(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'The {description} named Harbor Group was observed.' if reverse
             else f'Harbor Group is a {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


@pytest.mark.performance  # scans a 50k-word clause: a latency bound, not a 100 ms unit test
@pytest.mark.parametrize('filler', [
    'the x ', 'a remote flaw in the vendor portal ', 'a hacking group named Harbor Group ',
])
def test_long_unpunctuated_source_clause_is_scanned_in_linear_time(filler, monkeypatch):
    # A long list or table with no sentence punctuation collapses into one clause. The role
    # scan must not rescan the clause prefix once per article (the quadratic #973 regression).
    import okengine.actor_identity as ai
    walked = []
    real = ai._source_role_phrase_end

    def counting(text):
        walked.append(len(text))
        return real(text)

    monkeypatch.setattr(ai, '_source_role_phrase_end', counting)
    import time
    # The subject name is present, so the clause is not skipped before the role scan.
    body = 'If ' + filler * (50_000 // len(filler.split())) + 'Harbor Group'
    started = time.perf_counter()
    ai.actor_source_identity_error('Harbor Group', ['Harbor Group suffered a breach.', body])
    elapsed = time.perf_counter() - started
    # Primary contract is an operation count: characters handed to the phrase walk stay
    # linear in the clause length rather than growing with articles x clause length.
    assert sum(walked) <= 2 * len(body)
    # Generous wall-clock backstop (linear scan: well under a second; quadratic: minutes).
    assert elapsed < 20, elapsed


@pytest.mark.parametrize('claim', [
    'Hacking group named Harbor Group posted a notice.',
    'A  named Harbor Group was observed.',
])
def test_naming_without_a_preceding_role_phrase_keeps_victim_rejection(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization'


@pytest.mark.parametrize('claim', [
    'Harbor Group is a group.',
    'The group named Harbor Group was observed.',
])
def test_bare_actor_kind_without_a_cue_is_not_an_adversarial_identity(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization'


def test_reverse_role_after_a_second_agent_by_starts_a_new_phrase():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'The report by analysts was shared by a hacking group named Harbor Group.']) is None


def test_skipped_reverse_role_does_not_hide_a_later_agent_role():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Reports about the hotel that hired a hacking group named Harbor Group '
        'were written by a hacking group named Harbor Group.']) is None


def test_reverse_role_uses_the_nearest_agent_by_segment():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'The report by analysts was copied by the staff that shared it '
        'by a hacking group named Harbor Group.']) is None


def test_reverse_role_after_many_break_marks_is_still_recognized():
    # More than 256 earlier break marks: the comparison is by value, not identity.
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'x, ' * 300 + 'published by a hacking group named Harbor Group']) is None


# QA on !1268 (okengine#993): the actor-kind noun must be the HEAD of the noun phrase. When
# it only modifies a later head noun (a compound or a possessive), the subject is that head
# (a target, a victim), not an adversary.
_KIND_AS_MODIFIER_CLAIMS = [
    'Harbor Group is a former ransomware group target.',
    'Harbor Group is the latest ransomware group victim.',
    'Harbor Group is an extortion gang mark.',
    "Harbor Group is an APT group's target.",
    'Harbor Group is a hacker group victim.',
    "Harbor Group is a ransomware group's victim.",
    'Harbor Group is a ransomware group’s victim.',
    "Harbor Group is a threat actor's latest victim.",
    "Harbor Group is the ransomware operation's victim.",
    "Harbor Group is the ransomware group's latest victim.",
    "Harbor Group is the ransomware groups' victim.",
    'Harbor Group is a ransomware group target.',
    'Harbor Group is a ransomware group victim.',
    'Harbor Group is the ransomware gang victim.',
    'Harbor Group is a threat actor target.',
    'Harbor Group is a ransomware gang casualty.',
    'Harbor Group is a ransomware crew hostage.',
    'Harbor Group is a cybercriminal group target.',
    'Harbor Group is a cybercriminal group victim.',
    'Harbor Group is a criminal group target.',
    'Harbor Group is a criminal group victim.',
    'Harbor Group is a threat group target.',
    'Harbor Group is a threat group victim.',
    'Harbor Group is an intrusion team target.',
    'Harbor Group is a ransomware operation victim.',
    'Harbor Group is a ransomware operator customer.',
    'Harbor Group was a ransomware group target.',
    'Harbor Group is a ransomware group hit by LockBit.',
    'Harbor Group is a ransomware group victimized by LockBit.',
    'Harbor Group is a ransomware group targeted by LockBit.',
    'Harbor Group is a ransomware group by name only.',
    'Harbor Group is a ransomware group 2 victim.',
]


@pytest.mark.parametrize('claim', _KIND_AS_MODIFIER_CLAIMS)
def test_actor_kind_that_modifies_a_later_head_noun_keeps_victim_rejection(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('claim', [
    'Harbor Group is a ransomware gang.',
    'Harbor Group is a Russian ransomware group.',
    'Harbor Group is a financially motivated cybercriminal group.',
    'Harbor Group is the ransomware operation behind the attack.',
    'Harbor Group is a North Korean state-sponsored hacking group.',
    'Harbor Group is an APT group.',
    'Harbor Group is an extortion gang.',
    'Harbor Group is a threat actor.',
    'Harbor Group is a criminal organization.',
    'Harbor Group is a Russian-speaking ransomware group.',
    'Harbor Group is a ransomware-as-a-service operation.',
    'Harbor Group is a ransomware affiliate.',
    'Harbor Group is a ransomware group that targets hospitals.',
    'Harbor Group is a prolific ransomware group.',
    'Harbor Group is a RaaS operation.',
    'A ransomware group named Harbor Group claimed the attack.',
    'The extortion gang known as Harbor Group leaked data.',
    'Harbor Group is a ransomware group operating since 2020.',
    'Harbor Group is a ransomware group in Russia.',
    'Harbor Group is a ransomware group with ties to Conti.',
    'Harbor Group is a ransomware group (also called HG).',
    'Harbor Group is a ransomware group: it encrypts files.',
    'Harbor Group is a ransomware group known as HG.',
    'Harbor Group is a ransomware group linked to Conti.',
    'Harbor Group is a ransomware group targeting hospitals.',
])
def test_actor_kind_that_heads_the_noun_phrase_preserves_dual_role(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


# okengine#995 (operator decision 2026-10-06): the adversarial cue list covers every adversary
# self-description the corpus uses, including hyphenated, open-compound and plural forms, and
# the adversary-organization kind nouns that such cues modify.
_WIDENED_ADVERSARY_DESCRIPTIONS = [
    'hacktivist group', 'pro-Russian hacktivist collective', 'hacktivists collective',
    'cybercrime group', 'cyber-crime gang', 'cyber crime group', 'eCrime group',
    'e-crime syndicate', 'crime syndicate', 'cybercriminals gang', 'cyber-criminal crew',
    'criminals ring', 'espionage group', 'cyberespionage group', 'cyber-espionage group',
    'cyber espionage team', 'state-sponsored group', 'state sponsored actor',
    'state-backed group', 'state backed crew', 'nation-state group', 'nation state actor',
    'Chinese state-nexus group', 'state-directed group', 'state-affiliated actor',
    'state-aligned group', 'hacker collective', 'hackers group', 'APTs group',
    'malware gang', 'botnet operator', 'phishing gang', 'phishing-as-a-service operation',
    'PhaaS operation', 'carding group', 'fraudster ring', 'fraudsters network',
    'scam network', 'scammers syndicate', 'extortionist crew', 'extortionists group',
    'data-theft group', 'data theft gang', 'data-extortion gang', 'terrorist organization',
    'mercenary group', 'cyber-mercenary outfit', 'hack-for-hire group',
    'disinformation network', 'malicious actor', 'threat-actor group', 'hacking syndicate',
    'cybercrime ring', 'espionage network', 'hacktivist movement', 'threat cluster',
    'espionage outfit', 'hacktivist army', 'criminal enterprise', 'ransomware alliance',
    'initial access broker', 'access broker', 'initial-access broker',
    'Iranian state-sponsored espionage group', 'anti-Israel hacktivist group',
    'anti-government hacktivist collective',
]


@pytest.mark.parametrize('description', _WIDENED_ADVERSARY_DESCRIPTIONS)
@pytest.mark.parametrize('reverse', [False, True])
def test_widened_adversary_descriptor_preserves_dual_role(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    article = 'an' if description[0].lower() in 'aeiou' else 'a'
    claim = (f'The {description} named Harbor Group was observed.' if reverse
             else f'Harbor Group is {article} {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


@pytest.mark.parametrize('claim', [
    'Harbor Group is a hospital fighting ransomware groups.',
    'Harbor Group is a firm tracking hacktivist groups.',
    'Harbor Group is a bank battling cybercrime gangs.',
    'Harbor Group is an umbrella term encompassing cybercriminal groups.',
    'Harbor Group is a hospital fighting threat actors.',
    'Harbor Group is a provider studying access brokers.',
])
def test_bare_plural_kind_after_a_participle_keeps_victim_rejection(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


# The widened cues must not reopen the victim guard: a new cue that modifies a victim, target,
# defender or service head, or a kind noun that only modifies a later head, is not an adversary.
_WIDENED_CUE_VICTIM_OR_DEFENDER = [
    "a hacktivist group's victim", 'an espionage group target', 'a state-sponsored hacking victim',
    'a cybercrime research team', 'an espionage defense team', 'a hacktivist group victim',
    'a nation-state target', 'a state-sponsored attack victim', 'an espionage victim group',
    'a cybercrime prevention group', 'a police cybercrime team', 'a counter espionage team',
    'a counter-espionage group', 'an anti-phishing group', 'a phishing awareness team',
    'a fraud prevention team', 'a malware analysis team', 'a botnet takedown team',
    'a digital crimes unit', 'a cybercrime unit', 'an access broker victim',
    'a hacktivist-hit firm', 'an espionage group-hit firm', "the hacktivist groups' victim",
    'a sponsored group', 'a state group', 'a law enforcement cybercrime team',
    'a scam victim network', "a cybercrime syndicate's target", 'a terrorist organization target',
    'a disinformation research network', 'a state-sponsored espionage casualty',
    'an anti-fraud group',
]


@pytest.mark.parametrize('description', _WIDENED_CUE_VICTIM_OR_DEFENDER)
def test_widened_cue_on_a_victim_or_defender_head_keeps_victim_rejection(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'hacktivist group victim', 'espionage group target', 'cybercrime research team',
    'espionage defense team', 'police cybercrime team', 'counter espionage team',
    'anti-phishing group', 'cybercrime prevention group',
])
def test_widened_cue_on_a_victim_or_defender_head_keeps_reverse_victim_rejection(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'The {description} named Harbor Group was formed.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'threat actor group', 'threat-actor', 'ransomware operator crew', 'APT actor group',
])
@pytest.mark.parametrize('reverse', [False, True])
def test_cued_kind_noun_modifying_a_later_kind_noun_preserves_dual_role(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    article = 'an' if description[0].lower() in 'aeiou' else 'a'
    claim = (f'The {description} named Harbor Group was observed.' if reverse
             else f'Harbor Group is {article} {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


@pytest.mark.parametrize('description', [
    'a threat actor group victim', 'a threat actor victim group', "a threat actor group's target",
    'a ransomware gang victim network', 'a group gang',
])
def test_kind_noun_chain_still_requires_an_adversary_head(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


# QA of !1271 (okengine#995): state and nation-state descriptors and bare "hacker(s)" denote
# affiliation, not adversarial intent. They identify an adversary only before an adversary
# kind noun (group, actor, gang, crew, collective, adversary); before an organization,
# enterprise, network, team or other broad noun they describe a state-linked company or a
# hacker community, which is a common breach victim.
_RESTRICTED_CUE_ON_A_BROAD_KIND = [
    'Harbor Group is a state-backed organization.',
    'Harbor Group is a state-backed enterprise.',
    'Harbor Group is a state backed enterprise.',
    'Harbor Group is a state-affiliated organization.',
    'Harbor Group is a state-aligned network.',
    'Harbor Group is a state-directed enterprise.',
    'Harbor Group is a state-sponsored organization.',
    'Harbor Group is a state-sponsored enterprise.',
    'Harbor Group is a nation state enterprise.',
    'Harbor Group is a nation-state organization.',
    'Harbor Group is a state-nexus alliance.',
    'Harbor Group is a state-sponsored operation.',
    'Harbor Group is a state-backed team.',
    'Harbor Group is a nation-state cluster.',
    'Harbor Group is a hacker team.',
    'Harbor Group is a hacker network.',
    'Harbor Group is a hackers movement.',
    'The state-backed enterprise named Harbor Group was formed.',
    'The hacker network named Harbor Group was formed.',
    'Harbor Group is a state-backed group enterprise.',
    'Harbor Group is a hacker group network.',
    'Harbor Group is a state-sponsored threat actor network.',
]


@pytest.mark.parametrize('claim', _RESTRICTED_CUE_ON_A_BROAD_KIND)
def test_affiliation_cue_on_a_broad_kind_keeps_victim_rejection(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'Russian state-backed group', 'nation-state group', 'nation state threat actor',
    'state-sponsored actor', 'state sponsored gang', 'state-nexus adversary',
    'state-aligned crew', 'nation-state collective', 'hacker group', 'hackers collective',
    'state-sponsored hacking team', 'state-backed threat actor group',
])
@pytest.mark.parametrize('reverse', [False, True])
def test_affiliation_cue_on_an_adversary_kind_preserves_dual_role(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    article = 'an' if description[0].lower() in 'aeiou' else 'a'
    claim = (f'The {description} named Harbor Group was observed.' if reverse
             else f'Harbor Group is {article} {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


# Defender modifiers anywhere in the descriptor make it a defender or community, not an
# adversary ("an ethical hacker group", "a white hat hacker collective").
_DEFENDER_MODIFIED_DESCRIPTIONS = [
    'an ethical hacker group', 'an ethical hacking group', 'a white-hat hacker collective',
    'a white hat hacker collective', 'a whitehat hacking group', 'a grey-hat hacker group',
    'a gray hat hacking crew', 'a red-team hacking group', 'a blue-team threat group',
    'a purple-team hacking crew', 'a bug-bounty hacker group', 'a bug bounty hacking group',
    'a CTF hacking team', 'a student hacker group', 'a student-run hacking group',
    'a conference hacker collective', 'an academic hacking group', 'a university hacking group',
    'a hackathon hacker group', 'a pentest hacking team', 'a penetration testing threat group',
    'a pentesting hacking crew', 'a pentester hacker group', 'a research hacking group',
]


@pytest.mark.parametrize('description', _DEFENDER_MODIFIED_DESCRIPTIONS)
@pytest.mark.parametrize('reverse', [False, True])
def test_defender_modifier_in_the_descriptor_keeps_victim_rejection(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'The {description.split(" ", 1)[1]} named Harbor Group was formed.' if reverse
             else f'Harbor Group is {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'malware-as-a-service operation', 'MaaS gang', 'black-hat hacking group',
])
@pytest.mark.parametrize('reverse', [False, True])
def test_service_model_and_black_hat_descriptors_preserve_dual_role(description, reverse):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'The {description} named Harbor Group was observed.' if reverse
             else f'Harbor Group is a {description}.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


# okengine#996: a defender noun in the head noun's trailing post-modifier (a membership "of"
# phrase or a relative clause that says what the group is, runs or hosts) makes the subject
# a defender community, exactly as a defender modifier before the kind noun does.
_POSTMODIFIER_DEFENDER_DESCRIPTIONS = [
    'hacking group of ethical hackers',
    'hacking collective of students',
    'hacker group that runs a conference',
    'hacker group that hosts a hackathon',
    'hacking group of bug bounty hunters',
    'hacking team of university researchers',
    'hacker collective of CTF players',
    'hacking crew of pentesters',
    'hacking group of white-hat hackers',
    'hacking group of white hat hackers',
    'hacking group consisting of students',
    'hacker collective whose members are students',
    'hacking group who are ethical hackers',
    'hacker group which organizes a CTF',
    'hacking group of former Conti members and ethical hackers',
    'hacker group that is run by a university',
    'hacker group that has hosted an academic conference',
]


@pytest.mark.parametrize('description', _POSTMODIFIER_DEFENDER_DESCRIPTIONS)
@pytest.mark.parametrize('form', ['subject', 'named', 'tracked as'])
def test_defender_noun_in_the_post_modifier_keeps_victim_rejection(description, form):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'Harbor Group is a {description}.' if form == 'subject'
             else f'A {description} {form} Harbor Group posted a notice.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'ransomware group of former Conti members',
    'hacking group that targets hospitals',
    'state-backed group that runs phishing campaigns',
    'cybercrime crew of Russian speakers',
    # A defender noun that a relative clause makes the group's target, tool or lure is not
    # the group's own membership.
    'hacking group that targets university researchers',
    'hacking group that impersonates academic researchers',
    'hacking group that uses conference-themed lures',
    'hacking group that abuses pentest tools',
    'hacking group that is targeting students',
    'hacking group which was exposed by university researchers',
    # The membership phrase ends at a relative or preposition; a later defender noun is not
    # part of it.
    'hacking group of Russian speakers that phishes students',
    'hacking group of Russian speakers from a university town',
    'hacking group of former Conti members, which a university tracked',
    'hacking group that runs phishing campaigns that steal student credentials',
    'hacking group of hackers who were members',
])
def test_adversary_post_modifier_without_a_defender_member_preserves_dual_role(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is a {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


@pytest.mark.parametrize('claim', [
    'Harbor Group is a hacking group that is',
    'Harbor Group is a hacking group who',
    'Harbor Group is a hacking group of',
])
def test_truncated_post_modifier_preserves_dual_role(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


@pytest.mark.parametrize('claim', [
    'Harbor Group is a hacking group whose',
    'Harbor Group is a hacking group of Russian speakers at a university',
    'Harbor Group is a hacking group of hackers against university students',
])
def test_post_modifier_scan_bounds_preserve_dual_role(claim):
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


def test_constitutive_verbs_never_end_or_trigger_the_post_modifier_scan():
    # The scan starts AT the constitutive verb, so no such verb may be a stop, relation or
    # defender word.
    import okengine.actor_identity as ai
    for verb in ai._SOURCE_ROLE_CONSTITUTIVE:
        assert verb not in ai._SOURCE_ROLE_STOPS, verb
        assert verb not in ai._SOURCE_ROLE_RELATIONS, verb
        assert not ai._source_role_defender_compound(verb, ''), verb
        assert verb not in ai._SOURCE_ROLE_DEFENDERS, verb


@pytest.mark.parametrize('description', [
    'hacker group that hosts students', 'hacker group that has hosted students',
    'hacker group who are students',
])
def test_defender_right_after_a_constitutive_verb_keeps_victim_rejection(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is a {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


# QA of !1275: a constitutive membership verb keeps its own preposition ("consists of",
# "is made up of", "consist mostly of") before the members.
_MEMBERSHIP_VERB_DEFENDER_DESCRIPTIONS = [
    'hacking group that consists of students',
    'hacking group that consisted of pentesters',
    'hacking group that is composed of ethical hackers',
    'hacking group that is made up of students',
    'hacking group that is made of students',
    'hacking group which is comprised of students',
    'hacking group who consist mostly of students',
    'hacking group that is formed of university students',
    'hacking group of white hats',
    'hacking group of grey hats',
    'hacking group of security researchers',
    'hacker group that runs a student club',
    'hacking group comprising largely of students',
]


@pytest.mark.parametrize('description', _MEMBERSHIP_VERB_DEFENDER_DESCRIPTIONS)
@pytest.mark.parametrize('form', ['subject', 'named'])
def test_membership_verb_preposition_keeps_victim_rejection(description, form):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'Harbor Group is a {description}.' if form == 'subject'
             else f'A {description} {form} Harbor Group posted a notice.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


# A defender word counts only as the head of the member or object noun phrase, or as a
# modifier of a member noun; one that modifies any other head is an attributed adversary
# activity, which still counts as an adversarial identity.
@pytest.mark.parametrize('description', [
    'ransomware group that runs a bug bounty program',
    'hacking group that ran a conference-themed phishing campaign',
    'hacking group that hosts a student data leak',
    'hacking group that held a university hostage',
    'hacking group of students-turned-criminals',
    'hacking group that consists of former Conti members',
    'hacking group that is made up of Russian speakers',
    'hacking group that consists mostly',
    'hacking group that targets university researchers',
    'hacking group that is run by criminals and a ransomware crew',
])
def test_defender_word_modifying_another_head_preserves_dual_role(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is a {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim


def test_former_students_are_still_student_members():
    # "including former students" names membership by student status, as "of students" does;
    # it stays fail-closed.
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Harbor Group is a hacking group including former students.']) == 'victim organization'


@pytest.mark.parametrize('claim', [
    # Vault differential on !1275: an adverb in the verb chain is not a copula complement.
    'Earth Yako is a threat actor that has been actively targeting researchers in academic '
    'organizations and think tanks in Japan',
    'Harbor Group is a hacking group that is actively recruiting students',
    'Harbor Group is a hacking group that was reportedly exposed by university researchers',
])
def test_adverb_in_the_verb_chain_preserves_dual_role(claim):
    from okengine.actor_identity import actor_source_identity_error
    name = claim.split(' is a ')[0]
    assert actor_source_identity_error(name, [
        f'{name} suffered a breach.', claim]) is None, claim


@pytest.mark.parametrize('description', [
    'hacking group who are mostly students', 'hacking group that is largely composed of students',
])
def test_adverb_before_a_membership_complement_keeps_victim_rejection(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is a {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


def test_hyphenated_member_head_is_judged_by_its_last_part():
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Harbor Group is a hacking group of student-hackers.']) == 'victim organization'


def test_each_coordinated_object_phrase_is_judged_on_its_own():
    # The bug bounty program's defender word does not carry over to the next phrase.
    from okengine.actor_identity import actor_source_identity_error
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.',
        'Harbor Group is a ransomware group that runs a bug bounty program and recruits hackers.',
    ]) is None


# QA re-verify of !1275: the member-head list is an allowlist, so a person head it misses
# fails open. These were victim at 4062b926.
@pytest.mark.parametrize('description', [
    'hacking group of academics', 'hacking group of university staff',
    'hacking group of CTF veterans', 'hacking group of bug bounty participants',
])
@pytest.mark.parametrize('form', ['subject', 'named'])
def test_defender_person_heads_keep_victim_rejection(description, form):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'Harbor Group is a {description}.' if form == 'subject'
             else f'A {description} {form} Harbor Group posted a notice.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


def _plural(word):
    """The regular English plural, independent of the module's own singular rule."""
    if word.endswith('y') and word[-2:-1] not in ('a', 'e', 'i', 'o', 'u'):
        return word[:-1] + 'ies'
    return word + 's'


def _person_heads():
    # Derived from the source list, in singular and plural form, so a new head or a missing
    # plural is always exercised.
    from okengine.actor_identity import _SOURCE_ROLE_MEMBER_HEADS
    heads = sorted(_SOURCE_ROLE_MEMBER_HEADS)
    return heads, [_plural(head) for head in heads]


def _defender_words():
    from okengine.actor_identity import _SOURCE_ROLE_DEFENDERS
    return sorted(_SOURCE_ROLE_DEFENDERS)


def test_person_head_plurals_are_generated_independently():
    assert [_plural(w) for w in ('community', 'society', 'crew', 'day', 'staff')] == [
        'communities', 'societies', 'crews', 'days', 'staffs']


# One case per defender word and head form keeps each inside the unit lane's per-test budget.
@pytest.mark.parametrize('form', [0, 1], ids=['singular', 'plural'])
@pytest.mark.parametrize('defender', _defender_words())
def test_every_defender_word_before_every_person_head_keeps_victim_rejection(defender, form):
    # A vocabulary gap in either list shows up here as a red test, not in review.
    from okengine.actor_identity import actor_source_identity_error
    missed = []
    for head in _person_heads()[form]:
        claim = f'Harbor Group is a hacking group of {defender} {head}.'
        if actor_source_identity_error('Harbor Group', [
                'Harbor Group suffered a breach.', claim]) != 'victim organization':
            missed.append(claim)
    assert not missed, missed


# QA round 3 of !1275: plural group heads failed open. These were victim at 4062b926 and
# b1ad62ae.
@pytest.mark.parametrize('description', [
    'student teams', 'university clubs', 'student societies', 'student crews',
    'university groups', 'ctf groups', 'ethical crews', 'white-hat crews',
    'researcher collectives',
])
@pytest.mark.parametrize('form', ['subject', 'named'])
def test_plural_defender_group_heads_keep_victim_rejection(description, form):
    from okengine.actor_identity import actor_source_identity_error
    claim = (f'Harbor Group is a hacking group of {description}.' if form == 'subject'
             else f'A hacking group of {description} {form} Harbor Group posted a notice.')
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'hacking group of universities', 'hacking group of hackathons',
    'hacking group that runs CTFs', 'hacking group that hosts conferences',
])
def test_plural_defender_heads_keep_victim_rejection(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is a {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) == 'victim organization', claim


@pytest.mark.parametrize('description', [
    'hacking group of former Conti operators', 'hacking group of student operators',
    'hacking group of former Conti crew', 'ransomware group that runs a bug bounty program',
    'hacking group of Russian-speaking group members', 'hacking group of affiliate crews',
    'hacking group of ransomware crews', 'hacktivist collective of hacktivist groups',
    'hacking group of hacker communities', 'hacking group of former Conti operators',
])
def test_new_member_heads_do_not_reopen_adversary_forms(description):
    from okengine.actor_identity import actor_source_identity_error
    claim = f'Harbor Group is a {description}.'
    assert actor_source_identity_error('Harbor Group', [
        'Harbor Group suffered a breach.', claim]) is None, claim
