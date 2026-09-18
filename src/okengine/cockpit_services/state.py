from __future__ import annotations

import datetime
import os
import re
import shutil
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_WIKILINK = re.compile(r"\[\[\s*([^\]|#\n\\]*)(?:#([^\]|\n]+))?(?:\\?\|\s*([^\]\n]+?))?\s*\]\]")

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

_H1_RE = re.compile(r"^#\s+.*$", re.MULTILINE)

TODAY = datetime.date.today

_OPEN_STATUS = {
    s.strip().lower()
    for s in os.environ.get("OKENGINE_PREDICTION_OPEN_VALUES", "open,active").split(",")
    if s.strip()
}

_DEFAULT_TABS = ["home", "briefings", "predictions", "dashboards"]

_TRACKER_TABS = ("watchlist", "competitors")

_DISPLAY_ACRONYMS = {
    "ai": "AI",
    "ml": "ML",
    "llm": "LLM",
    "api": "API",
    "apis": "APIs",
    "id": "ID",
    "ids": "IDs",
    "url": "URL",
    "urls": "URLs",
    "uri": "URI",
    "ip": "IP",
    "dns": "DNS",
    "os": "OS",
    "ui": "UI",
    "ux": "UX",
    "http": "HTTP",
    "https": "HTTPS",
    "html": "HTML",
    "css": "CSS",
    "json": "JSON",
    "yaml": "YAML",
    "xml": "XML",
    "csv": "CSV",
    "pdf": "PDF",
    "sql": "SQL",
    "cpu": "CPU",
    "gpu": "GPU",
    "iot": "IoT",
    "saas": "SaaS",
    "faq": "FAQ",
    "kpi": "KPI",
    "kpis": "KPIs",
    "roi": "ROI",
}

_CFG_CACHE: tuple[float, dict | None] = (float("-inf"), None)

_CFG_TTL = 120.0

_MD_LOCAL_LINK = re.compile(r"(?<!\!)\[([^\]\n]+)\]\((?!https?://|mailto:|#)[^)\n]*\)")

_EMBED = re.compile(r"!\[\[\s*([^\]\n#|]+?)\s*(?:#[^\]\n|]+)?(?:\|[^\]\n]+)?\s*\]\]")

_EMBED_DIRS = (
    "operational",
    "dashboards",
    "marketing",
    "dailies",
    "briefings",
    "predictions",
    "reports",
)

_EMBED_PATH_CACHE: dict = {}

_SRC_LINK = re.compile(r'<a class="wl" data-page="(sources/[^"]+)">([^<]*)</a>')

_UNCODE_WIKILINK = re.compile(r"`(\[\[[^`]+?\]\])`")

_MARP = shutil.which("marp")

_DECK_CACHE = Path(
    os.environ.get("OKENGINE_DECK_CACHE", str(Path(tempfile.gettempdir()) / "okengine-deck-cache"))
)

_EV_PREFIX_RE = re.compile(r"^\s*\[(\d{4}-\d{2}-\d{2})(?:\s+([^\]]+?))?\]\s*(.*)$", re.S)

_EV_DIR_LEGACY = {
    "reinforce": "reinforces",
    "supports": "reinforces",
    "support": "reinforces",
    "confirms": "reinforces",
    "confirm": "reinforces",
    "up": "reinforces",
    "contradict": "contradicts",
    "refutes": "contradicts",
    "refute": "contradicts",
    "weakens": "contradicts",
    "down": "contradicts",
    "mixed": "partial",
    "regrade": "neutral",
    "note": "neutral",
    "context": "neutral",
}

_EV_DIR_FALLBACK = frozenset({"reinforces", "contradicts", "partial", "neutral"})

PREDICTION_CONFIDENCE_SCALE = {
    "very-low": 0.1,
    "low": 0.25,
    "medium-low": 0.375,
    "medium": 0.5,
    "medium-high": 0.625,
    "high": 0.75,
    "very-high": 0.9,
}

_DIR_CACHE: dict[str, tuple[float, list[dict]]] = {}

_DIR_TTL = 120.0

_DIR_LOCK = threading.Lock()

_DIR_REFRESHING: set[str] = set()

_ACTIVE_REQUESTS = 0

_ACTIVE_REQUESTS_LOCK = threading.Lock()

_POST_READY_WARM_DELAY = float(os.environ.get("OKENGINE_COCKPIT_WARM_DELAY_SECONDS", "1") or 0)

_POST_READY_WARM_GAP = float(os.environ.get("OKENGINE_COCKPIT_WARM_GAP_SECONDS", "0.25") or 0)

_POST_READY_IDLE_POLL = 0.05

_NUMISH_CELL = re.compile(r"\d{4}-\d{2}-\d{2}|-?\d[\d.,]*%?|[—-]")

_TONES = ("crit", "warn", "ok", "info", "mut", "acc")

_UM_FLAG = (
    ' <span class="um-flag" title="unmapped value — no label configured (okengine#188)">⚠</span>'
)

_UNMAPPED_KEY = "__unmapped__"

_assessment_terminal_cache: tuple[float, dict[str, dict], dict[str, dict]] = (float("-inf"), {}, {})

_VIEW_DEFAULT_LIMIT = {"cards": 12, "table": 10, "coverage": 10}

_TID_FACETS = [
    "relevance",
    "observable-requirements",
    "telemetry-source",
    "telemetry-fields",
    "strategy",
    "repository-analytic",
    "deployed-revision",
    "current-validation",
    "signal-path",
    "operational-effectiveness",
]

_TID_STATES = {
    "unassessed",
    "not-applicable",
    "unknown",
    "gap",
    "partial",
    "validated",
    "degraded",
    "stale",
    "covered",
    "error",
    "active",
    "disabled",
    "passed",
    "failed",
    "inconclusive",
    "enabled",
    "present",
    "approved",
    "draft",
    "retired",
    "resolved",
    "superseded",
    "improved",
    "unchanged",
    "not-run",
    "not-required",
    "pending",
    "rejected",
    "proposed",
    "deferred",
    "accepted-risk",
    "completed",
}
_TID_PRIORITY_WEIGHT = {"critical": 400, "high": 300, "medium": 200, "low": 100}

_DOC_INLINE_CAP = 65536


class _MetaValues(dict):
    """Keep a malformed pack-owned meta template from taking down an entire cockpit tab."""

    def __missing__(self, key):
        return "{" + key + "}"


_DRILL_CAP = 300

_DRILL_SUMMARY_FIELDS = (
    "summary",
    "description",
    "claim",
    "reason",
    "judgment",
    "rationale",
    "consequence",
)

_DRILL_FALLBACK_FIELDS = (
    ("published", "Published", True),
    ("news_last_seen", "Seen", True),
    ("last_seen", "Last seen", True),
    ("first_seen", "First seen", True),
    ("due_date", "Due", True),
    ("date_added", "Added", True),
    ("resolves_by", "Resolves", True),
    ("as_of", "As of", True),
    ("updated", "Updated", True),
    ("created", "Created", True),
    ("status", "Status", False),
    ("confidence", "Confidence", False),
    ("severity", "Severity", False),
    ("publisher", "Publisher", False),
    ("source_kind", "Source kind", False),
    ("sector", "Sector", False),
)

_DATED_SERIES_RE = re.compile(r"^(.*)-(\d{4}-\d{2}-\d{2})$")

_OPS_GROUPS = [
    (
        "Health",
        [
            "dashboards/fleet-health",
            "operational/collection-health",
            "HEALTH",
            "operational/kb-health-snapshots",
            "operational/page-quality-snapshots",
            "operational/page-quality-queue",
        ],
    ),
    (
        "Conformance",
        [
            "operational/schema-conformance",
            "dashboards/schema-drift",
            "operational/schema-drift",
            "operational/deployment-validation",
            "operational/field-loss-snapshots",
            "operational/bare-name-link-normalize",
        ],
    ),
    (
        "Review & grounding",
        [
            "_review-queue",
            "dashboards/source-grounding",
            "dashboards/source-staleness",
            "operational/source-staleness",
        ],
    ),
    ("Operator", ["dashboards/operator"]),
]

_OPS_PRIORITY = {"stale": 0, "unknown": 1, "current": 2}

_RPANELS_CACHE: list = [0.0, None]

_META_PANEL_SKIP = {
    "title",
    "name",
    "type",
    "version",
    "raw",
    "needs_review",
    "sources",
    "candidate_evidence",
    "qualification_result",
    "collection_attempt",
    "score_components",
    "source_refs",
}

_META_SECONDARY = {
    "tlp",
    "created",
    "updated",
    "last_updated",
    "last_seen",
    "first_seen",
    "assembled_from",
    "tier",
    "tlp_caveat",
    "maintained_by",
    "discovered_by",
    "created_by",
    "last_modified_by",
}

_REL_RANK = {c: i for i, c in enumerate("FEDCBA")}

_SRC_REL_CACHE: tuple[float, dict] = (float("-inf"), {})

_OBS_INDEX_CACHE: tuple[float, dict] = (
    float("-inf"),
    {},
)

_ID_INDEX_CACHE: tuple[float, dict] = (
    float("-inf"),
    {},
)

_ID_INDEX_NS = ("entities", "techniques", "cves", "concepts")

_STALE_DAYS = max(0, int(os.environ.get("OKENGINE_COCKPIT_STALE_DAYS", "90")))

_THIN_CHARS = 240

_TYPE_REQ_CACHE: tuple[float, dict] = (
    float("-inf"),
    {},
)

_GROUNDING_REVIEW = re.compile(
    r"##[ \t]+Grounding check.*?(unsupported|not[- ]found|not in source|contradict)", re.S | re.I
)

_review_snapshot_cache: tuple[float, list[dict], list[dict]] = (float("-inf"), [], [])

_review_snapshot_lock = threading.Lock()

_REVIEW_SNAPSHOT_TTL = 120.0

_review_snapshot_refreshing = False


class _desc:
    """Invert ONE component of a multi-key sort.

    `reverse=True` flips the ENTIRE key, so using it to get newest-first would also invert
    the reason-priority bands (grounding would sort last) and the subject tiebreak. Wrapping
    just the one component keeps every other component ascending.
    """

    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v

    def __lt__(self, other):
        return other.v < self.v

    def __eq__(self, other):
        return isinstance(other, _desc) and self.v == other.v


_assessment_subject_cache: tuple[float, dict[str, list[dict]]] = (
    float("-inf"),
    {},
)

_assessment_subject_lock = threading.Lock()

_PDF_CSS = (
    "@page{size:A4;margin:1.8cm 1.7cm}"
    "html{font-family:'DejaVu Serif',serif;font-size:10.5pt;line-height:1.42}"
    "body{max-width:100%}"
    "h1{font-size:18pt;margin:0 0 .3em}h2{font-size:13.5pt;margin:1.1em 0 .3em}"
    "h3{font-size:11.5pt;margin:.9em 0 .2em}"
    "p,li{overflow-wrap:break-word;word-wrap:break-word}"
    "pre,code{white-space:pre-wrap;word-break:break-word;font-size:9pt}"
    "pre{background:#f5f5f5;padding:6px 8px;border-radius:4px}"
    "table{width:100%;table-layout:fixed;border-collapse:collapse;font-size:8.6pt;margin:.6em 0}"
    "th,td{border:1px solid #bbb;padding:3px 5px;vertical-align:top;"
    "overflow-wrap:break-word;word-break:break-word}"
    "th{background:#f0f0f0;text-align:left}"
    "img{max-width:100%}a{color:inherit;text-decoration:none}"
)

_DL_MIME = {
    "md": "text/markdown; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf": "application/pdf",
}

_NARRATION = re.compile(
    r"^\s*(?:"
    r"(?:checking|pulling|retrieving|searching|assessing|reviewing|looking|gathering|fetching|"
    r"scanning|querying|reading|opening|examining|compiling|cross-referencing|digging|"
    r"good|okay|alright)\b"
    r"|found (?:a|an|the|no|it|some|several|\d|pages?|entries|entit|nothing)"
    r"|(?:based on|from|according to|per|drawing on|pulling from) the vault"
    r"|here(?:'s| is| are) what"
    r"|one moment"
    r"|i (?:now|have|'ll|'ve|'m)\b"
    r"|let me\b"
    r"|now (?:pulling|checking|retrieving|searching)\b"
    r")",
    re.I,
)

_SEARCH_RANK = {
    "entities": 0,
    "concepts": 1,
    "predictions": 2,
    "weekly": 3,
    "dailies": 3,
    "briefings": 3,
    "marketing": 4,
    "questions": 4,
    "dashboards": 5,
    "reports": 6,
    "operational": 7,
    "sources": 9,
}

_BACKLINKS: dict = {"map": None, "ts": 0.0}

_BACKLINKS_TTL = max(60, int(os.environ.get("OKENGINE_BACKLINKS_TTL", "86400")))

_BL_LOCK = threading.Lock()

_BL_ARTIFACT_MAX_AGE = max(3600, int(os.environ.get("OKENGINE_BACKLINKS_MAX_AGE", "172800")))

_BL_ARTIFACT: dict = {"map": None, "mtime": None}

_RESERVED_BL_NAMES = frozenset({"HOT.md", "log.md"})

_BL_DROP_CACHE: tuple = (0.0, None)

_H1_BL = re.compile(r"^# (.+)$", re.MULTILINE)

_BL_FM = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL)

_BL_WIKI = re.compile(r"\[\[([^\]]+?)\]\]", re.DOTALL)

_BL_MD = re.compile(r"\[[^\]\n]*\]\(([^)\s]+?)\)")

_BL_FENCE = re.compile(r"^([ \t]*)(```+|~~~+)[^\n]*\n.*?^\1\2[^\n]*$", re.DOTALL | re.MULTILINE)

_BL_INLINE = re.compile(r"(`+)[^\n]*?\1")

_BL_GROUP_CAP = 12

_BROWSE_TTL = 120.0

_EXCLUDE_CACHE: tuple[float, frozenset] = (float("-inf"), frozenset())

_GROUPS_CACHE: tuple[float, list] = (float("-inf"), [])

_RAILTOP_CACHE: tuple[float, tuple] = (float("-inf"), ("", ()))

_BROWSE_CACHE: dict[str, tuple[float, list[dict]]] = {}

_SURFACED_DERIVED = frozenset({"dashboards"})

_DERIVED_TYPES = {"dashboard"}

_FM_SCAN_BYTES = 16384

_AGENT_API = os.environ.get("OKENGINE_AGENT_API", "").rstrip("/")

_AGENT_KEY = os.environ.get("OKENGINE_AGENT_KEY", "")

_AGENT_MODEL = os.environ.get("OKENGINE_AGENT_MODEL", "OKEngine Agent")

_AGENT_SYSTEM = os.environ.get("OKENGINE_AGENT_SYSTEM") or (
    "You are the OKEngine vault agent. This OKF knowledge vault is your long-term memory and "
    "the FIRST place you look for anything. Open EVERY reply with a one-line acknowledgement "
    'of what you\'re about to do (e.g. "Checking the vault for Scattered Spider…") before you '
    "call any tools, so the user gets immediate feedback. Keep it to that ONE line — do not narrate "
    'each search round ("good leads", "pulling the pages now", "good data"). Then:\n'
    "1. SEARCH THE VAULT FIRST — use your tools (search, then get_page / retrieve_context / "
    "find_references) and build your answer from those pages. Search is lexical, so it matches "
    "words not meanings: if the first query is thin, RETRY with synonyms and related terms "
    "before concluding the vault lacks it (e.g. health → medical / clinical / hospital / "
    "patient; actor → group / intrusion-set / threat-actor; ransomware → extortion). Prefer the "
    "most RECENT pages — the vault is fed continuously, so current-year material exists; lead with "
    "it and don't lean on old advisories when fresher reporting is present. Cite each page you use "
    "as a linked title — `[Page Title](path)`, e.g. `[Scattered Spider](entities/s/scattered-spider)` "
    "— never a bare file path.\n"
    "2. If the vault already covers it, answer ONLY from the vault — do not add outside or "
    "prior knowledge.\n"
    "3. If the vault is missing or thin on the topic, RESEARCH IT WITH YOUR WEB TOOLS — you "
    "have web search & scraping; use them to gather and verify facts from the open web — THEN "
    "write what you learn back into the vault with your write tools (create_entity / "
    "update_entity / append_to_section). Before writing a NEW page, first fetch an existing page "
    "of the SAME type and mirror its frontmatter field names exactly — reuse the established "
    "fields, do not invent new ones (e.g. use whatever attribution/status field that type "
    "already uses). Then tell the user which page you created or updated. The wiki must grow — "
    "every external fact you rely on gets captured so the next query finds it here.\n"
    "4. Never fabricate — and never claim you lack external access: you HAVE web search, so use "
    "it before giving up. Only call a fact unverifiable after a web search has actually failed "
    "to confirm it.\n"
    "5. Speak as the vault's own analyst, never as software. Do NOT name or describe the machinery "
    "behind you: never mention Hermes, your model or model provider, or the tools/functions you use "
    "(search, web research, retrieve_context, write tools, and the like), and do not sign a reply "
    'or report off as any "agent". Referring to THE VAULT and citing your sources is expected — '
    "describe WHAT you found and WHERE (linked page titles, web sources), never the plumbing that "
    "fetched it.\n"
    "6. Be specific and disciplined. Surface the concrete detail the pages hold — dates, CVEs, "
    "IOCs, named techniques/TTPs — not generic advice; state the time window your assessment covers "
    "and say so plainly if the freshest evidence is old. Stay within the question's scope: if you "
    "raise an adjacent but DISTINCT threat (different actor class or motivation), label it as "
    "context, don't blend it into the main assessment.\n"
    "7. Only when asked for a REPORT, BRIEFING, or DECK (not a quick question): make it a "
    "SELF-CONTAINED document that BEGINS at its title / executive summary — your search-and-pull "
    "narration must NOT appear anywhere in it. Structure it: a short impact-framed executive "
    "summary, comparison TABLES where you contrast actors/options, and a specific "
    "detection/mitigation section drawn from the vault. Keep ordinary questions concise."
)

__all__ = (
    "_FM_RE",
    "_WIKILINK",
    "_DATE_RE",
    "_H1_RE",
    "TODAY",
    "_OPEN_STATUS",
    "_DEFAULT_TABS",
    "_TRACKER_TABS",
    "_DISPLAY_ACRONYMS",
    "_CFG_CACHE",
    "_CFG_TTL",
    "_MD_LOCAL_LINK",
    "_EMBED",
    "_EMBED_DIRS",
    "_EMBED_PATH_CACHE",
    "_SRC_LINK",
    "_UNCODE_WIKILINK",
    "_MARP",
    "_DECK_CACHE",
    "_EV_PREFIX_RE",
    "_EV_DIR_LEGACY",
    "_EV_DIR_FALLBACK",
    "PREDICTION_CONFIDENCE_SCALE",
    "_DIR_CACHE",
    "_DIR_TTL",
    "_DIR_LOCK",
    "_DIR_REFRESHING",
    "_ACTIVE_REQUESTS",
    "_ACTIVE_REQUESTS_LOCK",
    "_POST_READY_WARM_DELAY",
    "_POST_READY_WARM_GAP",
    "_POST_READY_IDLE_POLL",
    "_NUMISH_CELL",
    "_TONES",
    "_UM_FLAG",
    "_UNMAPPED_KEY",
    "_assessment_terminal_cache",
    "_VIEW_DEFAULT_LIMIT",
    "_TID_FACETS",
    "_TID_STATES",
    "_TID_PRIORITY_WEIGHT",
    "_DOC_INLINE_CAP",
    "_MetaValues",
    "_DRILL_CAP",
    "_DRILL_SUMMARY_FIELDS",
    "_DRILL_FALLBACK_FIELDS",
    "_DATED_SERIES_RE",
    "_OPS_GROUPS",
    "_OPS_PRIORITY",
    "_RPANELS_CACHE",
    "_META_PANEL_SKIP",
    "_META_SECONDARY",
    "_REL_RANK",
    "_SRC_REL_CACHE",
    "_OBS_INDEX_CACHE",
    "_ID_INDEX_CACHE",
    "_ID_INDEX_NS",
    "_STALE_DAYS",
    "_THIN_CHARS",
    "_TYPE_REQ_CACHE",
    "_GROUNDING_REVIEW",
    "_review_snapshot_cache",
    "_review_snapshot_lock",
    "_REVIEW_SNAPSHOT_TTL",
    "_review_snapshot_refreshing",
    "_desc",
    "_assessment_subject_cache",
    "_assessment_subject_lock",
    "_PDF_CSS",
    "_DL_MIME",
    "_NARRATION",
    "_SEARCH_RANK",
    "_BACKLINKS",
    "_BACKLINKS_TTL",
    "_BL_LOCK",
    "_BL_ARTIFACT_MAX_AGE",
    "_BL_ARTIFACT",
    "_RESERVED_BL_NAMES",
    "_BL_DROP_CACHE",
    "_H1_BL",
    "_BL_FM",
    "_BL_WIKI",
    "_BL_MD",
    "_BL_FENCE",
    "_BL_INLINE",
    "_BL_GROUP_CAP",
    "_BROWSE_TTL",
    "_EXCLUDE_CACHE",
    "_GROUPS_CACHE",
    "_RAILTOP_CACHE",
    "_BROWSE_CACHE",
    "_SURFACED_DERIVED",
    "_DERIVED_TYPES",
    "_FM_SCAN_BYTES",
    "_AGENT_API",
    "_AGENT_KEY",
    "_AGENT_MODEL",
    "_AGENT_SYSTEM",
)
