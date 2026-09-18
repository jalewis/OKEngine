"""Deterministic, evidence-text actor identity classification.

This is deliberately narrower than general entity recognition.  It answers the admission
question the engine must fail closed on: does the supplied evidence define this named subject as
an adversarial agent, or does it define it as a technique, campaign, exploit, or software object?
"""
from __future__ import annotations

import re


_NON_ACTOR_KIND = (
    r"(?:application|attack technique|campaign variant|clickfix variant|exploit|implant|loader|lure|"
    r"malware|method|payload|phishing campaign|phishing kit|ransomware|service|stealer|"
    r"software(?: product)?|platform|product|technique|tool|toolkit|trojan|variant|vulnerability)"
)
_ACTOR_KIND = (
    r"(?:adversar(?:y|ies)|affiliate|cartel|collective|crew|criminal organization|gang|group|"
    r"intrusion set|operation|operator|team|threat actor)"
)
_ACTOR_WORDS = re.compile(
    r"\b(?:actor|adversary|affiliate|cartel|collective|crew|gang|group|intrusion set|operation|"
    r"operator|organization|team)\b",
    re.I,
)


def _plain(body: str) -> str:
    text = re.sub(r"[`*_#]+", " ", str(body or "")[:12000])
    return re.sub(r"\s+", " ", text).strip()


def actor_identity_error(title: str, body: str) -> str | None:
    """Return a high-confidence contradictory entity class, otherwise ``None``."""
    name = str(title or "").strip()
    text = _plain(body)
    if not name or not text:
        return None
    named = re.escape(name)

    # "A new ClickFix variant, dubbed TerminalFix" and equivalent lead-with-kind definitions.
    kind_first = re.search(
        rf"\b(?P<definition>[^.;:]{{0,100}}?\b(?P<kind>{_NON_ACTOR_KIND})\b[^.;:]{{0,50}}?)"
        rf"\s*,?\s*(?:dubbed|named|called|tracked as)\s+[\"']?{named}\b",
        text,
        re.I,
    )
    if kind_first and not _ACTOR_WORDS.search(kind_first.group("definition")):
        return str(kind_first.group("kind")).lower()

    # "TerminalFix is a ClickFix variant" / "TerminalFix was a phishing campaign".
    subject_first = re.search(
        rf"\b{named}\b\s+(?:is|was|refers to|describes)\s+"
        rf"(?P<definition>[^.;:]{{0,120}}?\b(?P<kind>{_NON_ACTOR_KIND})\b[^.;:]{{0,40}})",
        text,
        re.I,
    )
    if subject_first and not _ACTOR_WORDS.search(subject_first.group("definition")):
        return str(subject_first.group("kind")).lower()

    # Reporting about a product vulnerability often puts the product name after the actual actor:
    # "Threat actors exploit a flaw impacting JFrog Artifactory."  The title is the affected
    # object in that construction, not the agent performing the exploitation.
    affected_object = re.search(
        rf"\b(?P<kind>bug|flaw|vulnerability|weakness)\b[^.;:]{{0,50}}?"
        rf"\b(?:affecting|impacting|in)\s+(?:the\s+)?{named}\b",
        text,
        re.I,
    )
    if affected_object:
        return str(affected_object.group("kind")).lower()
    return None


def has_positive_actor_identity(title: str, body: str) -> bool:
    """Require the evidence to identify the named subject as an adversarial agent."""
    name = str(title or "").strip()
    text = _plain(body)
    if not name or not text or actor_identity_error(name, text):
        return False
    named = re.escape(name)
    return bool(
        re.search(
            rf"\b{named}\b[^.;:]{{0,100}}\b(?:is|was|are|operates as|has been identified as|"
            rf"has been described as)\b[^.;:]{{0,100}}\b{_ACTOR_KIND}\b",
            text,
            re.I,
        )
        or re.search(
            rf"\b{_ACTOR_KIND}\b\s+(?:dubbed|named|called|tracked as|known as)\s+"
            rf"[\"']?{named}\b",
            text,
            re.I,
        )
    )
