"""Source-quality decay library.

Used by select_source_staleness.py and any future consumer that needs
a *current-claim credibility* score for a source. The frozen source
rating (`reliability`, `credibility` in source frontmatter) stays
correct AT THE TIME OF INGEST. This library computes the effective
score AS A SOURCE FOR CURRENT CLAIMS — which decays with age.

Half-lives by `source_kind`:
  filing      1095 days  (point-in-time facts that don't unmake themselves)
  report      547  days  (12-18 month half-life; 18mo conservative end)
  article     547  days  (same as report)
  blog        180  days  (6-month half-life)
  thread      180  days
  primary     1825 days  (5-year half-life — primary document;
                          very slow until contradicted)
  default     365  days  (unknown / unset source_kind)

Decay function: effective = base × 0.5 ** (age_days / half_life)

base is the mean of two 0-1 factors derived from the source rating:
  reliability A→F  → 1.00, 0.85, 0.70, 0.55, 0.40, 0.25
  credibility 1→6  → 1.00, 0.85, 0.70, 0.55, 0.40, 0.30

When reliability or credibility is missing/unrecognized, the factor
defaults to 0.5 (neutral). The source-rating section in the pack's vault
CLAUDE.md is the authoritative scoring rubric — this is just the numeric
mapping.

Stale-threshold convention: effective_score < 0.5 is "stale" for the
purposes of flagging a page whose anchors are all stale.
"""
from __future__ import annotations

import os
from datetime import date

# Half-lives in days, keyed by source_kind. Env-overridable for tuning.
HALF_LIVES: dict[str, int] = {
    "filing":  int(os.environ.get("DECAY_HL_FILING", "1095")),
    "report":  int(os.environ.get("DECAY_HL_REPORT", "547")),
    "article": int(os.environ.get("DECAY_HL_ARTICLE", "547")),
    "blog":    int(os.environ.get("DECAY_HL_BLOG", "180")),
    "thread":  int(os.environ.get("DECAY_HL_THREAD", "180")),
    "primary": int(os.environ.get("DECAY_HL_PRIMARY", "1825")),
}
DEFAULT_HALF_LIFE = int(os.environ.get("DECAY_HL_DEFAULT", "365"))

RELIABILITY_FACTOR: dict[str, float] = {
    "A": 1.00, "B": 0.85, "C": 0.70,
    "D": 0.55, "E": 0.40, "F": 0.25,
}
CREDIBILITY_FACTOR: dict[int, float] = {
    1: 1.00, 2: 0.85, 3: 0.70,
    4: 0.55, 5: 0.40, 6: 0.30,
}
NEUTRAL_FACTOR = 0.5

STALE_THRESHOLD = float(os.environ.get("DECAY_STALE_THRESHOLD", "0.5"))


def half_life_for(source_kind: str | None) -> int:
    if not source_kind:
        return DEFAULT_HALF_LIFE
    return HALF_LIVES.get(source_kind.strip().lower(), DEFAULT_HALF_LIFE)


def reliability_to_factor(reliability) -> float:
    if isinstance(reliability, str):
        return RELIABILITY_FACTOR.get(reliability.strip().upper(), NEUTRAL_FACTOR)
    return NEUTRAL_FACTOR


def credibility_to_factor(credibility) -> float:
    try:
        c = int(credibility)
    except (TypeError, ValueError):
        return NEUTRAL_FACTOR
    return CREDIBILITY_FACTOR.get(c, NEUTRAL_FACTOR)


def base_score(reliability, credibility) -> float:
    """Mean of reliability and credibility factors. Both 0.5 when unset."""
    r = reliability_to_factor(reliability)
    c = credibility_to_factor(credibility)
    return round((r + c) / 2, 4)


def decay_factor(age_days: int, source_kind: str | None) -> float:
    """0.5 ** (age_days / half_life). Negative ages (sources from the future,
    which happen on bulk imports with future-dated `published`) clamped to 0."""
    if age_days < 0:
        age_days = 0
    hl = half_life_for(source_kind)
    if hl <= 0:
        return 1.0
    return round(0.5 ** (age_days / hl), 4)


def effective_score(reliability, credibility, source_kind: str | None, age_days: int) -> float:
    return round(base_score(reliability, credibility) * decay_factor(age_days, source_kind), 4)


def compute_for(reliability, credibility, source_kind: str | None, source_date: date | None, today: date) -> dict:
    """Return a structured score dict the dashboard can render directly."""
    age_days = (today - source_date).days if source_date else 0
    base = base_score(reliability, credibility)
    df = decay_factor(age_days, source_kind)
    eff = round(base * df, 4)
    hl = half_life_for(source_kind)
    return {
        "reliability": str(reliability) if reliability is not None else None,
        "credibility": int(credibility) if isinstance(credibility, (int, str)) and str(credibility).isdigit() else credibility,
        "source_kind": source_kind,
        "age_days": age_days,
        "half_life_days": hl,
        "base_score": base,
        "decay_factor": df,
        "effective_score": eff,
        "is_stale": eff < STALE_THRESHOLD,
    }


__all__ = [
    "HALF_LIVES", "DEFAULT_HALF_LIFE",
    "RELIABILITY_FACTOR", "CREDIBILITY_FACTOR", "NEUTRAL_FACTOR",
    "STALE_THRESHOLD",
    "half_life_for", "reliability_to_factor", "credibility_to_factor",
    "base_score", "decay_factor", "effective_score", "compute_for",
]
