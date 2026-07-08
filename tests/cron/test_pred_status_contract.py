"""Regression + cross-file contract: the prediction status vocabulary must agree across surfaces.

`pred_lib.is_open` used to match only status=='open', silently excluding the canonical `active`
synonym — so grading, regrade and forecast-review skipped every `active` prediction. forecast-review
also omitted the terminal `expired-ungraded` status. This pins the OPEN/RESOLVED sets to their two
authoritative declarations (config/base-schema.yaml `open_values` and the schema-drain prompt enum)
so the lanes can't drift from the schema again.
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "extensions" / "okengine.predictions" / "pred_lib.py"
BASE_SCHEMA = REPO / "config" / "base-schema.yaml"
DRAIN_PROMPT = REPO / "extensions" / "okengine.predictions" / "prompts" / "schema-drain.md"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="pred_lib absent")


def _load():
    spec = importlib.util.spec_from_file_location("pred_lib", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


P = _load()


def test_is_open_accepts_active_synonym():
    assert P.is_open({"status": "open"})
    assert P.is_open({"status": "active"})          # the bug: was silently False
    assert P.is_open({"status": "Active"})          # case-insensitive
    assert not P.is_open({"status": "confirmed"})
    assert not P.is_open({"status": ""})


def test_is_resolved_includes_expired_ungraded():
    for s in ("confirmed", "refuted", "partial", "expired-ungraded"):
        assert P.is_resolved({"status": s}), s
    assert not P.is_resolved({"status": "open"})
    assert not P.is_resolved({"status": "active"})


def test_open_and_resolved_are_disjoint():
    assert not (P.OPEN_VALUES & P.RESOLVED_VALUES)


def test_graded_is_resolved_minus_ungraded_expiry():
    # GRADED (has an outcome) ⊂ RESOLVED (terminal); base-rates/output-outcome use GRADED so an
    # ungraded expiry never enters a resolution rate. Guards against collapsing the two sets.
    assert P.GRADED_VALUES < P.RESOLVED_VALUES
    assert P.RESOLVED_VALUES - P.GRADED_VALUES == {"expired-ungraded"}
    assert not (P.GRADED_VALUES & P.OPEN_VALUES)


def test_open_values_match_base_schema():
    bs = yaml.safe_load(BASE_SCHEMA.read_text(encoding="utf-8"))
    ov = bs["tier"]["namespaces"]["predictions"]["open_values"]
    assert P.OPEN_VALUES == {str(s).lower() for s in ov}, \
        "pred_lib.OPEN_VALUES drifted from base-schema tier.namespaces.predictions.open_values"


def test_vocabulary_matches_drain_prompt_enum():
    """The schema-drain prompt declares the canonical enum ('one of open, active, confirmed,
    refuted, partial, expired-ungraded'). OPEN∪RESOLVED must equal exactly that set."""
    if not DRAIN_PROMPT.is_file():
        pytest.skip("schema-drain prompt absent")
    txt = DRAIN_PROMPT.read_text(encoding="utf-8")
    declared = {s for s in ("open", "active", "confirmed", "refuted", "partial", "expired-ungraded")
                if s in txt}
    assert declared == {"open", "active", "confirmed", "refuted", "partial", "expired-ungraded"}, \
        "drain prompt enum changed — reconcile pred_lib OPEN/RESOLVED_VALUES"
    assert (P.OPEN_VALUES | P.RESOLVED_VALUES) == declared
