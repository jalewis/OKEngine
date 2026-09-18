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
