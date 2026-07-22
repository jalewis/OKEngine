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
# Worst-grade factor for a rank-derived scale (see scale_from_enum). 0.25 reproduces the Admiralty
# reliability curve (A..F = 1.0..0.25) exactly, so a pack that declares the standard vocabulary scores
# identically whether it rides the hardcoded default or a schema-driven scale.
FACTOR_FLOOR = 0.25

STALE_THRESHOLD = float(os.environ.get("DECAY_STALE_THRESHOLD", "0.5"))


def _norm_grade(v) -> str:
    """Case/space-insensitive grade key ('a' -> 'A', ' 3 ' -> '3') so lookups match regardless of how
    a page authored the value or how the schema declared the enum."""
    return str(v).strip().upper()


# The engine-DEFAULT vocabularies as normalized scales — the FALLBACK used only when the governing
# schema declares no reliability/credibility `field_enums`. A pack SHOULD declare its own grading enum
# (base-schema states the scheme is a pack enum); until it does, the engine ranks by this Admiralty
# default and any grade outside it is surfaced as out-of-vocab (grade_recognized -> False), never
# silently scored neutral.
_DEFAULT_RELIABILITY_SCALE = {_norm_grade(k): v for k, v in RELIABILITY_FACTOR.items()}
_DEFAULT_CREDIBILITY_SCALE = {_norm_grade(k): v for k, v in CREDIBILITY_FACTOR.items()}


def scale_from_enum(ordered) -> "dict[str, float]":
    """Map an ORDERED grade vocabulary (best -> worst) to decay factors by RANK: best = 1.0, worst =
    FACTOR_FLOOR, linear between. This is how a pack drives the reliability/credibility vocabulary from
    its schema `field_enums` instead of the engine hardcoding Admiralty. A 6-value vocabulary yields
    1.0, 0.85, 0.70, 0.55, 0.40, 0.25 — the Admiralty reliability scale exactly. Empty -> {} (the
    caller falls back to the engine default)."""
    vals = [_norm_grade(v) for v in (ordered or []) if str(v).strip()]
    n = len(vals)
    if n == 0:
        return {}
    if n == 1:
        return {vals[0]: 1.0}
    step = (1.0 - FACTOR_FLOOR) / (n - 1)
    return {v: round(1.0 - i * step, 4) for i, v in enumerate(vals)}


def grade_recognized(value, scale: "dict | None") -> "bool | None":
    """None if `value` is unset; True if it is in the active vocabulary; False if it is OUT OF VOCAB
    (present but unrecognized — scored NEUTRAL, and the caller should SURFACE it rather than let a
    silently-neutralized grade read as 'average'). `scale` None uses no default here — pass the scale
    actually in effect (default or pack) so the answer matches what scored the page."""
    if value is None or str(value).strip() == "":
        return None
    return _norm_grade(value) in (scale or {})


def half_life_for(source_kind: str | None) -> int:
    if not source_kind:
        return DEFAULT_HALF_LIFE
    return HALF_LIVES.get(source_kind.strip().lower(), DEFAULT_HALF_LIFE)


def reliability_to_factor(reliability, scale: "dict | None" = None) -> float:
    """Reliability grade -> decay factor. `scale` (from scale_from_enum over the pack's declared
    vocabulary) overrides the Admiralty default; unset -> NEUTRAL; an unrecognized grade -> NEUTRAL
    (and grade_recognized flags it so the caller can surface it)."""
    scale = scale or _DEFAULT_RELIABILITY_SCALE
    if reliability is None or str(reliability).strip() == "":
        return NEUTRAL_FACTOR
    return scale.get(_norm_grade(reliability), NEUTRAL_FACTOR)


def credibility_to_factor(credibility, scale: "dict | None" = None) -> float:
    """Credibility grade -> decay factor (see reliability_to_factor for the scale/OOV contract)."""
    scale = scale or _DEFAULT_CREDIBILITY_SCALE
    if credibility is None or str(credibility).strip() == "":
        return NEUTRAL_FACTOR
    return scale.get(_norm_grade(credibility), NEUTRAL_FACTOR)


def base_score(reliability, credibility, reliability_scale=None, credibility_scale=None) -> float:
    """Mean of reliability and credibility factors. Both 0.5 when unset. Optional pack scales drive
    the vocabulary; omitted -> the engine Admiralty default (byte-identical to the pre-#351 behavior)."""
    r = reliability_to_factor(reliability, reliability_scale)
    c = credibility_to_factor(credibility, credibility_scale)
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


def effective_score(reliability, credibility, source_kind: str | None, age_days: int,
                    reliability_scale=None, credibility_scale=None) -> float:
    return round(base_score(reliability, credibility, reliability_scale, credibility_scale)
                 * decay_factor(age_days, source_kind), 4)


def compute_for(reliability, credibility, source_kind: str | None, source_date: date | None,
                today: date, reliability_scale=None, credibility_scale=None) -> dict:
    """Return a structured score dict the dashboard can render directly. Optional pack scales drive
    the grading vocabulary; the OOV flags mark a grade that is present but outside the active
    vocabulary (scored neutral) so the caller can surface it instead of silently laundering it."""
    age_days = (today - source_date).days if source_date else 0
    base = base_score(reliability, credibility, reliability_scale, credibility_scale)
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
        "reliability_oov": grade_recognized(reliability, reliability_scale or _DEFAULT_RELIABILITY_SCALE) is False,
        "credibility_oov": grade_recognized(credibility, credibility_scale or _DEFAULT_CREDIBILITY_SCALE) is False,
    }


__all__ = [
    "HALF_LIVES", "DEFAULT_HALF_LIFE",
    "RELIABILITY_FACTOR", "CREDIBILITY_FACTOR", "NEUTRAL_FACTOR", "FACTOR_FLOOR",
    "STALE_THRESHOLD",
    "half_life_for", "reliability_to_factor", "credibility_to_factor",
    "base_score", "decay_factor", "effective_score", "compute_for",
    "scale_from_enum", "grade_recognized",
]
