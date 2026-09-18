from __future__ import annotations

import contextvars
import os
import re
from collections import Counter
from pathlib import Path

VAULT = Path(os.environ.get("WIKI_PATH") or "/opt/vault")

WIKI = VAULT / "wiki"

_FM = re.compile(r"\A---[ \t]*\n(.*?\n)---(.*)\Z", re.S)

_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)

_MAX_ENTITY_SLUG_LEN = 80

_REVIEW_STATES = {"open", "in-review", "changes-requested", "approved", "rejected", "dismissed"}

_REVIEW_DECISIONS = {
    "approve": ("approved", False),
    "request-changes": ("changes-requested", True),
    "reject": ("rejected", True),
    "dismiss": ("dismissed", False),
    "defer": ("open", True),
}

_caller_var: contextvars.ContextVar = contextvars.ContextVar("okengine_write_caller", default=None)

_policy_cache: dict = {"key": None, "value": None}

_REVIEW_MANAGED_FIELDS = {
    "review_state",
    "review_id",
    "reviewed_by",
    "reviewed_on",
    "reviewed_at",
    "reviewed_version",
}

_WIKILINK_FULL = re.compile(r"^\[\[\s*([^\]|#]+?)\s*(?:[#|][^\]]*)?\]\]$")

_FALLBACK_LIST_FIELDS = frozenset({"aliases", "tags", "maintained_by", "discovered_by"})

_base_list_fields_cache = None

_base_int_fields_cache = None

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_base_item_rules_cache = None

_RESERVED_NAMES = {
    "log.md",
    "index.md",
    "agents.md",
    "hot.md",
    "readme.md",
    "health.md",
    "bundle.md",
}

_PERM_KEYS = ("create", "update", "delete")

_OKF_ALWAYS = {
    "type",
    "name",
    "title",
    "tlp",
    "version",
    "last_updated",
    "created",
    "updated",
    "needs_review",
    "conflicts",
    "assembled_from",
    "aliases",
    "refs",
    "tags",
    "status",
    "id",
    "raw",
    "sources",
    "source",
    "producer_lane",
}

_okf_always_cache = None

QUEUE_HEADER = (
    "---\ntitle: Review Queue\n---\n\n"
    "# Review Queue\n\nAgent-flagged pages awaiting human review "
    "(highlight, not a gate — the writes already landed).\n\n"
)

_REAL_URL = re.compile(r"^https?://[^\s/]+", re.I)

_URL_SOURCE_ID = re.compile(r"^sources:url-[0-9a-f]{20}$")

_RECORD_DATE_FIELDS = ("published", "updated", "created", "last_updated")

_WIKILINK = re.compile(r"\[\[([^\]|#\n]+)")

_STRICT_LINK_NS = ("briefings",)

_LINK_REVIEW_NS = ("concepts", "entities")

_SINGULAR_SOURCE_REF = re.compile(r"^source/[a-z0-9][a-z0-9._/-]+$")

_SLUG_DESIGNATION = re.compile(r"(?:^|[-_])([a-z]{2,16})[-_](\d{3,8})(?:[-_]|$)", re.I)

_VALUE_DESIGNATION = re.compile(r"\b([a-z]{2,16})[-_ ](\d{3,8})\b", re.I)

_DEGEN_FENCE = re.compile(r"```.*?```", re.DOTALL)

_DEGEN_WIKILINK = re.compile(r"\[\[[^\]]*\]\]")

_DEGEN_STOP = re.compile(r"[.!?;:\n,]")

_DEGEN_MAX_RUN = 250

_IMMUTABLE_KEYS = ("id", "created", "created_by", "discovered_by")

_STAMP_KEYS = {"version", "last_updated", "needs_review"}

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")

_MALFORMED_HEADING_RE = re.compile(r"^##[ \t]+##(?:[ \t]+|$)", re.MULTILINE)

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

_DERIVED_PANEL_HEADINGS = {
    "incoming backlinks",
    "outbound references",
    "referenced by",
    "references",
}

_registries: dict = {}

_VAGUE_WORDS = frozenset(
    {
        "unnamed",
        "unidentified",
        "unattributed",
        "generic",
        "placeholder",
        "various",
        "multiple",
        "several",
        "suspected",
        "alleged",
        "undisclosed",
    }
)

_GENERIC_ACTOR_LABELS = frozenset(
    """
actor|actors|adversary|ai agent|ai agents|ai attacker|ai attackers|attack group|attacker|attackers
autonomous llm agent|criminal group|cyber criminals|cybercriminal|cybercriminal group|cybercriminals
hacker|hacker group|hackers|initial access broker|initial access brokers|intruder|intruders|llm agent
llm agents|malware campaign|outsider|placeholder|ransomware campaign|ransomware gang|ransomware gangs
ransomware group|threat actor|threat actor name|threat actors|threat group|unknown|unknown actor
unknown threat actor|unsafe
""".strip().replace("\n", "|").split("|")
)

_NON_ACTOR_DEFINITION = re.compile(
    r"\b(?:is|was|dubbed|described as|documented)\b.{0,100}\b"
    r"(?:backdoor|malware|implant|loader|ransomware|stealer|trojan|"
    r"phishing[- ]as[- ]a[- ]service|phishing (?:service|toolkit)|toolkit)\b",
    re.I | re.S,
)

_SOURCE_KINDS = {
    "advisory",
    "commentary",
    "community-reporting",
    "government-alert",
    "incident-report",
    "news",
    "paper",
    "post",
    "release",
    "report",
    "threat-report",
    "vendor-blog",
    "vendor-research",
}

DEFAULT_LOCAL_TOKEN = "okengine-local"

_LOOPBACK = ("127.0.0.1", "localhost", "::1")

__all__ = (
    "VAULT",
    "WIKI",
    "_FM",
    "_H1",
    "_MAX_ENTITY_SLUG_LEN",
    "_REVIEW_STATES",
    "_REVIEW_DECISIONS",
    "_caller_var",
    "_policy_cache",
    "_REVIEW_MANAGED_FIELDS",
    "_WIKILINK_FULL",
    "_FALLBACK_LIST_FIELDS",
    "_base_list_fields_cache",
    "_base_int_fields_cache",
    "_ISO_DATE_RE",
    "_base_item_rules_cache",
    "_RESERVED_NAMES",
    "_PERM_KEYS",
    "_OKF_ALWAYS",
    "_okf_always_cache",
    "QUEUE_HEADER",
    "_REAL_URL",
    "_URL_SOURCE_ID",
    "_RECORD_DATE_FIELDS",
    "_WIKILINK",
    "_STRICT_LINK_NS",
    "_LINK_REVIEW_NS",
    "_SINGULAR_SOURCE_REF",
    "_SLUG_DESIGNATION",
    "_VALUE_DESIGNATION",
    "_DEGEN_FENCE",
    "_DEGEN_WIKILINK",
    "_DEGEN_STOP",
    "_DEGEN_MAX_RUN",
    "_IMMUTABLE_KEYS",
    "_STAMP_KEYS",
    "_HEADING_RE",
    "_MALFORMED_HEADING_RE",
    "_FENCE_RE",
    "_DERIVED_PANEL_HEADINGS",
    "_registries",
    "_VAGUE_WORDS",
    "_GENERIC_ACTOR_LABELS",
    "_NON_ACTOR_DEFINITION",
    "_SOURCE_KINDS",
    "DEFAULT_LOCAL_TOKEN",
    "_LOOPBACK",
)
