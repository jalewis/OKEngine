"""Deterministic, evidence-text actor identity classification.

This is deliberately narrower than general entity recognition.  It answers the admission
question the engine must fail closed on: does the supplied evidence define this named subject as
an adversarial agent, or does it define it as a technique, campaign, exploit, or software object?
"""
from __future__ import annotations

import bisect
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
    # A generated title may not occur in the prose at all. An opening article
    # whose grammatical subject is explicitly a vulnerability/severity rating
    # still describes that object, rather than an adversary exploiting it.
    # A matching display-title H1 is page presentation, not the prose subject.
    # Do not discard unrelated headings or change other classification rules.
    opening_text = text
    heading, separator, remainder = str(body or "").lstrip().partition("\n")
    if separator and re.fullmatch(r"#\s+" + re.escape(name) + r"\s*", heading, re.I):
        opening_text = _plain(remainder)
    opening = re.match(
        r"(?:a|an|the)\s+(?:(?:critical|high|medium|low|severe|security|windows|"
        r"driver|kernel|new|patched|unpatched|zero-day|remote|code|execution)\s+){0,8}"
        r"(?P<kind>vulnerability|flaw|bug|severity rating|risk rating)\b"
        r"(?:\s*\([^)]{1,100}\))?\s+"
        r"(?:that|which|allows|permits|affects|enables|has|had|was|is|poses|indicates)\b",
        opening_text,
        re.I,
    )
    if opening:
        # A vulnerability-led introduction can still explicitly identify the
        # named adversary later. Require an affirmative sentence subject, not
        # a nearby group noun, a negation, or a hypothetical subordinate clause.
        affirmative = re.search(
            rf"(?:^|[.;]\s*){re.escape(name)}\s+(?:is|was)\s+(?:a|an|the)\s+"
            rf"(?:(?:threat|cybercriminal|criminal|adversarial|ransomware|intrusion)\s+){{0,3}}"
            rf"{_ACTOR_KIND}\b",
            text,
            re.I,
        )
        active_agent = re.search(
            rf"(?:^|[.;]\s*){re.escape(name)}\s+(?:exploits?|exploited|targets?|targeted|"
            r"compromises?|compromised|deploys?|deployed|attacks?|attacked)\b", text, re.I,
        )
        named_role = re.search(
            rf"(?:^|[.;]\s*)(?:the\s+)?{_ACTOR_KIND}\s+(?:named|called|tracked as|known as)\s+"
            rf"{re.escape(name)}\s+(?:targets?|targeted|exploits?|exploited|attacks?|attacked)\b", text, re.I,
        )
        if not (affirmative or active_agent or named_role):
            return opening.group("kind").lower()

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


def is_severity_identity_title(title: str) -> bool:
    return bool(re.fullmatch(
        r"(?:critical|high|severe|medium|low|major|significant)\s+(?:threat|risk)",
        str(title or "").strip(), re.I,
    ))


def severity_source_identity_error(title: str, source_bodies: list[str]) -> str | None:
    """Reject contrary source prose for ambiguous severity-label canonical names only."""
    name = str(title or "").strip()
    if not is_severity_identity_title(name):
        return None
    contrary = None
    for body in source_bodies:
        prose = re.sub(
            r"(?m)^## Actors named in this report\s*\n(?:\s*- \[\[[^\n]+\]\]\s*\n?)*", "", body
        )
        text = _plain(prose)
        named = re.escape(name)
        affirmative = re.search(
            rf"(?:^|[.;]\s*){named}\s+(?:is|was)\s+(?:a|an|the)\s+"
            rf"(?:(?:threat|cybercriminal|criminal|adversarial|ransomware|intrusion)\s+){{0,3}}{_ACTOR_KIND}\b",
            text, re.I,
        )
        active_agent = re.search(
            rf"(?:^|[.;]\s*){named}\s+(?:exploits?|exploited|targets?|targeted|"
            r"compromises?|compromised|deploys?|deployed|attacks?|attacked)\b", text, re.I,
        )
        named_role = re.search(
            rf"(?:^|[.;]\s*)(?:the\s+)?{_ACTOR_KIND}\s+(?:named|called|tracked as|known as)\s+"
            rf"{named}\s+(?:targets?|targeted|exploits?|exploited|attacks?|attacked)\b", text, re.I,
        )
        if affirmative or active_agent or named_role:
            return None
        explicit = re.search(
            rf"\b{named}\b\s+(?:is|was|refers to|describes)\s+(?:a|an|the)\s+"
            r"(?:severity|risk|threat)\s+(?:rating|level|label|classification)\b", text, re.I,
        )
        object_sense = re.search(
            rf"\b(?:poses?|presents?|constitutes?)\s+(?:a|an|the)\s+{named}\b", text, re.I
        )
        error = actor_identity_error(name, prose)
        rating_sense = re.search(
            rf"\b{named}(?:[- ]risk)?\s+(?:ratings?|levels?|scores?|labels?|classifications?)\b", text, re.I
        )
        generic_object = re.search(rf"\b(?:a|an)\s+{named}\s+(?:to|for|of)\b", text, re.I)
        if explicit or object_sense or rating_sense or generic_object or error:
            contrary = error or "severity label"
    return contrary


def _source_role_clauses(body: str, name: str) -> list[str]:
    """Bound roles to source clauses; generated actor link lists are not evidence."""
    prose = re.sub(
        r"(?m)^## Actors named in this report\s*\n(?:\s*- \[\[[^\n]+\]\]\s*\n?)*", "", str(body or "")
    )
    # Headings describe page presentation; evaluate the complete original prose.
    prose = re.sub(r"(?m)^\s*#{1,6}\s+[^\n]*(?:\n|$)", "", prose)
    text = re.sub(r"[`*_]+", " ", prose)
    text = re.sub(r"\s+", " ", text).strip()
    # Protect punctuation inside this exact subject, rather than changing sentence grammar.
    protected = re.sub(re.escape(name), lambda m: m.group().translate(str.maketrans({
        ".": "\ue000", ";": "\ue001", "!": "\ue002", "?": "\ue003"
    })), text, flags=re.I)
    clauses = re.split(r"[.;!?]\s*|\s+(?:but|whereas|while)\s+", protected, flags=re.I)
    return [clause.translate(str.maketrans({
        "\ue000": ".", "\ue001": ";", "\ue002": "!", "\ue003": "?"
    })) for clause in clauses]


# Every token here denotes an adversary by itself when it modifies an adversary-organization
# noun (okengine#995: "all" adversary self-descriptions, surveyed from the CTI vault vocabulary).
# Topic words a defender is named after are excluded where the defender reading is common
# ("fraud team", "cybercrime unit"); a compound counts only as a whole token, so
# "ransomware-affected" or "anti-phishing" never becomes a cue through one of its parts.
_SOURCE_ROLE_CUES = frozenset({
    "cybercriminal", "cybercriminals", "cyber-criminal", "cyber-criminals", "criminal",
    "criminals", "crime", "cybercrime", "cyber-crime", "ecrime", "e-crime", "adversarial",
    "malicious", "ransomware", "ransomware-as-a-service", "raas", "threat", "threat-actor",
    "intrusion", "hacking", "hacktivist", "hacktivists", "apt", "apts",
    "extortion", "extortionist", "extortionists", "data-extortion", "data-theft",
    "espionage", "cyberespionage", "cyber-espionage", "malware", "malware-as-a-service",
    "maas", "botnet", "phishing", "phishing-as-a-service", "phaas", "carding",
    "fraudster", "fraudsters", "scam", "scammer", "scammers", "terrorist", "mercenary",
    "cyber-mercenary", "hack-for-hire", "disinformation",
})
# Affiliation cues: a state link or "hacker" says who someone is tied to, not that they are an
# adversary. Before a broad noun they describe a state-linked company or a hacker community
# ("a state-backed enterprise", "a hacker network"), so they count only before an adversary
# kind noun (QA of !1271).
_SOURCE_ROLE_AFFILIATION_CUES = frozenset({
    "nation-state", "state-sponsored", "state-backed", "state-nexus", "state-directed",
    "state-affiliated", "state-aligned", "hacker", "hackers",
})
_SOURCE_ROLE_ADVERSARY_KINDS = frozenset({
    "actor", "adversary", "collective", "crew", "gang", "group",
})
_SOURCE_ROLE_STOPS = frozenset({
    "that", "which", "who", "whom", "whose", "where", "when", "and", "or", "but",
    "whereas", "while", "although", "though", "because", "if", "whether", "is", "are",
    "was", "were", "be", "been", "being", "has", "have", "had", "does", "do", "did",
    "may", "might", "could", "can", "would", "should", "must", "will", "shall", "not",
    "never", "no", "neither", "nor", "denied", "denies", "false", "unfounded",
    "hypothetical", "example",
})
# A descriptor that relates the subject TO an adversary (a victim of, hit by, a customer of)
# rather than naming what the subject is. Prepositions and victimization participles end
# the noun phrase before any later actor noun can be borrowed.
_SOURCE_ROLE_RELATIONS = frozenset({
    "of", "by", "from", "for", "with", "without", "against", "at", "in", "on", "to", "into",
    "onto", "via", "about", "after", "before", "over", "under", "through", "toward", "towards",
    "among", "amongst", "between", "during", "within", "upon", "like", "near", "across", "per",
    "hit", "targeted", "attacked", "breached", "compromised", "affected", "impacted", "hacked",
    "infected", "struck", "victimized", "extorted", "exploited", "ransomed",
})
# Defender and community modifiers, wherever they sit in the descriptor or in the head
# noun's membership post-modifier ("a hacking group of ethical hackers", okengine#996).
_SOURCE_ROLE_DEFENDERS = frozenset({
    "ethical", "whitehat", "greyhat", "grayhat", "bounty", "ctf", "student", "students",
    "conference", "academic", "academics", "university", "hackathon", "pentest", "pentesting",
    "penetration", "pentester", "pentesters", "researcher", "researchers",
})
# Victim, target and defender/service nouns. A cue that precedes one of these modifies it
# (a ransomware negotiation team, a threat intelligence group), not an adversary.
_SOURCE_ROLE_NON_ADVERSARY = frozenset({
    "victim", "victims", "target", "targets", "customer", "customers", "client", "clients",
    "company", "intelligence", "negotiation", "negotiator", "negotiators", "defense",
    "defence", "recovery", "prevention", "response", "responder", "responders", "research",
    "security", "support", "detection", "protection",
    "mitigation", "remediation", "investigation", "forensics", "law", "legal", "insurance",
    "vendor", "awareness", "hunting", "police", "enforcement", "counter",
    *_SOURCE_ROLE_DEFENDERS,
})
# Defender compounds whose parts are not defenders by themselves ("black-hat" is not one);
# matched as a whole token or as an open compound ("white hat").
_SOURCE_ROLE_DEFENDER_COMPOUNDS = frozenset({
    "white-hat", "grey-hat", "gray-hat", "white-hats", "grey-hats", "gray-hats", "red-team",
    "blue-team", "purple-team",
})
_SOURCE_ROLE_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)
# Kind nouns that denote an adversary organization once an adversarial cue modifies them.
# Deliberately NOT unit/squad/cell/division/task force: those name the police and defender
# bodies ("a cybercrime unit") far more often than adversaries. Singular only: a bare plural
# after "is a ..." is the object of a participle ("a hospital fighting ransomware groups"),
# never the subject's own head noun.
_SOURCE_ROLE_KIND = re.compile(
    r"(?:actor|adversary|affiliate|alliance|army|cartel|cluster|collective|crew|enterprise|"
    r"gang|group|movement|network|operation|operator|organi[sz]ation|outfit|ring|syndicate|"
    r"team)(?![\w-])",
    re.I,
)
# Kinds that identify an adversary without a preceding cue.
_SOURCE_ROLE_SELF_KIND = re.compile(
    r"(?:threat[- ]actor|intrusion[- ]set|(?:initial[- ])?access[- ]broker)(?![\w-])",
    re.I,
)
_SOURCE_ROLE_WHITESPACE = re.compile(r"\s+")
_SOURCE_ROLE_ARTICLE = re.compile(r"\b(?:a|an|the)\s+", re.I)
_SOURCE_ROLE_AGENT_BY = re.compile(r"^|\bby\s+", re.I)
_SOURCE_ROLE_BREAK_MARK = re.compile(r"[,\"'()]")
# What may follow the actor-kind noun when it is the HEAD of the noun phrase: the clause end,
# a phrase-closing mark, a stop/relative word, a post-modifying preposition, a naming or
# linking participle, or an -ing participle ("operating since 2020", "targeting hospitals").
# Anything else (a possessive, a further noun such as target/victim/casualty, a
# victimization participle such as "hit by", a digit) means the kind noun only modifies a
# later head noun, so the subject is that head rather than an adversary.
_SOURCE_ROLE_HEAD_CLOSE = re.compile(r"\s*(?:$|[,:(])")
_SOURCE_ROLE_HEAD_FOLLOWERS = frozenset({
    "of", "from", "for", "with", "without", "against", "at", "in", "on", "behind", "since",
    "within", "across", "among", "amongst", "under", "over", "through", "during", "via",
    "known", "also", "tracked", "called", "named", "dubbed", "linked", "tied", "associated",
    "affiliated", "responsible", "believed", "suspected", "active", "based",
})
_SOURCE_ROLE_POSTMODIFIER_ING = re.compile(r"[^\W\d_]{2,}ing", re.UNICODE)


def _source_role_heads_phrase(rest: str) -> bool:
    """True when the actor-kind noun that ends just before ``rest`` heads its noun phrase."""
    if _SOURCE_ROLE_HEAD_CLOSE.match(rest):
        return True
    # The kind pattern forbids a word character or hyphen right after the noun, so a word
    # here is always whitespace-separated; a possessive or digit yields no token.
    token = _SOURCE_ROLE_TOKEN.match(rest.lstrip())
    if token is None:
        return False
    word = token.group().casefold()
    return (word in _SOURCE_ROLE_STOPS or word in _SOURCE_ROLE_HEAD_FOLLOWERS
            or bool(_SOURCE_ROLE_POSTMODIFIER_ING.fullmatch(word)))


def _source_role_defender_compound(folded: str, previous: str) -> bool:
    """A defender compound, as a whole token or as an open compound ("white hat")."""
    return (folded in _SOURCE_ROLE_DEFENDER_COMPOUNDS
            or f"{previous}-{folded}" in _SOURCE_ROLE_DEFENDER_COMPOUNDS)


def _source_role_descriptor_breaks(folded: str, previous: str) -> bool:
    """A token that cannot sit inside an adversary's own descriptive noun phrase."""
    return (folded in _SOURCE_ROLE_STOPS or folded in _SOURCE_ROLE_RELATIONS
            or folded in {"a", "an", "the"}
            or _source_role_defender_compound(folded, previous)
            or any(part in _SOURCE_ROLE_NON_ADVERSARY for part in folded.split("-")))


# The head noun's post-modifier says who the group IS when it is a membership phrase ("of
# ethical hackers", "consisting of students") or a relative clause whose verb constitutes,
# runs or hosts it ("that runs a conference", "who are students", "that is run by a
# university", "that consists of students"). A relative clause with any other verb ("that
# targets university researchers", "that uses conference-themed lures", "that is targeting
# students") names what the group does, so a defender noun there is its target, tool or lure.
_SOURCE_ROLE_MEMBERSHIP = frozenset({"of", "consisting", "comprising", "including"})
# Each relative word maps to where its verb may start: "whose members are students" puts the
# relative's own noun before the verb.
_SOURCE_ROLE_RELATIVES = {"that": 1, "which": 1, "who": 1, "whose": 2}
_SOURCE_ROLE_AUXILIARIES = frozenset({"has", "have", "had"})
_SOURCE_ROLE_COPULAS = frozenset({"is", "are", "was", "were", "be", "been", "being"})
_SOURCE_ROLE_CONSTITUTIVE = frozenset({
    "run", "runs", "ran", "host", "hosts", "hosted", "organize", "organizes", "organized",
    "organise", "organises", "organised", "hold", "holds", "held", "consist", "consists",
    "consisted", "comprise", "comprises", "comprised", "include", "includes", "included",
    "made", "composed", "founded", "formed", "led", "staffed",
})
# Heads that name the people or body a defender modifier describes ("ethical hackers", "CTF
# players", "a student club"). A defender word that modifies any other head is not the
# group's membership: "a bug bounty program", "a student data leak", "a university hostage".
# Singular-canonical: _source_role_known() also accepts the regular plural, so "student
# teams" and "university communities" need no second spelling. Words whose plural is not
# regular are listed as they are used (staff, personnel, faculty, alumni, people).
_SOURCE_ROLE_MEMBER_HEADS = frozenset({
    "hacker", "member", "player", "hunter", "enthusiast", "volunteer", "professional",
    "expert", "practitioner", "tester", "analyst", "engineer", "people", "team", "club",
    "community", "society", "staff", "staffer", "personnel", "faculty", "alumni", "alumnus",
    "veteran", "participant", "contributor", "recruit", "graduate", "mentor", "individual",
    "person", "folk", "crew", "group", "collective",
})
# Coordinated members stay in the phrase ("of former Conti members and ethical hackers"), and
# a passive agent starts a new noun phrase ("run by a university").
_SOURCE_ROLE_PHRASE_JOINS = frozenset({"and", "or", "by"})
_SOURCE_ROLE_POSTMODIFIER = re.compile(r'[^,:()"]*')
_SOURCE_ROLE_PARTICIPLE = re.compile(r"[^\W\d_]{2,}(?:ing|ed)", re.UNICODE)
# Adverbs carry no membership or head information, so the post-modifier scan drops them: a
# verb chain reads through them ("has been actively targeting") and so does a membership
# verb's own preposition ("consist mostly of", "who are mostly students").
_SOURCE_ROLE_ADVERB = re.compile(r"[^\W\d_]{2,}ly", re.UNICODE)


def _source_role_membership_object(words: list[str], index: int) -> int:
    """Skip a membership verb's own preposition: "consists of", "made up of"."""
    if words[index:index + 2] == ["up", "of"]:
        return index + 2
    if words[index:index + 1] == ["of"]:
        return index + 1
    return index


def _source_role_relative_object(words: list[str]) -> int | None:
    """Where a constitutive relative clause's object or complement starts, else None."""
    index = _SOURCE_ROLE_RELATIVES[words[0]]
    copula = False
    while index < len(words) and (
            words[index] in _SOURCE_ROLE_AUXILIARIES or words[index] in _SOURCE_ROLE_COPULAS):
        copula = copula or words[index] in _SOURCE_ROLE_COPULAS
        index += 1
    if index >= len(words):
        return None
    if words[index] in _SOURCE_ROLE_CONSTITUTIVE:
        return _source_role_membership_object(words, index + 1)
    # A copula's noun complement ("who are ethical hackers"), not a progressive or passive
    # participle ("that is targeting students", "which was exposed by researchers").
    if copula and not _SOURCE_ROLE_PARTICIPLE.fullmatch(words[index]):
        return index
    return None


def _source_role_known(word: str, vocabulary: frozenset[str]) -> bool:
    """A word, or the singular of its regular plural, is in a singular-canonical vocabulary.

    Only the plural suffix is removed ("communities" -> "community", "crews" -> "crew"); a
    word outside the vocabulary stays outside it, so "operators" never becomes a member.
    """
    return (word in vocabulary
            or (word.endswith("ies") and f"{word[:-3]}y" in vocabulary)
            or (word.endswith("s") and word[:-1] in vocabulary))


def _source_role_defender_head(head: str, previous: str) -> bool:
    """A noun phrase head that is itself a defender ("a conference", "white hats")."""
    return (_source_role_defender_compound(head, previous)
            or _source_role_known(head.split("-")[-1], _SOURCE_ROLE_DEFENDERS))


def _source_role_postmodifier_defender(rest: str) -> bool:
    """True when the head noun's post-modifier makes the group a defender community.

    ``rest`` follows the actor-kind noun that heads the phrase. Each member or object noun
    phrase counts when it holds a defender word and its head (its last word) is a defender or
    a member noun. The scan reads the post-modifier once, up to a phrase-closing mark, and
    ends at a stop or relation word, so it stays linear in the clause.
    """
    tail = _SOURCE_ROLE_POSTMODIFIER.match(rest).group()
    words = [word for word in (
        token.group().casefold() for token in _SOURCE_ROLE_TOKEN.finditer(tail))
        if not _SOURCE_ROLE_ADVERB.fullmatch(word)]
    if not words:
        return False
    if words[0] in _SOURCE_ROLE_RELATIVES:
        start = _source_role_relative_object(words)
        if start is None:
            return False
    elif words[0] in _SOURCE_ROLE_MEMBERSHIP:
        start = _source_role_membership_object(words, 1)
    else:
        return False
    # Sentinel join: the last phrase is judged like every other one.
    head = previous = ""
    defender = False
    for word in [*words[start:], "and"]:
        if word in _SOURCE_ROLE_PHRASE_JOINS or word in _SOURCE_ROLE_STOPS or (
                word in _SOURCE_ROLE_RELATIONS):
            if defender and (_source_role_defender_head(head, previous) or _source_role_known(
                    head.split("-")[-1], _SOURCE_ROLE_MEMBER_HEADS)):
                return True
            if word not in _SOURCE_ROLE_PHRASE_JOINS:
                return False
            head = previous = ""
            defender = False
            continue
        defender = defender or _source_role_defender_compound(word, head) or any(
            _source_role_known(part, _SOURCE_ROLE_DEFENDERS) for part in word.split("-"))
        previous, head = head, word
    return False


def _source_role_phrase_end(text: str) -> int | None:
    """Recognize an adversary noun phrase: descriptors, then a cue that modifies the kind noun.

    The cue must be the token immediately before the actor-kind noun (or part of the kind
    itself, as in "threat actor"), so a cue that modifies some other noun ("threat
    intelligence group") or sits inside a relation ("victim of ransomware group") is not an
    adversarial identity. A cued kind noun may itself modify a following kind noun ("threat
    actor group"); the phrase ends at the last one, whose head-noun check the caller applies.
    Ambiguous phrases fail closed to "not an adversary".
    """
    position = 0
    end = None
    previous = ""
    # Whether the previous token is a cue, and whether it is only an affiliation cue
    # (adversary kinds only). The two cue sets are disjoint.
    cue = affiliation = False
    while position < len(text):
        whitespace = _SOURCE_ROLE_WHITESPACE.match(text, position)
        if whitespace:
            position = whitespace.end()
        kind = _SOURCE_ROLE_SELF_KIND.match(text, position)
        if kind is None:
            kind = _SOURCE_ROLE_KIND.match(text, position)
            if kind is not None and (not cue or (
                    affiliation and kind.group().casefold() not in _SOURCE_ROLE_ADVERSARY_KINDS)):
                return None
        if kind is not None:
            # A kind noun cues a following kind noun ("threat actor group"). An affiliation
            # cue still restricts the whole chain: "a state-backed group enterprise" is not
            # an adversary.
            cue = True
            position = end = kind.end()
            continue
        if end is not None:
            return end
        token = _SOURCE_ROLE_TOKEN.match(text, position)
        if token is None:
            return None
        word = token.group().casefold()
        if _source_role_descriptor_breaks(word, previous):
            return None
        # An open compound counts as its hyphenated form ("nation state", "state sponsored").
        forms = (word, f"{previous}-{word}")
        affiliation = any(form in _SOURCE_ROLE_AFFILIATION_CUES for form in forms)
        cue = affiliation or any(form in _SOURCE_ROLE_CUES for form in forms)
        previous = word
        position = token.end()
        # Descriptor tokens must be separated by whitespace, not punctuation or quotes.
        if position < len(text) and not text[position].isspace():
            return None
    return end


def _source_reverse_role(named: str, clause: str) -> tuple[int, int] | None:
    """The first "<article> <adversary phrase> named <name>" role, in linear time.

    A role phrase cannot contain an article, so only the last article before a naming
    phrase can open it. Article, "by" and break positions are indexed once per clause
    instead of rescanning the clause prefix for every article.
    """
    articles = list(_SOURCE_ROLE_ARTICLE.finditer(clause))
    if not articles:
        return None
    starts = [article.start() for article in articles]
    # Segment starts: the clause start and the end of every explicit agent "by".
    segments = [agent.end() for agent in _SOURCE_ROLE_AGENT_BY.finditer(clause)]
    breaks = [mark.start() for mark in _SOURCE_ROLE_BREAK_MARK.finditer(clause)]
    breaks.extend(token.start() for token in _SOURCE_ROLE_TOKEN.finditer(clause)
                  if token.group().casefold() in _SOURCE_ROLE_STOPS)
    breaks.sort()
    for naming in re.finditer(
            rf"\s+(?:named|called|tracked as|known as)\s+[\"']?{named}", clause, re.I):
        # The last article before the naming phrase. With none, index -1 selects an article
        # AFTER the naming phrase, whose phrase slice is empty and so never matches.
        index = bisect.bisect_right(starts, naming.start()) - 1
        article = articles[index]
        # An explicit agent preposition starts a new noun phrase, independently
        # of an earlier report noun phrase ("The report was published by a ...").
        segment = segments[bisect.bisect_right(segments, article.start()) - 1]
        # The phrase from the segment's first article up to this article must not cross
        # punctuation, quotes or a syntax stop ("the hotel that employs a hacking group").
        earlier_end = articles[bisect.bisect_left(starts, segment)].end()
        if bisect.bisect_left(breaks, article.start()) > bisect.bisect_left(breaks, earlier_end):
            continue
        phrase = clause[article.end():naming.start()]
        if _source_role_phrase_end(phrase) == len(phrase):
            return (article.start(), naming.end())
    return None


def _source_adversarial_role(name: str, clause: str) -> bool:
    """An affirmative named adversarial subject, not a nearby malicious noun."""
    named = rf"(?<!\w){re.escape(name)}(?!\w)"
    if not re.search(named, clause, re.I):
        return False
    role = None
    subject = re.search(
        rf"{named}\s+(?:is|was|operates as|has been identified as)\s+(?:a|an|the)\s+",
        clause, re.I,
    )
    if subject:
        end = _source_role_phrase_end(clause[subject.end():])
        rest = clause[subject.end() + end:] if end is not None else ""
        if (end is not None and _source_role_heads_phrase(rest)
                and not _source_role_postmodifier_defender(rest)):
            role = (subject.start(), subject.end() + end)
    if role is None:
        role = _source_reverse_role(named, clause)
    action = re.search(
        rf"{named}\s+(?:"
        r"(?:launched|carried out|conducted)\s+(?:a|an|the)\s+(?:ransomware|malware|cyber)\s+attack|"
        r"(?:claimed|claims)\s+responsibility\s+for\s+(?:a|an|the)\s+(?:ransomware|malware|cyber)\s+attack|"
        r"(?:stole|steals|exfiltrated|exfiltrates)\s+(?:credentials|passwords|private data|confidential data)|"
        r"(?:attacked|attacks|compromised|compromises)\s+[^,;.!?]{1,80}?\s+(?:using|with)\s+(?:ransomware|malware)|"
        r"(?:deployed|deploys)\s+(?:ransomware|malware)\s+(?:against|to attack|to compromise)\b)",
        clause, re.I,
    )
    match = role or ((action.start(), action.end()) if action else None)
    if not match:
        return False
    # Denied/conditional identity claims do not turn a victim into a perpetrator.
    prefix = clause[:match[0]]
    if re.search(r"\b(?:claim|allegation|description|identity)\b.{0,30}\b(?:denied|false|unfounded)\b|\bwas denied\b", clause[match[1]:], re.I):
        return False
    return not re.search(r"\b(?:denied|denies|not|never|if|whether|might|may|could|example|hypothetical)\b", prefix, re.I)


def actor_source_identity_error(title: str, source_bodies: list[str]) -> str | None:
    """Return explicit original-source victim contradiction, preserving unknown inputs.

    Generated profile prose is intentionally not an argument. A separately cited affirmative
    adversarial role can establish a genuine dual-role identity; victim mentions alone cannot.
    """
    name = str(title or "").strip()
    if not name:
        return None
    named = rf"(?<!\w){re.escape(name)}(?!\w)"
    victim = False
    for body in source_bodies:
        for clause in _source_role_clauses(body, name):
            if _source_adversarial_role(name, clause):
                return None
            subject = re.search(
                rf"{named}\s+(?:"
                r"(?:suffered|suffers|experienced)\s+(?:a|an|the)\s+(?:data\s+)?breach|"
                r"(?:was|is|has been)\s+(?:attacked|compromised|breached)\s+by\b|"
                r"(?:was|is)\s+(?:a|the)\s+victim\s+of\b|"
                r"(?:disclosed|reported)\s+(?:a|an|the)\s+(?:data\s+)?breach\s+"
                r"(?:affecting|impacting)\s+(?:its|their)\s+customers\b)",
                clause, re.I,
            )
            patient = re.search(rf"\b(?:attacked|breached|compromised)\s+{named}(?!['’](?:s\b|\s))", clause, re.I)
            match = subject or patient
            if match and not re.search(
                r"\b(?:not|never|denied|denies|if|whether|might|may|could)\b", clause[:match.start()], re.I
            ):
                victim = True
    if victim:
        return "victim organization"
    return severity_source_identity_error(name, source_bodies)
