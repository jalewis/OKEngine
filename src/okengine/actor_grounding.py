"""Ground an actor's identity in the sources it cites (okengine#1012).

``actor_identity`` classifies the text it is handed. Handed the profile a model has just written,
it lets the model attest to its own claim: "ANY.RUN is a threat group ..." passes, although the
cited report says only that ANY.RUN, a sandbox vendor, *identified* the phishkit. This module asks
the question that self-attestation cannot answer: does a cited source name this subject, and does
the source itself affirm that the subject is an adversary?

One predicate, three callers:

* the enforced write path refuses a model write that creates or retypes an actor it cannot ground
  (fail closed: an unresolvable source is not evidence);
* ``importer_guard`` reports it to deterministic direct writers, where a curated provenance label
  (``MITRE ATT&CK``, ``MISP galaxy``) is the importer's own authority and vouches by itself;
* ``corpus_audit`` measures the stored corpus with the same verdict.

Everything here is pure text. Callers resolve ``sources`` with their own page reader and hand the
documents in, so the write path and the cron fleet share the verdict without sharing a reader.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import yaml

from okengine.actor_identity import (
    _ACTOR_WORDS,
    _SOURCE_ROLE_AFFILIATION_CUES,
    _SOURCE_ROLE_CUES,
    _source_adversarial_role,
    _source_role_clauses,
    actor_identity_error,
    has_positive_actor_identity,
)
from okengine.list_scalars import split_list_scalar

#: An operator-attested curated record (okengine#980). Only a deterministic importer can set it.
CURATED_EVIDENCE_ROLE = "curated-authority"

#: Refusal reasons, stable so the detector can count them.
UNSOURCED = "unsourced"
NO_RESOLVABLE_SOURCE = "no_resolvable_source"
NAME_ABSENT = "name_absent_from_sources"
NAME_EQUALS_PUBLISHER = "name_equals_publisher"
SOURCE_DEFINES_NON_ACTOR = "source_defines_non_actor"
NOT_AFFIRMED = "no_source_affirms_actor"

# Text either side of a mention that the role checks read. The role patterns themselves stop at
# clause punctuation, so the window only bounds the cost; it does not widen what a match means.
_WINDOW = 300
_TAG = re.compile(r"<[^>]{0,400}>")
# `|` survives: a table cell boundary ends a phrase ("| Acme | ransomware group | LockBit |").
_MARKUP = re.compile(r"[`*_#>]+")
_BLANKS = re.compile(r"[^\S\n]+")
_LINE_BREAK = re.compile(r" ?\n[\s]*")
_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})
# Tracker designators (UNC2452, TA488, Storm-0558, UNK_DeadDrop ...) name an adversary cluster
# by construction: a source that cites one is naming a tracked actor.
# Real designators carry 3-5 digits (UNC2452, TA488, Storm-0558, UAC-0050; APT and FIN number
# from 1), joined without a space except UNC's common "UNC 4221" spelling, so "TA 2024" or "DEV 2"
# is not one; an UNK_ cluster is a CamelCase coinage (UNK_DeadDrop), so "UNK Labs" is not one.
_DESIGNATOR = re.compile(
    r"(?i:APT|FIN)\d{1,3}|(?i:UNC)[\s_-]?\d{3,5}"
    r"|(?i:TA|TEMP|DEV|Storm|CL-STA|CL-CRI|UAC|TAG)[-_]?\d{3,5}"
    r"|UNK[\s_][A-Z][a-z]+(?:[A-Z][a-z]+)+")
# Words an alias may share with the title without being the same name.
_GENERIC_NAME_WORDS = frozenset({
    "the", "group", "team", "gang", "crew", "actor", "actors", "threat", "apt", "ransomware",
    "operation", "operator", "operators", "cluster", "collective", "cartel", "hackers", "unit",
    "labs", "lab", "security", "research", "intelligence", "raas", "of", "and", "de",
})
# An alias is tied to the page's name in a sentence that says so: "APT28, also known as Fancy Bear".
_ALIAS_CUE = re.compile(
    r"\b(?:aka|also\s+known\s+as|known\s+as|tracked\s+as|dubbed|alias|formerly)\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
# A source reference names a page or a record. A bare phrase is a provenance label.
_REF_SHAPE = re.compile(r"[/:\\]|\.md\Z")

# An adversary acting in the source's own sentence: "LockBit claimed ...", "Scattered Spider
# targeted ...", "attributed to Lazarus". The subject may carry its kind noun ("the ShinyHunters
# group stole ..."), and an auxiliary or a hedge-free adverb may sit before the verb.
_SUBJECT_KIND = (
    r"(?:(?:ransomware|threat|hacking|hacker|cybercriminal|extortion|apt)\s+)?"
    r"(?:group|gang|crew|actors?|hackers?|operators?|affiliates?|collective|cartel)"
)
# A past form is also a passive participle, and a headline drops the copula: "Okta Breached",
# "Bank Targeted in Attack", "Firm Hacked by X" name a VICTIM. So a past form counts only when
# an object follows it, never a preposition or the end of the clause.
_AGENT_PAST = (
    r"(?:targeted|attacked|breached|hacked|compromised|exploited|deployed|abused|stole|"
    r"leaked|extorted|encrypted|infiltrated|orchestrated)"
    r"(?=\s+(?!(?:by|in|via|after|through|during|on|at|as|and|or|but|with|over|amid|from|"
    r"again|for|to|last|this|earlier)\b)[\w\"'$])"
)
# "claims" reports speech as often as it claims an attack. A claim needs something after it ("Acme
# claims." and "as Acme claims," claim nothing), and a reported clause that DENIES harm ("Acme
# claims the breach was contained") names no adversary. A leak-site claim ("Play claims Acme was
# hacked", "LockBit claims the attack was carried out by its affiliates") still does.
_AGENT_CLAIM = (
    r"(?:claims|claimed)(?=\s+(?!(?:by|in|via|after|through|during|on|at|as|and|or|but|with|over|"
    r"amid|from|again|for|to|last|this|earlier)\b)[\w\"'$])"
    r"(?!\s+(?:that\s+)?(?:[\w'-]+\s+){1,4}(?:was|were|is|are|has\s+been|had\s+been|remains?)\s+"
    r"(?:\w+\s+)?(?:contained|safe|secure|resolved|limited|unaffected|routine|false|untrue|"
    r"inaccurate|minimal|isolated|restored|fixed|patched|not|never)\b)"
)
_AGENT_VERB = (
    r"(?:(?:pleaded|pled)\s+guilty|(?:was|were)\s+(?:charged|indicted|sentenced|convicted|"
    r"arrested|extradited)|targets|attacks|breaches|hacks|compromises|exploits|deploys|"
    r"abuses|steals|leaks|extorts|(?:is|was|are|were)\s+(?:behind|responsible\s+for)|"
    rf"{_AGENT_CLAIM}|{_AGENT_PAST})"
)
_AGENT_ADVERB = r"(?:(?:also|reportedly|allegedly|recently|previously|now|then|later)\s+)?"
# Perfect tense only: "has been targeted" is passive, and names the victim.
_AGENT_AUXILIARY = r"(?:(?:has|have|had)\s+)?"
_AGENT_BY = (
    r"(?:(?:campaign|attacks?|activity|intrusions?|operations?)\s+(?:linked|tied|attributed|"
    r"connected)|(?:targeted|exploited|compromised|breached|hacked|attacked|encrypted|"
    r"extorted|deployed|abused)|attributed|claimed|carried\s+out|conducted|orchestrated|perpetrated|launched|"
    r"(?:attacks?|breach(?:es)?|intrusions?)\s+(?:launched\s+|carried\s+out\s+)?)\s*(?:to|by)\s+"
    r"(?:the\s+)?"
)
# Name-then-kind: "SilverFox APT group", "the Vect ransomware group", "Tren de Aragua gang". A
# kind noun that names an organization of any sort ("group", "operators") needs an adversarial
# cue between it and the name, so "the Volkswagen Group" and "Microsoft Threat Intelligence
# group" are not adversaries; the few kinds below say "criminal" by themselves.
_ADJACENT_CUES = _SOURCE_ROLE_CUES.union(
    _SOURCE_ROLE_AFFILIATION_CUES, {"cyber", "vishing", "smishing", "financially", "motivated"})
_ADJACENT_KINDS = frozenset({
    "group", "groups", "actor", "actors", "operation", "operator", "operators", "affiliate",
    "affiliates", "hackers", "gang", "gangs", "crew", "cartel", "syndicate", "collective",
})
_ADJACENT_FREE_KINDS = frozenset({"gang", "gangs", "crew", "cartel", "syndicate", "collective"})
# A list under an adversary heading: "ransomware groups Vect and TeamPCP", "threat actors such as
# A, B and C". The heading needs a cue for the same reason as above.
_LIST_HEAD = re.compile(
    r"\b(?:groups|gangs|actors|operators|crews|cartels|collectives)"
    r"\s*(?:,\s*)?(?:like|such\s+as|including|named|called|dubbed|:)?\s+(?P<items>[^.;:]{1,240})",
    re.I,
)
# A line break is usually a soft wrap ("Yanluowang Ransomware / Group Profile"), but it ends the
# phrase before a new record field or list item ("Victim: Acme / Ransomware group: LockBit").
_PHRASE_END = re.compile(
    r"[.;:!?,\"|(\u2013\u2014]|\s-\s|\n(?=[^\n:]{0,40}:|[-+]\s|\d+[.)]\s)")
_LIST_SPLIT = re.compile(r"\s*(?:,|\band\b|\bor\b|/)\s*", re.I)
_WIKILINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")
# `no` hedges only as an absence of evidence ("no evidence X attacked", not "no doubt X attacked").
_HEDGE = re.compile(
    r"\b(?:not|never|denied|denies|deny|if|whether|might|may|could|example|hypothetical|"
    r"falsely|wrongly|no\s+(?:evidence|indication|sign|signs|proof|confirmation)|"
    r"nothing\s+(?:suggests|indicates|shows|links|ties))\b",
    re.I,
)

# What a source calls a subject that is NOT an adversary. These name the reason for a refusal;
# the refusal itself comes from the absence of an affirmation, so they never block an actor a
# source also affirms (a dual-use brand such as a ransomware group named after its malware).
_NON_ACTOR_NOUNS = (
    r"(?:campaign|malware|implant|phish\s?kit|phishing\s+kit|technique|botnet|backdoor|loader|"
    r"stealer|trojan|exploit|vulnerability|tool|toolkit|framework|rat|variant|lure)"
)
# What a source DEFINES as software: "malware dubbed RedLine", "Cobalt Strike, a commercial
# penetration testing tool", "Lumma is an infostealer". Software is the grammatical subject of the
# same verbs an operator is ("RedLine steals", "Cobalt Strike deploys"), so for a subject the source
# itself defines as software those verbs are not evidence of an adversary. Only a definition counts:
# "the Sandworm wiper" or "a LockBit variant" names what a GROUP made. `ransomware` is never read:
# a ransomware brand is routinely its operators' name ("LockBit ransomware claimed ...").
_SOFTWARE_NOUNS = (
    r"(?:malware|stealer|infostealer|loader|trojan|rat|backdoor|botnet|implant|beacon|tool|toolkit|"
    r"framework|variant|strain|payload|dropper|worm|wiper|spyware|rootkit|miner|kit)"
)
_ORGANIZATION_NOUNS = (
    r"(?:hosting\s+provider|provider|cdn|content\s+delivery\s+network|marketplace|forum|"
    r"registry|vendor|company|firm|sandbox|platform|service|non-profit|nonprofit|agency|"
    r"researcher|research\s+team|project|exchange|university)"
)


@dataclass(frozen=True)
class SourceDocument:
    """One resolved source page: what it is called, what it says, and whether it is curated."""

    ref: str
    title: str
    body: str
    #: An attested curated record or an imported reference row (see :func:`source_document`).
    curated: bool = False


@dataclass(frozen=True)
class GroundingFailure:
    """Why the cited sources do not establish the subject as an adversary."""

    code: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if isinstance(item, str) and str(item).strip()]


#: The list fields read here (``aliases``, ``sources`` and their legacy singulars).
_LIST_FIELDS = frozenset({"aliases", "alias", "sources", "source"})


def _field_strings(fm: Mapping[str, Any], field: str) -> list[str]:
    """A field's values. A legacy SCALAR in a list field is the delimited list the write path
    coerces it to (``aliases: APT1, Comment Crew`` is two names), so the raw-page graders read
    the stored shape the way the write path and the id-index do; a list entry is never split, and
    neither is a title or a name (okengine#1032 [23])."""
    value = fm.get(field)
    if field in _LIST_FIELDS and isinstance(value, str):
        return split_list_scalar(value)
    return _strings(value)


def _names(fm: Mapping[str, Any], fields: tuple[str, ...], seen: set[str]) -> list[str]:
    names: list[str] = []
    for field in fields:
        for value in _field_strings(fm, field):
            key = _key(value)
            if key and key not in seen:
                seen.add(key)
                names.append(value)
    return names


def actor_names(fm: Mapping[str, Any], slug: str = "") -> list[str]:
    """What the page IS called: title, then name, without repeats.

    A page with neither is still called something (okengine#1020: 998 actor pages on one live
    vault carry only ``aliases``, or nothing but their file name). Its aliases stand in for the
    name, and failing those its ``slug`` (``lazarus-group`` reads as "lazarus group"), so the
    guard looks for what the page is actually called instead of refusing it as nameless.
    """
    names = _names(fm, ("title", "name"), set()) or _names(fm, ("aliases", "alias"), set())
    if names:
        return names
    spelled = " ".join(_TOKEN.findall(str(slug or "")))
    return [spelled] if spelled else []


def actor_aliases(fm: Mapping[str, Any]) -> list[str]:
    """The page's aliases that are not already its title or name."""
    return _names(fm, ("aliases", "alias"), {_key(name) for name in actor_names(fm)})


def source_entries(fm: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Split a page's ``sources`` (or legacy ``source``) into page references and labels.

    A reference is shaped like a path, an id or a URL; a provenance label is a bare phrase such
    as ``MITRE ATT&CK``. References are normalized the way the write path resolves them.
    """
    field = "source" if fm.get("sources") is None else "sources"
    refs: list[str] = []
    labels: list[str] = []
    for value in _field_strings(fm, field):
        text = value.strip("[]").strip()
        if _REF_SHAPE.search(text):
            refs.append(text.strip("/").removeprefix("wiki/").removesuffix(".md"))
        else:
            labels.append(text)
    return refs, labels


def source_document(ref: str, source_fm: Mapping[str, Any], body: str) -> SourceDocument:
    """Build a document from a resolved source page, as the write path's victim check reads it."""
    title = str(source_fm.get("title") or "").strip()
    heading, separator, remainder = str(body or "").lstrip().partition("\n")
    if title and separator and re.fullmatch(r"#\s+" + re.escape(title) + r"\s*", heading, re.I):
        body = remainder
    # Only `evidence_role` marks a curated record: it is operator-owned (attestation.py), so no
    # MCP write can set it. `source_kind: reference-data` is not, so a model could otherwise
    # turn a news page into a "curated record" with one update (okengine#1012 QA B3).
    role = str(source_fm.get("evidence_role") or "").strip().casefold()
    return SourceDocument(
        ref=ref,
        title=title,
        body=str(body or ""),
        curated=role == CURATED_EVIDENCE_ROLE,
    )


SourceReader = Callable[[str], Optional[SourceDocument]]


def resolve_sources(fm: Mapping[str, Any], read: SourceReader
                    ) -> tuple[list[SourceDocument], list[str], list[str]]:
    """A page's cited sources: (resolved documents, provenance labels, unresolved references)."""
    refs, labels = source_entries(fm)
    documents: list[SourceDocument] = []
    unresolved: list[str] = []
    for ref in refs:
        found = read(ref)
        if found is None:
            unresolved.append(ref)
        else:
            documents.append(found)
    return documents, labels, unresolved


def ground_page(fm: Mapping[str, Any], read: SourceReader, *, fallback_name: str = "",
                labels_vouch: bool = False,
                non_actor_names: Iterable[str] = ()) -> Optional[GroundingFailure]:
    """:func:`ground_actor_identity` for a stored or proposed page, resolving with ``read``."""
    documents, labels, unresolved = resolve_sources(fm, read)
    names = actor_names(fm, fallback_name)
    return ground_actor_identity(names, documents, publisher=fm.get("publisher"), labels=labels,
                                 unresolved=unresolved, labels_vouch=labels_vouch,
                                 aliases=actor_aliases(fm), non_actor_names=non_actor_names)


def name_key(value: Any) -> str:
    """A name folded the way grounding compares names: "ANY.RUN" and "any run" are one key."""
    return _key(value)


def _compact(value: Any) -> str:
    """A name with case, spacing and separators folded away: "Fancy Bear" = "FancyBear"."""
    return _key(value).replace(" ", "")


def name_keys(fm: Mapping[str, Any]) -> set[str]:
    """Every name a page answers to (title, name, aliases), folded so that a RESPELLING (case,
    spaces, hyphens, dots) is the same name: "Fancy Bear" = "FancyBear", "APT28" = "APT 28".

    A legacy scalar `aliases: APT1, Comment Crew` holds the same names as the list the write path
    canonicalizes it to, so a comma separates names here: an echo of a legacy shape is no rename."""
    return {_compact(part) for field in ("title", "name", "aliases", "alias")
            for value in _strings(fm.get(field)) for part in value.split(",")} - {""}


def rename_split(prev_fm: Mapping[str, Any], fm: Mapping[str, Any],
                 slug: str = "") -> tuple[list[str], list[str]]:
    """``(new, kept)``: the names ``fm`` answers to that ``prev_fm`` did not, and the ones it keeps.

    Renaming a stored actor can repurpose it as a different subject ("LockBit" retitled "ANY.RUN"),
    so the NEW names are judged (okengine#1014). Dropping or respelling a name is no rename, and a
    page always answered to its ``slug``: titling `lazarus-group` "Lazarus Group" is not one. A
    page that had no other name keeps its slug (spelled out) among the kept names."""
    before = name_keys(prev_fm) | {_compact(slug)}
    new: list[str] = []
    kept: list[str] = []
    for value in actor_names(fm) + actor_aliases(fm):
        known = all(_compact(part) in before for part in value.split(",") if _compact(part))
        (kept if known else new).append(value)
    spelled = " ".join(_TOKEN.findall(str(slug or "")))
    # A page with no title or name is called by its slug too, even when it keeps aliases
    # ("lyceum" + HEXANE): "Lyceum APT" extends the slug (overblock QA of !1297).
    if spelled and not _names(prev_fm, ("title", "name"), set()) and _compact(spelled) not in {
            _compact(value) for value in kept}:
        kept.append(spelled)
    return new, kept


def renamed(prev_fm: Mapping[str, Any], fm: Mapping[str, Any], slug: str = "") -> bool:
    """The write gives the page a name it did not answer to before (see :func:`rename_split`)."""
    return bool(name_keys(fm) - name_keys(prev_fm) - {_compact(slug)})


# The words that tie one name to another in a source: "X (aka Y)", "X, also known as Y",
# "X (formerly Y)", "X, tracked as Y".
_TIE_CUE = (r"(?:aka|a\.k\.a\.?|also\s+known\s+as|known\s+as|also\s+tracked\s+as|tracked\s+as|"
            r"formerly(?:\s+known\s+as)?|previously\s+(?:known|tracked)\s+as|alias|dubbed)")
# Other names that may sit in the same gloss or list before the one sought ("aka A, B and Y",
# "APT-C-35 / Origami Elephant"); a dot inside a name ("TEMP.Periscope") does not end the list.
_TIE_ITEM = r"(?:[^(),.;:/]|\.(?=\S)){1,60}?"
_TIE_SEP = r"(?:,|/|\band\b|\bor\b)"
_TIE_LIST = rf"(?:{_TIE_ITEM}{_TIE_SEP}\s*){{0,8}}"
# A kind word may sit between a name and its gloss: "DoNot APT group (aka ...)".
_TIE_KIND = r"(?:\s+(?:apt|group|team|gang|crew|ransomware|actor|cluster)){0,2}"


_FULLWIDTH_PARENS = str.maketrans({"\uff08": "(", "\uff09": ")"})


def alias_tied(name: str, alias: str, text: str) -> bool:
    """A source binds ``alias`` to ``name`` itself, not to another subject in the sentence.

    The tie must start AT one of the two names: "Earth Preta (aka Mustang Panda, Bronze
    President)", "Mustang Panda (Earth Preta)", "Earth Preta, also known as Mustang Panda",
    "Rare Werewolf / Librarian Ghouls". Two names glossed together are siblings: "Gamaredon (aka
    Shuckworm, Armageddon, Primitive Bear)" ties Primitive Bear to Shuckworm. In "Detailed
    CrowdStrike analysis of LIMINAL PANDA (formerly LightBasin)" the gloss belongs to Liminal
    Panda, so the reporting vendor is tied to nothing (QA of !1297).
    """
    if not _name_pattern(name) or not _name_pattern(alias):
        return False
    first, second = _name_pattern(name).pattern, _name_pattern(alias).pattern
    text = text.translate(_FULLWIDTH_PARENS)
    gloss = rf"\(\s*{_TIE_LIST}(?:{_TIE_CUE}\s*:?\s*)?{_TIE_LIST}"
    for x, y in ((first, second), (second, first)):
        if re.search(
                rf"{x}{_TIE_KIND}\s*{gloss}{y}[^()]{{0,120}}\)"
                rf"|{x}{_TIE_KIND}\s*,?\s*{_TIE_CUE}\s*:?\s*{_TIE_LIST}{y}"
                rf"|{x}\s*/\s*{y}"
                # siblings inside one gloss or alias list
                rf"|{gloss}{x}{_TIE_KIND}\s*{_TIE_SEP}\s*{_TIE_LIST}{y}[^()]{{0,120}}\)"
                rf"|{_TIE_CUE}\s*:?\s*{_TIE_LIST}{x}{_TIE_KIND}\s*{_TIE_SEP}\s*{_TIE_LIST}{y}",
                text, re.I):
            return True
    return False


def shares_distinctive_word(name: str, other: str) -> bool:
    """The two names share a word that is not a generic kind ("Wekby" in "Wekby Group"). Only
    safe once the caller has judged every name the deployment holds as a non-actor."""
    words = set(_key(other).split()) - _GENERIC_NAME_WORDS
    return bool(set(_key(name).split()) & words)


# The words that may EXTEND a kept name and leave it the same actor: a kind word ("Wekby Group",
# "Lyceum APT", "Krybit ransomware", "Medusa Ransomware Gang", "Kraken RaaS") or a numeral. Unlike
# _GENERIC_NAME_WORDS it holds no organization word: "Labs", "Security" or "Inc" make the name a
# company, and "Stealer" or "Loader" make it software (okengine#1030).
_EXTENSION_WORDS = frozenset({
    "the", "of", "and", "de", "del", "el", "la", "al", "group", "groups", "team", "gang", "crew", "actor", "actors", "threat",
    "apt", "ransomware", "raas", "operation", "operator", "operators", "cluster", "collective",
    "cartel", "syndicate", "hackers", "unit",
    "one", "two", "three", "four", "five", "ii", "iii", "iv", "vi", "vii", "viii", "ix",
})


def extends_name(name: str, kept: str) -> bool:
    """``name`` is ``kept`` extended only by kind words, numerals or tracker designators.

    A shared distinctive word ties a new name to a kept one ("Wekby Group" is Wekby) only when
    nothing else in it names a different subject. "Liminal Labs", "Panda Bank", "Gypsy Stealer"
    and "Cobalt Strike" share a word with an actor but add one that makes them an organization or
    a piece of software, so they stay untied and must ground on their own (okengine#1030)."""
    if not shares_distinctive_word(name, kept):
        return False
    designators = {word for match in _DESIGNATOR.finditer(name) for word in _key(match[0]).split()}
    added = set(_key(name).split()) - set(_key(kept).split()) - designators
    return all(word in _EXTENSION_WORDS or word.isdigit() for word in added)


def untied_names(new: Sequence[str], kept: Sequence[str],
                 documents: Sequence["SourceDocument"]) -> list[str]:
    """The ``new`` names no cited source binds to a ``kept`` one (okengine#1014).

    A vendor's alias of a stored actor is not a new subject: when a cited source glosses the kept
    name with it ("Earth Preta (aka Mustang Panda) deployed ...", "X, also known as Y"), or the
    new name extends a kept one by a shared distinctive word plus only kind words or numerals
    ("Wekby Group", "Krybit ransomware"; :func:`extends_name`), it is the same actor and needs no
    affirmation of its own. Generic kind words ("group", "APT", "ransomware", "RaaS") alone tie
    nothing, and a shared word beside an organization or software word ("Liminal Labs", "Preta
    Bank", "Gypsy Stealer") ties nothing either (okengine#1030). Whatever stays untied is judged
    like a create.

    The caller MUST judge the namesakes first: a name the deployment holds as a non-actor
    ("Cobalt Strike" beside "Cobalt Group") is never tied away (QA of !1297)."""
    texts = [f"{_plain(d.title)}. {_plain(d.body)}" for d in documents]
    return [name for name in new
            if not any(extends_name(name, keep)
                       or any(alias_tied(keep, name, text) for text in texts) for keep in kept)]


_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.S)


class WikiSources:
    """Read cited source pages straight from a wiki tree, for writers outside the write path.

    A path resolves to ``<wiki>/<ref>.md``; an id-shaped reference resolves through
    ``source_ids`` (a source page's ``id`` to its path), the lookup the write path makes through
    its id index. Only pages under ``sources/`` are evidence. Results are cached per reference.
    """

    def __init__(self, wiki: Path, source_ids: Optional[Mapping[str, str]] = None):
        self.wiki = Path(wiki)
        self.source_ids = dict(source_ids or {})
        self._cache: dict[str, Optional[SourceDocument]] = {}

    def __call__(self, ref: str) -> Optional[SourceDocument]:
        if ref not in self._cache:
            self._cache[ref] = self._read(ref)
        return self._cache[ref]

    def _read(self, ref: str) -> Optional[SourceDocument]:
        rel = ref if (self.wiki / f"{ref}.md").is_file() else self.source_ids.get(ref)
        if not rel or rel.partition("/")[0] != "sources":
            return None
        try:
            text = (self.wiki / f"{rel}.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        match = _FRONTMATTER.match(text)
        try:
            fm = yaml.safe_load(match.group(1)) if match else None
        except yaml.YAMLError:
            fm = None
        if not isinstance(fm, dict):
            return None
        return source_document(rel, fm, text[match.end():])


def _key(value: Any) -> str:
    return " ".join(_TOKEN.findall(str(value or "").casefold()))


def _link_text(match: re.Match[str]) -> str:
    # [[path|Label]] and [[entities/x/y Label]] read as Label; a bare [[path]] stays a path.
    return re.sub(r"^\S*/\S*\s+", "", match.group(1))


def _plain(text: str) -> str:
    """Markup-free text. A line break survives (collapsed to one) so a phrase can end at a new
    record field (see ``_PHRASE_END``); typographic quotes become plain ones."""
    text = _WIKILINK.sub(_link_text, _TAG.sub(" ", str(text or ""))).translate(_QUOTES)
    text = _MARKUP.sub(" ", text)
    text = _LINE_BREAK.sub("\n", _BLANKS.sub(" ", text))
    return text.strip()


def _name_pattern(name: str) -> Optional[re.Pattern[str]]:
    """The name as a whole word sequence, tolerant of the separators a source spells it with.

    "ANY.RUN", "ANYRUN" and "Any Run" are one name; "Accord" is not "accordance".
    """
    tokens = _TOKEN.findall(name)
    if not tokens:
        return None
    joined = r"[\W_]{0,3}".join(re.escape(token) for token in tokens)
    return re.compile(rf"(?<![^\W_]){joined}(?![^\W_])", re.I)


def _proper(name: str, surface: str) -> bool:
    """A capitalized name is a proper noun: "the attack targeted ..." does not mention Attack."""
    head = next((ch for ch in name if ch.isalpha()), "")
    found = next((ch for ch in surface if ch.isalpha()), "")
    return not head.isupper() or found.isupper()


def _around(text: str, match: re.Match[str]) -> str:
    """The mention with up to ``_WINDOW`` characters of text either side of it."""
    return text[max(0, match.start() - _WINDOW):match.end() + _WINDOW]


def _tail_words(text: str, count: int) -> set[str]:
    """The last ``count`` words of the last clause of ``text``, casefolded."""
    clause = re.split(r"[.;:,!?]", text)[-1]
    return {word.casefold() for word in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", clause)[-count:]}


def mentions(name: str, text: str) -> list[tuple[str, str]]:
    """Each place ``text`` names ``name``: the spelling used there, and the text around it."""
    pattern = _name_pattern(name)
    if pattern is None:
        return []
    found = []
    for match in pattern.finditer(text):
        if _proper(name, match.group()):
            found.append((match.group(), _around(text, match)))
    return found


def _agent_action(surface: str, window: str) -> bool:
    named = re.escape(surface)
    for pattern in (
        rf"(?<![^\W_]){named}(?![^\W_])\s+(?:{_SUBJECT_KIND}\s+)?{_AGENT_AUXILIARY}{_AGENT_ADVERB}"
        rf"{_AGENT_VERB}\b",
        # Progressive only: "TA488 is exploiting ..." names the agent; "Zimbra is exploited" a
        # victim, so no passive participle is read after a copula.
        rf"(?<![^\W_]){named}(?![^\W_])\s+(?:is|are|was|were)\s+{_AGENT_ADVERB}"
        r"(?:(?:actively|currently|still)\s+)?(?:targeting|attacking|exploiting|deploying|"
        r"compromising|breaching|stealing|extorting|abusing|hacking|phishing)\b",
        rf"\b{_AGENT_BY}{named}(?![^\W_])",
        # A record's labelled attribution field: "Reported actor: Cl0p", "Threat actor: X".
        rf"\b(?:threat\s+|reported\s+|attributed\s+)?actor\s*:\s*{named}(?![^\W_])",
    ):
        if any(not _hedged(window, match.start())
               for match in re.finditer(pattern, window, re.I)):
            return True
    return False


def _hedged(window: str, start: int) -> bool:
    return bool(_HEDGE.search(re.split(r"[.;!?]\s", window[:start])[-1]))


def _named_kind(surface: str, window: str) -> bool:
    """The name followed by an adversary kind noun: "Vect ransomware group"."""
    for match in re.finditer(rf"(?<![^\W_]){re.escape(surface)}(?![^\W_])", window, re.I):
        # "a new China-nexus threat actor": after an indefinite article the name only modifies.
        if _tail_words(window[:match.start()], 3) & {"a", "an", "another"} or _hedged(
                window, match.start()):
            continue
        # Read only up to the end of the name's own phrase: the next sentence or field ("...hit
        # Acme. Ransomware group Qilin ...") is another subject.
        after = _PHRASE_END.split(window[match.end():])[0]
        words = [word.casefold() for word in
                 re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", after)[:3]]
        for index, word in enumerate(words):
            if word in _ADJACENT_KINDS and (index or word in _ADJACENT_FREE_KINDS):
                return True
            if word not in _ADJACENT_CUES:
                break
    return False


def _listed_member(surface: str, window: str) -> bool:
    """The name listed under an adversary heading: "ransomware groups Vect and TeamPCP"."""
    key = _key(surface)
    for match in _LIST_HEAD.finditer(window):
        # The heading's own clause, up to six words back: "an extortion pillar (producing groups
        # like ...)" and "ransomware groups ..." both carry their cue there.
        if not _tail_words(window[:match.start()], 6) & _ADJACENT_CUES or _hedged(
                window, match.start()):
            continue
        # The last item runs on into the sentence ("... and TeamPCP have formed ..."), so an
        # item counts when it IS the name or begins with it.
        items = [_key(item) for item in _LIST_SPLIT.split(match.group("items"))]
        if any(item == key or item.startswith(key + " ") for item in items):
            return True
    return False


def _calling(surface: str, window: str) -> bool:
    """Naming phrases has_positive_actor_identity does not read: "an operator calling himself X",
    "a group they are calling X", "the operator, known as X"."""
    pattern = (
        r"\b(?:actor|adversary|affiliate|cartel|crew|gang|group|operator|hacker|"
        r"threat\s+actor|intrusion\s+set)s?\b[^.;:]{0,30}?\b(?:calling\s+(?:him|her|it|them)selves?|"
        r"calling\s+(?:him|her|it)self|(?:are|is)\s+calling|known\s+as|tracked\s+as|dubbed|"
        rf"named|called|referred\s+to\s+as)\s*,?\s*[\"']?{re.escape(surface)}(?![^\W_])"
    )
    return any(not _hedged(window, match.start())
               for match in re.finditer(pattern, window, re.I))


def software_named(surface: str, text: str) -> bool:
    """The source DEFINES ``surface`` as software: "malware dubbed X", "X, a ... tool", "X is a
    ... stealer". A name merely beside a software noun is not a definition: "the Sandworm wiper"
    and "the Lazarus loader" name what a GROUP made (QA of !1297)."""
    named = rf"(?<![^\W_]){re.escape(surface)}(?![^\W_])"
    return bool(re.search(
        rf"\b{_SOFTWARE_NOUNS}s?,?\s+(?:dubbed|named|called|codenamed|known\s+as)\s+[\"']?{named}"
        rf"|{named},?\s+(?:is\s+|was\s+)?(?:a|an|the)\s+(?:[\w-]+\s+){{0,3}}?{_SOFTWARE_NOUNS}\b",
        text, re.I))


def affirms_actor(surface: str, window: str, actions: bool = True) -> bool:
    """The source's own text around the mention makes the subject an adversary.

    ``actions=False`` leaves out what the subject DOES (:func:`_agent_action`): for a subject that
    is software or a place, "RedLine steals ..." and "attributed to <country>" name a tool or an
    attribution value, not an actor (okengine#1014)."""
    if (_DESIGNATOR.fullmatch(surface) or has_positive_actor_identity(surface, window)
            or (actions and _agent_action(surface, window))
            or _named_kind(surface, window) or _listed_member(surface, window)
            or _calling(surface, window)):
        return True
    # A role clause is also a statement of what the subject does ("RedLine steals credentials").
    return actions and any(_source_adversarial_role(surface, clause)
                           for clause in _source_role_clauses(window, surface))


def source_non_actor_role(surface: str, window: str) -> Optional[str]:
    """What the source calls the subject when that is not an adversary, else ``None``."""
    kind = actor_identity_error(surface, window)
    if kind:
        return kind
    named = rf"[\"']?{re.escape(surface)}(?![^\W_])"
    checks = (
        (rf"\b(?P<kind>{_NON_ACTOR_NOUNS})\b[^.;:]{{0,60}}?\b(?:dubbed|named|called|codenamed)\s+"
         rf"{named}", None),
        (rf"\b(?:a|an)\s+{named}[\s-]+(?P<kind>campaign|lure|nexus)\b", None),
        (rf"{named},?\s+(?:a|an|the)\s+(?:[\w-]+\s+){{0,3}}?(?P<kind>{_ORGANIZATION_NOUNS})\b",
         None),
        (rf"\bresearchers?\s+(?:at|from|with)\s+{named}", "researcher"),
        (rf"{named}\s+(?:has\s+|have\s+)?(?:identified|discovered|uncovered|detected|disclosed|"
         r"observed|reported|published|analy[sz]ed)\b", "reporting organization"),
        (rf"{named}\s+(?P<kind>packages?|crates?|registry|ecosystem|repositor(?:y|ies)|"
         r"maintainers?)\b", None),
    )
    for pattern, label in checks:
        match = re.search(pattern, window, re.I)
        if match:
            return label or re.sub(r"\s+", " ", match.group("kind").lower())
    return None


def _curated_actor_record(document: SourceDocument, name: str, title: str) -> bool:
    """A curated record vouches only for the actor it is a record OF.

    Curated datasets also hold malware families, campaigns, CVEs and ransomware VICTIM rows, each
    of which names things that are not adversaries. So the record must be titled after the
    subject and declare itself an actor record in that title ("Worok -- Thai CERT actor record").
    Anything else is read like any other source: its own text must affirm the role.
    """
    return document.curated and bool(mentions(name, title)) and bool(_ACTOR_WORDS.search(
        _name_pattern(name).sub(" ", title)))


def _usable_aliases(names: Sequence[str], aliases: Sequence[str], text: str) -> list[str]:
    """The aliases that may stand for the page in ``text`` (see :func:`ground_actor_identity`)."""
    words = set().union(*(set(_key(name).split()) for name in names)) - _GENERIC_NAME_WORDS
    # A source that names the page AND another actor does not make that actor an alias: the
    # vendor that reported a campaign and the group that ran it share sentences all the time.
    sentences = _SENTENCE.split(re.sub(r"\ba\.k\.a\.?", "aka", text, flags=re.I))
    return [alias for alias in aliases if set(_key(alias).split()) & words or any(
        _ALIAS_CUE.search(sentence) and mentions(alias, sentence)
        and any(mentions(name, sentence) for name in names) for sentence in sentences)
        or any(_slashed(name, alias, text) for name in names)]


def _slashed(name: str, alias: str, text: str) -> bool:
    """"Rare Werewolf / Librarian Ghouls": two names joined by a slash are one actor's names."""
    first, second = _name_pattern(name).pattern, _name_pattern(alias).pattern
    return bool(re.search(rf"{first}\s*/\s*{second}|{second}\s*/\s*{first}", text, re.I))


def ground_actor_identity(
    names: Sequence[str],
    documents: Sequence[SourceDocument],
    *,
    publisher: Any = None,
    labels: Iterable[str] = (),
    unresolved: Iterable[str] = (),
    labels_vouch: bool = False,
    aliases: Sequence[str] = (),
    non_actor_names: Iterable[str] = (),
) -> Optional[GroundingFailure]:
    """``None`` when the cited sources establish the named subject as an adversary.

    ``names`` are what the page is called (title, name). An alias is model-written too, so it
    grounds the page only when it shares a distinctive word with a name, or in a source that
    also names the page: otherwise any actor a source mentions could be borrowed as an "alias"
    of an unrelated subject (okengine#1012 QA B1).

    A source grounds the subject when it names it (title, name or alias) and, around that
    mention, affirms the adversary role in its own words. A curated record vouches for a subject
    it names without that affirmation. ``labels_vouch`` lets a provenance label stand in for a
    source; only a deterministic importer of that curated list may pass it, because a model can
    type any label. A subject named like the page's own ``publisher`` must be affirmed: neither a
    curated record nor a label can turn a byline into an adversary.

    ``non_actor_names`` are names the deployment itself holds as something else: a pack's
    geopolitical titles, or the names of the corpus's non-actor entity pages (a `malware` page called
    RedLine). For such a name, and for a name the source calls software, what the subject does is
    not evidence (okengine#1014): only an identity statement ("X is a threat group") or another
    affirmation grounds it.
    """
    names = [name for name in names if _name_pattern(str(name))]
    aliases = [alias for alias in aliases if _name_pattern(str(alias))]
    labels = list(labels)
    unresolved = list(unresolved)
    if not names:
        return GroundingFailure(NAME_ABSENT, "the page has no name to look for")
    is_bylined = bylined(names, publisher)
    held = {_key(value) for value in non_actor_names}
    mentioned = False
    non_actor: Optional[tuple[str, str]] = None
    for document in documents:
        title = _plain(document.title)
        body = _plain(document.body)
        text = f"{title}. {body}" if title else body
        for name in names + _usable_aliases(names, aliases, text):
            found = mentions(name, text)
            if not found:
                continue
            mentioned = True
            # A headline capitalizes every word, so a name the body never repeats ("Cryptominers
            # Exploiting WebLogic RCE") is a headline word, not a subject the report describes.
            headline_only = bool(title) and not mentions(name, body)
            if not is_bylined and _curated_actor_record(document, name, title):
                return None
            actions = _key(name) not in held and not software_named(name, text)
            for surface, window in found:
                if not headline_only and affirms_actor(surface, window, actions):
                    return None
                if non_actor is None:
                    role = source_non_actor_role(surface, window)
                    if role:
                        non_actor = (document.ref, role)
    if labels and labels_vouch and not is_bylined:
        return None
    if is_bylined:
        return GroundingFailure(NAME_EQUALS_PUBLISHER,
                                f"{names[0]!r} is the page's own publisher and no source "
                                "affirms it as an adversary")
    if not documents:
        if unresolved or labels:
            return GroundingFailure(NO_RESOLVABLE_SOURCE,
                                    "cited: " + ", ".join((unresolved + labels)[:3]))
        return GroundingFailure(UNSOURCED, "the page cites no source")
    if not mentioned:
        return GroundingFailure(NAME_ABSENT, f"no cited source names {names[0]!r}")
    if non_actor is not None:
        return GroundingFailure(SOURCE_DEFINES_NON_ACTOR,
                                f"{non_actor[0]} describes it as a {non_actor[1]}")
    return GroundingFailure(NOT_AFFIRMED,
                            f"the cited sources name {names[0]!r} but none says it is an adversary")


# --- corpus signals (corpus_audit `actor_identity_ungrounded_pages`) ------------------------------

#: A headline verb or report noun in an actor's title marks an article title minted as a name.
_HEADLINE_WORDS = frozenset({
    "pleads", "pleaded", "breach", "breaches", "breached", "targets", "targeting", "exploits",
    "exploiting", "uses", "using", "campaign", "warns", "analyzing", "analysis", "attacks",
    "hackers", "profile", "report", "infects", "deploys", "abuse", "abuses",
})
_HEADLINE_MIN_WORDS = 6


def headline_title(title: str, slug: str = "") -> bool:
    """An article headline minted as an actor name: six or more words, or a headline verb."""
    words = _TOKEN.findall(str(title or "").casefold())
    slug_words = set(re.split(r"[-_]+", str(slug or "").casefold()))
    return len(words) >= _HEADLINE_MIN_WORDS or bool(
        _HEADLINE_WORDS & (set(words) | slug_words))


def nexus_label(title: str, reserved: Iterable[str] = ()) -> bool:
    """An attribution value, not an actor: a configured geopolitical title or an ``X-nexus``."""
    key = _key(title)
    return bool(key) and (key.endswith(" nexus") or key in {_key(value) for value in reserved})


def bylined(names: Sequence[str], publisher: Any) -> bool:
    """The page's own ``publisher`` is one of its names: a byline promoted to an actor."""
    key = _key(publisher) if isinstance(publisher, str) else ""
    return bool(key) and any(_key(name) == key for name in names)


def reserved_titles(schema: Mapping[str, Any]) -> list[str]:
    """The pack's ``identity_admission.actor.excluded_exact_titles`` (a list, or a mapping whose
    values are the titles), as the write path reads it."""
    try:
        configured = ((schema.get("identity_admission") or {}).get("actor") or {}).get(
            "excluded_exact_titles") or ()
    except AttributeError:
        return []
    values = configured.values() if isinstance(configured, Mapping) else configured
    if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
        return []
    return [str(value) for value in values if str(value).strip()]
