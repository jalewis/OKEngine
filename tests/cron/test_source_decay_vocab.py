"""source_decay grading vocabulary is pack-schema driven, and out-of-vocab grades are surfaced,
not silently laundered to neutral (invariant-audit #351 / A4)."""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "source_decay.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="source_decay absent")


def _load():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("source_decay", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["source_decay"] = m
    spec.loader.exec_module(m)
    return m


def test_scale_from_enum_reproduces_admiralty():
    """A 6-value ordered vocabulary ranks linearly 1.0 -> 0.25 — exactly the hardcoded Admiralty
    reliability scale, so a pack that declares the standard grades scores identically."""
    m = _load()
    scale = m.scale_from_enum(["A", "B", "C", "D", "E", "F"])
    assert scale == {"A": 1.0, "B": 0.85, "C": 0.70, "D": 0.55, "E": 0.40, "F": 0.25}
    assert m.scale_from_enum([]) == {}
    assert m.scale_from_enum(["only"]) == {"ONLY": 1.0}


def test_default_fallback_is_byte_identical():
    """scale=None -> the engine Admiralty default, unchanged from pre-#351 behavior."""
    m = _load()
    assert m.reliability_to_factor("A") == 1.0 and m.reliability_to_factor("f") == 0.25
    assert m.reliability_to_factor("Z") == m.NEUTRAL_FACTOR       # unrecognized -> neutral
    assert m.reliability_to_factor(None) == m.NEUTRAL_FACTOR      # unset -> neutral
    assert m.credibility_to_factor(1) == 1.0 and m.credibility_to_factor("6") == 0.30
    assert m.credibility_to_factor("high") == m.NEUTRAL_FACTOR


def test_pack_scale_overrides_the_default():
    """A pack's own ordered vocabulary drives the factors instead of Admiralty."""
    m = _load()
    scale = m.scale_from_enum(["trusted", "mixed", "unreliable"])   # 3 values -> 1.0, 0.625, 0.25
    assert m.reliability_to_factor("mixed", scale) == 0.625
    assert m.reliability_to_factor("TRUSTED", scale) == 1.0          # case-insensitive
    assert m.reliability_to_factor("A", scale) == m.NEUTRAL_FACTOR   # Admiralty grade is OOV here


def test_grade_recognized_distinguishes_unset_known_oov():
    m = _load()
    scale = m.scale_from_enum(["A", "B", "C"])
    assert m.grade_recognized(None, scale) is None                  # unset
    assert m.grade_recognized("", scale) is None
    assert m.grade_recognized("b", scale) is True                   # known
    assert m.grade_recognized("Q", scale) is False                  # out of vocab


def test_compute_for_flags_out_of_vocab_grade():
    """An OOV grade still scores neutral (can't invent a factor) but is FLAGGED so the caller can
    surface it — it must not silently read as 'average'."""
    m = _load()
    today = date(2026, 6, 15)
    known = m.compute_for("A", 1, "report", date(2026, 6, 1), today)
    assert known["reliability_oov"] is False and known["credibility_oov"] is False
    oov = m.compute_for("BOGUS", 99, "report", date(2026, 6, 1), today)
    assert oov["reliability_oov"] is True and oov["credibility_oov"] is True
    assert oov["base_score"] == m.NEUTRAL_FACTOR                    # both neutral -> mean 0.5
    unset = m.compute_for(None, None, "report", date(2026, 6, 1), today)
    assert unset["reliability_oov"] is False and unset["credibility_oov"] is False  # unset != OOV
