"""okengine#1005: a list field's scalar string becomes a list by the FIELD's declared shape.

A `list` field (identifiers, references, tags) splits on its commas, as okengine#196 always did,
except at a comma inside brackets, a date or a number, which would cut one item in half. A
`prose-list` field keeps the whole string as one item. The legacy-shape repair lane planned to
shred four live prose `basis` values on every comma; they are the fixtures below, verbatim.
"""
from __future__ import annotations

import pytest

from okengine.list_scalars import PROSE_LIST, coerce_list_scalar, split_list_scalar

#: Live deployment `predictions/*.md` `basis` values (a `list` field), verbatim.
LIVE_PROSE_BASIS = [
    "JadePuffer (first fully autonomous AI ransomware, week of 2026-06-30) follows historical "
    "malware commoditization patterns. Chinese open-weight LLMs (Seed2.0, GLM 5.2) erase "
    "model-capability moat. Tooling commoditization typically follows initial disclosure within "
    "weeks.",
    "White House NSPM-12 signed June 29, 2026 mandating NSS cybersecurity governance reform; "
    "structural fragmentation across NSS agencies; personal-accountability incentives for "
    "compliance; historical NSPM governance precedents",
    "CISA KEV catalog + public breach disclosures; Oracle PeopleSoft/EBS active exploitation "
    "pattern observed June 2026 (Nissan, Oracle Health); structural factors favor attacker "
    "replication",
    "Swiss media report June 30, 2026: federal cybersecurity positions at 120K+ CHF receiving zero "
    "applicants; structural barriers (language, citizenship, cost of living); NIS2-equivalent "
    "regulatory demand shock; AI skills premium widening gap",
]

#: A live `tags` value that was correctly split (a delimited token list).
LIVE_TOKEN_TAGS = "vulnerability, rce, kemp-loadmaster, uninitialized heap, pre-authentication exploit"

#: QA B1: canonical source paths over 80 characters, and long URLs, are still references.
LONG_PATHS = [
    "sources/2026/07/06/skillcloak-evasion-of-llm-agent-skill-scanners-via-encoded-payloads-hkust",
    "sources/2026/07/06/trojpix-pixel-level-trojan-triggers-in-multimodal-models-shandong-university",
]
LONG_URL = ("https://example.org/research/2026/07/06/a-very-long-path/with/many/segments/and-a-query"
            "?utm_source=feed&utm_medium=rss&id=1234567890")

#: QA B2: real alias forms, each a two-item list.
ALIASES = [
    ("Volexity, Volexity Inc.", ["Volexity", "Volexity Inc."]),
    ("Adobe Inc., Adobe Systems", ["Adobe Inc.", "Adobe Systems"]),
    ("Bluenoroff, Subgroup: Bluenoroff", ["Bluenoroff", "Subgroup: Bluenoroff"]),
    ("U.S. Cyber Command, NSA", ["U.S. Cyber Command", "NSA"]),
    ("Dr. Web, Kaspersky", ["Dr. Web", "Kaspersky"]),
    ("Yahoo!, Google", ["Yahoo!", "Google"]),
    ("And Co, Lazarus", ["And Co", "Lazarus"]),
    ("The International Organization of Very Long Names Group, IOVLNG",
     ["The International Organization of Very Long Names Group", "IOVLNG"]),
]


@pytest.mark.parametrize("basis", LIVE_PROSE_BASIS, ids=["jadepuffer", "nspm-12", "oracle", "swiss"])
def test_a_live_prose_basis_is_never_cut_inside_brackets_or_a_date(basis):
    assert split_list_scalar(basis) == [basis]


def test_the_live_comma_joined_tags_still_split():
    assert split_list_scalar(LIVE_TOKEN_TAGS) == [
        "vulnerability", "rce", "kemp-loadmaster", "uninitialized heap",
        "pre-authentication exploit"]


def test_long_reference_paths_and_urls_still_split():
    assert split_list_scalar(", ".join(LONG_PATHS)) == LONG_PATHS
    assert split_list_scalar(f"{LONG_URL}, {LONG_PATHS[0]}") == [LONG_URL, LONG_PATHS[0]]


@pytest.mark.parametrize("text,items", ALIASES, ids=[a for a, _ in ALIASES])
def test_real_alias_forms_still_split(text, items):
    assert split_list_scalar(text) == items


@pytest.mark.parametrize("text", [
    "first (a, b) second", "alpha [x, y]", "alpha {x, y}", "outer (inner [a, b], c) end",
])
def test_a_comma_inside_brackets_is_not_a_delimiter(text):
    assert split_list_scalar(text) == [text]


def test_only_the_commas_outside_brackets_split():
    assert split_list_scalar("f(x, y), g[a, b], h") == ["f(x, y)", "g[a, b]", "h"]
    assert split_list_scalar("[[entities/a]], [[entities/b]]") == ["[[entities/a]]", "[[entities/b]]"]


def test_nested_brackets_close_one_level_at_a_time():
    assert split_list_scalar("a(((b)), c), d") == ["a(((b)), c)", "d"]


def test_a_stray_closing_bracket_does_not_swallow_later_delimiters():
    assert split_list_scalar("a), b, c") == ["a)", "b", "c"]


@pytest.mark.parametrize("text", [
    "signed June 29, 2026", "on Sept. 3rd, 2025", "Dec 1, 1999", "may 5,2026", "1,000 hosts",
])
def test_a_comma_inside_a_date_or_number_is_not_a_delimiter(text):
    assert split_list_scalar(text) == [text]


def test_a_date_and_a_real_delimiter_in_one_value():
    assert split_list_scalar("June 29, 2026, rce") == ["June 29, 2026", "rce"]
    assert split_list_scalar("10, 20") == ["10", "20"], "a spaced comma between numbers delimits"


@pytest.mark.parametrize("text,items", [
    ("", []), (" , ", []), ("solo", ["solo"]), ("  solo  ", ["solo"]), ("solo,", ["solo"]),
    ("a,b,,c", ["a", "b", "c"]), ("a, , b", ["a", "b"]),
])
def test_empty_items_are_dropped_and_single_values_kept(text, items):
    assert split_list_scalar(text) == items


@pytest.mark.parametrize("text", [
    "assumes the vendor patches, and attackers move on",   # short clauses: still one item
    "a, b",
    LIVE_PROSE_BASIS[0],
])
def test_a_prose_list_scalar_is_one_item_never_split(text):
    assert coerce_list_scalar(f"  {text}  ", prose=True) == [text]


def test_an_empty_prose_list_scalar_is_no_item():
    assert coerce_list_scalar("   ", prose=True) == []


def test_a_list_scalar_is_split():
    assert coerce_list_scalar("a, b", prose=False) == ["a", "b"]


def test_the_declared_shape_name():
    assert PROSE_LIST == "prose-list"
