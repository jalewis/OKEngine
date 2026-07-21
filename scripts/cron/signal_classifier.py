#!/usr/bin/env python3
"""Pack-configured, conservative source forecasting-role classifier (#221)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CURRENT_MARKET_SIGNAL = "current-market-signal"
HISTORICAL_BASELINE = "historical-baseline"
MARKETING_POSITIONING = "marketing-positioning"
ENTITY_ENRICHMENT = "entity-enrichment"
ALL_CLASSES = (CURRENT_MARKET_SIGNAL, HISTORICAL_BASELINE,
               MARKETING_POSITIONING, ENTITY_ENRICHMENT)

DEFAULTS = {
    "freshness_days": 180,
    "material_tags": ["funding", "acquisition", "earnings", "product-launch",
                      "partnership", "exec-change", "regulation", "enforcement"],
    "material_title_patterns": [r"\bacquir(?:es?|ed|ing|ition)\b", r"\bfunding round\b",
                                r"\blaunch(?:es|ed|ing)?\b", r"\bearnings\b",
                                r"\bexecutive order\b", r"\bappoints?\b"],
    "historical_title_patterns": [r"\bretrospective\b", r"\bprimer\b", r"\bdeep dive\b",
                                  r"\bplaybook\b", r"\btradecraft\b"],
    "marketing_path_prefixes": ["marketing/"],
    "marketing_fragments": ["internal"],
}


def _values(config: dict, key: str) -> list[str]:
    value = config.get(key, DEFAULTS[key])
    return [str(v) for v in value] if isinstance(value, list) else list(DEFAULTS[key])


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _fresh(rel_path: str, fm: dict, config: dict, today: date | None) -> bool:
    today = today or datetime.now(timezone.utc).date()
    candidates = [d for d in (_date(fm.get("published")), _date(fm.get("date"))) if d]
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", Path(rel_path).stem)
    if match:
        try:
            candidates.append(date(*map(int, match.groups())))
        except ValueError:
            pass
    if not candidates:
        ingested = _date(fm.get("ingested"))
        candidates = [ingested] if ingested else []
    return not candidates or min(candidates) >= today - timedelta(
        days=int(config.get("freshness_days", DEFAULTS["freshness_days"])))


def classify(rel_path: str, fm: dict, body: str = "", *, config: dict | None = None,
             today: date | None = None) -> tuple[str, str]:
    """Return ``(class, first-matching-rule)``. Unknown content defaults to enrichment."""
    del body
    config = config or {}
    lower_path = rel_path.lower()
    title = str(fm.get("title") or Path(rel_path).stem)
    publisher = str(fm.get("publisher") or "").lower()
    fragments = [v.lower() for v in _values(config, "marketing_fragments")]
    if any(lower_path.startswith(v.lower()) for v in
           _values(config, "marketing_path_prefixes")):
        return MARKETING_POSITIONING, "marketing-path"
    if any(v in lower_path or v in publisher for v in fragments):
        return MARKETING_POSITIONING, "marketing-fragment"
    if not _fresh(rel_path, fm, config, today):
        return HISTORICAL_BASELINE, "stale"
    tags = fm.get("tags") if isinstance(fm.get("tags"), list) else []
    material = {v.lower() for v in _values(config, "material_tags")}
    if {str(v).lower() for v in tags} & material:
        return CURRENT_MARKET_SIGNAL, "material-tag"
    if any(re.search(pattern, title, re.I) for pattern in
           _values(config, "material_title_patterns")):
        return CURRENT_MARKET_SIGNAL, "material-title"
    if any(re.search(pattern, title, re.I) for pattern in
           _values(config, "historical_title_patterns")):
        return HISTORICAL_BASELINE, "historical-title"
    return ENTITY_ENRICHMENT, "default"
