"""Deterministic v1 typed-field extraction for ``okengine.events`` (#220).

The extractor names are semantic mechanisms.  Packs map their own event page types to these names
with ``event_scoring.typed_extractors``; OKEngine never assumes a domain type vocabulary.
"""
from __future__ import annotations

import re
from typing import TypedDict


class FundingFields(TypedDict, total=False):
    amount_usd: float | None
    round: str | None
    valuation_usd: float | None
    lead_investor: str | None


class MergerAcquisitionFields(TypedDict, total=False):
    deal_value_usd: float | None
    acquirer: str | None
    target: str | None


class ProductMoveFields(TypedDict, total=False):
    launch_type: str | None
    is_general_availability: bool | None


class BuyerSignalFields(TypedDict, total=False):
    survey_n: int | None
    buyer_role: str | None
    percentage: float | None


class RegulatoryFields(TypedDict, total=False):
    deadline_date: str | None
    enforcement_amount_usd: float | None
    jurisdiction: str | None
    affected_entities: list[str]


SUPPORTED_EXTRACTORS = {"funding", "m-and-a", "product-launch", "buyer-signal", "regulatory"}
_DOLLAR = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*([KMB])(?:illion)?\b", re.I)
_ROUND = re.compile(r"\b(seed|series\s*[a-f]|pre[-\s]?ipo|ipo|secondary)\b", re.I)
_LED_BY = re.compile(r"\bled\s+by\s+([A-Z][\w&.' -]{2,50}?)(?=\s+(?:with|alongside)|[,.;]|$)", re.I)
_TO_ACQUIRE = re.compile(
    r"\b([A-Z][\w&.' -]{2,50}?)\s+(?:to acquire|acquires|has acquired)\s+"
    r"([A-Z][\w&.' -]{2,50}?)(?=\s+(?:for|in|with|on)|[,.;]|$)", re.I)
_ACQUIRED_BY = re.compile(
    r"\b([A-Z][\w&.' -]{2,50}?)\s+(?:acquired|to be acquired)\s+by\s+"
    r"([A-Z][\w&.' -]{2,50}?)(?=\s+(?:for|in|with|on)|[,.;]|$)", re.I)
_GA = re.compile(r"\b(general availability|going ga|ga (?:today|now)|now available)\b", re.I)
_BETA = re.compile(r"\b(beta|preview|early access|limited release)\b", re.I)
_SURVEY_N = re.compile(
    r"\b(?:surveyed|polled|n\s*=\s*|of\s+|across\s+)(\d{2,6})\s+"
    r"(?:respondents|leaders|professionals|practitioners|enterprises|buyers)", re.I)
_PERCENT = re.compile(r"\b(\d+(?:\.\d+)?)\s*%")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_JURISDICTIONS = [
    ("us-federal", re.compile(r"\b(SEC|FTC|federal|U\.?S\.? federal)\b", re.I)),
    ("us-state", re.compile(r"\b(California|Texas|New York|state of \w+)\b", re.I)),
    ("eu", re.compile(r"\b(EU|European Union|GDPR|NIS2|DORA)\b", re.I)),
    ("uk", re.compile(r"\b(UK|United Kingdom|FCA|ICO)\b", re.I)),
]


def _amounts(text: str) -> list[float]:
    factors = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
    return [float(m.group(1)) * factors[m.group(2).upper()] for m in _DOLLAR.finditer(text)]


def _funding(text: str) -> FundingFields:
    amounts = _amounts(text)
    round_match = _ROUND.search(text)
    led_by = _LED_BY.search(text)
    smallest = min(amounts) if amounts else None
    largest = max(amounts) if amounts else None
    return {
        "amount_usd": smallest,
        "round": re.sub(r"\s+", "-", round_match.group(1).lower()) if round_match else None,
        "valuation_usd": largest if len(amounts) > 1 and largest > 4 * smallest else None,
        "lead_investor": led_by.group(1).strip() if led_by else None,
    }


def _ma(text: str) -> MergerAcquisitionFields:
    match = _TO_ACQUIRE.search(text)
    acquirer = target = None
    if match:
        acquirer, target = match.group(1).strip(), match.group(2).strip()
    else:
        match = _ACQUIRED_BY.search(text)
        if match:
            target, acquirer = match.group(1).strip(), match.group(2).strip()
    amounts = _amounts(text)
    return {"deal_value_usd": amounts[0] if amounts else None,
            "acquirer": acquirer, "target": target}


def _product(text: str) -> ProductMoveFields:
    if _GA.search(text):
        return {"launch_type": "ga", "is_general_availability": True}
    if _BETA.search(text):
        return {"launch_type": "beta", "is_general_availability": False}
    return {"launch_type": None, "is_general_availability": None}


def _buyer(text: str) -> BuyerSignalFields:
    n = _SURVEY_N.search(text)
    pct = _PERCENT.search(text)
    role = re.search(r"\b(CISO|CIO|CTO|security engineer|SOC analyst|buyer)\b", text, re.I)
    return {"survey_n": int(n.group(1)) if n else None,
            "buyer_role": role.group(1).lower().replace(" ", "-") if role else None,
            "percentage": float(pct.group(1)) / 100 if pct else None}


def _regulatory(text: str, entities: list[str]) -> RegulatoryFields:
    deadline = _ISO_DATE.search(text)
    amounts = _amounts(text)
    jurisdiction = next((name for name, regex in _JURISDICTIONS if regex.search(text)), None)
    return {"deadline_date": deadline.group(1) if deadline else None,
            "enforcement_amount_usd": amounts[0] if amounts else None,
            "jurisdiction": jurisdiction, "affected_entities": entities}


def extract_typed_fields(extractor: str, title: str, body: str,
                         entities: list[str] | None = None) -> dict:
    """Extract the fields supported by *extractor*; unknown extractors return an empty mapping."""
    text = f"{title} {body[:2000]}"
    if extractor == "funding":
        return dict(_funding(text))
    if extractor == "m-and-a":
        return dict(_ma(text))
    if extractor == "product-launch":
        return dict(_product(text))
    if extractor == "buyer-signal":
        return dict(_buyer(text))
    if extractor == "regulatory":
        return dict(_regulatory(text, list(entities or [])))
    return {}
