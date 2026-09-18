"""Contract examples for every deterministic event-field extractor."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE = Path(__file__).resolve().parents[2] / "extensions/okengine.events/event_schemas.py"


def _load():
    spec = importlib.util.spec_from_file_location("event_schemas_contract", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_funding_amount_round_valuation_and_investor():
    module = _load()
    result = module.extract_typed_fields(
        "funding", "Acme raises $12.5M Series A",
        "at a $100 million valuation, led by Big Ventures alongside Seed Fund.",
    )
    assert result == {
        "amount_usd": 12_500_000.0,
        "round": "series-a",
        "valuation_usd": 100_000_000.0,
        "lead_investor": "Big Ventures",
    }
    assert module.extract_typed_fields("funding", "No disclosed round", "") == {
        "amount_usd": None, "round": None, "valuation_usd": None, "lead_investor": None,
    }
    assert module._amounts("$2K $3b") == [2_000.0, 3_000_000_000.0]


def test_merger_both_grammar_directions_and_missing():
    module = _load()
    forward = module.extract_typed_fields(
        "m-and-a", "Alpha acquires Beta for $5M", "transaction"
    )
    assert forward == {"deal_value_usd": 5_000_000.0, "acquirer": "Alpha", "target": "Beta"}
    reverse = module.extract_typed_fields(
        "m-and-a", "Beta to be acquired by Alpha for $6M", "transaction"
    )
    assert reverse == {"deal_value_usd": 6_000_000.0, "acquirer": "Alpha", "target": "Beta"}
    assert module.extract_typed_fields("m-and-a", "Rumor", "") == {
        "deal_value_usd": None, "acquirer": None, "target": None,
    }


def test_product_buyer_regulatory_and_unknown_extractors():
    module = _load()
    assert module.extract_typed_fields("product-launch", "Tool now available", "") == {
        "launch_type": "ga", "is_general_availability": True,
    }
    assert module.extract_typed_fields("product-launch", "Tool preview", "") == {
        "launch_type": "beta", "is_general_availability": False,
    }
    assert module.extract_typed_fields("product-launch", "Tool update", "") == {
        "launch_type": None, "is_general_availability": None,
    }
    assert module.extract_typed_fields(
        "buyer-signal", "Surveyed 250 respondents including CISO", "62.5% agreed"
    ) == {"survey_n": 250, "buyer_role": "ciso", "percentage": 0.625}
    assert module.extract_typed_fields("buyer-signal", "Anecdote", "") == {
        "survey_n": None, "buyer_role": None, "percentage": None,
    }
    assert module.extract_typed_fields(
        "regulatory", "EU deadline 2026-12-31", "fine $3M", ["entities/a/acme"]
    ) == {
        "deadline_date": "2026-12-31", "enforcement_amount_usd": 3_000_000.0,
        "jurisdiction": "eu", "affected_entities": ["entities/a/acme"],
    }
    assert module.extract_typed_fields("regulatory", "Notice", "", None) == {
        "deadline_date": None, "enforcement_amount_usd": None,
        "jurisdiction": None, "affected_entities": [],
    }
    assert module.extract_typed_fields("not-supported", "x", "y") == {}
