#!/usr/bin/env python3
"""Fail-closed, claim-scoped disposition for deterministic authority imports.

This module does not decide that a publisher is globally trustworthy. A consuming pack supplies a
strict policy for one import surface; this evaluator verifies the record is inside that scope before
returning auditable review metadata. News pages and model-derived claims should never call it.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any
from urllib.parse import urlparse


_REQUIRED_POLICY_KEYS = {
    "id", "authority", "eligible_types", "source_names", "url_hosts", "id_field",
    "id_pattern", "verified_fields",
}


def _values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _utc_second(value: str | None) -> str:
    if value:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
            return text
        raise ValueError("reviewed_at must be UTC with second precision (YYYY-MM-DDTHH:MM:SSZ)")
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def evaluate(record: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Return an explainable eligibility result; malformed/incomplete input always fails closed."""
    missing = sorted(key for key in _REQUIRED_POLICY_KEYS if not policy.get(key))
    reasons: list[str] = []
    if missing:
        return {"eligible": False, "reasons": [f"policy missing: {', '.join(missing)}"]}

    ptype = str(record.get("type") or "").strip()
    if ptype not in {str(value) for value in policy["eligible_types"]}:
        reasons.append("record type is outside policy scope")

    sources = {value.casefold() for value in _values(record.get("sources"))}
    expected_sources = {str(value).strip().casefold() for value in policy["source_names"]}
    if not sources.intersection(expected_sources):
        reasons.append("direct authority source identity is absent")

    raw_url = str(record.get(policy.get("url_field", "url")) or "").strip()
    parsed = urlparse(raw_url)
    hosts = {str(value).strip().casefold() for value in policy["url_hosts"]}
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in hosts:
        reasons.append("authority URL is absent, non-HTTPS, or outside the approved host")
    path_pattern = str(policy.get("url_path_pattern") or "")
    if path_pattern and not re.fullmatch(path_pattern, parsed.path or ""):
        reasons.append("authority URL path is outside the policy scope")

    identity = str(record.get(str(policy["id_field"])) or "").strip()
    try:
        id_ok = bool(re.fullmatch(str(policy["id_pattern"]), identity))
    except re.error:
        return {"eligible": False, "reasons": ["policy id_pattern is invalid"]}
    if not id_ok:
        reasons.append("required authority identifier is absent or malformed")

    if record.get("conflicts"):
        reasons.append("record contains unresolved conflicts")
    required_values = policy.get("required_values") or {}
    if not isinstance(required_values, dict):
        reasons.append("policy required_values must be an object")
    else:
        for field, expected in required_values.items():
            if record.get(str(field)) != expected:
                reasons.append(f"required provenance marker {field} does not match")

    return {"eligible": not reasons, "reasons": reasons}


def disposition(record: dict[str, Any], policy: dict[str, Any], *, reviewed_at: str | None = None
                ) -> dict[str, Any]:
    """Return policy approval fields, or raise when the direct-authority contract is not met."""
    result = evaluate(record, policy)
    if not result["eligible"]:
        raise ValueError("authority disposition refused: " + "; ".join(result["reasons"]))
    timestamp = _utc_second(reviewed_at)
    return {
        "needs_review": False,
        "review_state": "approved",
        "reviewed_by": f"policy:{policy['id']}",
        "reviewed_at": timestamp,
        "review_method": "authority-auto-disposition",
        "review_policy": str(policy["id"]),
        "authority": str(policy["authority"]),
        "authority_source_url": str(record.get(policy.get("url_field", "url")) or ""),
        "authority_verified_fields": [str(value) for value in policy["verified_fields"]],
    }
