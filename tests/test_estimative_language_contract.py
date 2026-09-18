"""Universal estimative probability is separate, closed, and ambiguity-safe (okengine#646)."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
CANONICAL = [
    "almost-no-chance", "very-unlikely", "unlikely", "roughly-even-chance", "likely",
    "very-likely", "almost-certain",
]


def test_base_schema_declares_a_separate_closed_probability_contract():
    schema = yaml.safe_load((REPO / "config/base-schema.yaml").read_text(encoding="utf-8"))
    assert "estimative_probability" in schema["common_optional"]
    assert schema["enums"]["estimative_probability"] == CANONICAL
    assert schema["field_enums"]["estimative_probability"] == {
        "enum": "estimative_probability"
    }
    aliases = schema["value_aliases"]["estimative_probability"]
    assert aliases["probable"] == "likely"
    assert aliases["remote"] == "almost-no-chance"
    assert not {"possible", "medium", "maybe"} & aliases.keys()
    assert "confidence_band" not in schema["field_enums"], (
        "the engine must not reinterpret a pack-owned evidence-confidence field"
    )


def test_a_pack_cannot_redefine_the_closed_universal_vocabulary_or_alias(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO / "scripts/cron"))
    import schema_lib

    (tmp_path / "schema.yaml").write_text(
        "enums:\n  estimative_probability: [certain]\n"
        "field_enums:\n  estimative_probability: {enum: invented}\n"
        "value_aliases:\n  estimative_probability: {probable: almost-certain, custom: likely}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(REPO / "config/base-schema.yaml"))
    composed = schema_lib.merged_schema(tmp_path)
    assert composed["enums"]["estimative_probability"] == CANONICAL
    assert composed["field_enums"]["estimative_probability"] == {
        "enum": "estimative_probability"
    }
    assert composed["value_aliases"]["estimative_probability"]["probable"] == "likely"
    assert composed["value_aliases"]["estimative_probability"]["custom"] == "likely"


def _load_write_server(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config/base-schema.yaml")
    (vault / "wiki").mkdir(parents=True, exist_ok=True)
    (vault / "schema.yaml").write_text(
        "types:\n  finding: {required: [type]}\n"
        "partitioning:\n  namespaces: {finding: {strategy: flat}}\n",
        encoding="utf-8",
    )
    module_path = REPO / "okengine-mcp/write_server.py"
    spec = importlib.util.spec_from_file_location("write_server_estimative", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_write_boundary_normalizes_only_a_declared_meaning_preserving_alias(tmp_path):
    module = _load_write_server(tmp_path)
    page = module._safe("finding.md")
    normalized, _ = module._normalize_drift(
        {"type": "finding", "estimative_probability": "Probable"}, page
    )
    assert normalized["estimative_probability"] == "likely"

    ambiguous, _ = module._normalize_drift(
        {"type": "finding", "estimative_probability": "possible"}, page
    )
    assert ambiguous["estimative_probability"] == "possible"


def test_write_boundary_handles_non_mapping_alias_policies(tmp_path, monkeypatch):
    module = _load_write_server(tmp_path)
    policy_module = sys.modules[module._normalize_drift.__module__]
    page = module._safe("finding.md")
    incoming = {"type": "finding", "estimative_probability": "Probable"}

    monkeypatch.setattr(
        policy_module,
        "_governing",
        lambda _page: {"value_aliases": ["not-a-mapping"]},
    )
    monkeypatch.setattr(policy_module, "drift_policy", lambda _path: {})
    assert module._normalize_drift(incoming, page) == (incoming, [])

    monkeypatch.setattr(
        policy_module,
        "_governing",
        lambda _page: {
            "value_aliases": {"estimative_probability": {"probable": "likely"}}
        },
    )
    monkeypatch.setattr(policy_module, "drift_policy", lambda _path: {"value_aliases": []})
    normalized, _ = module._normalize_drift(incoming, page)
    assert normalized["estimative_probability"] == "likely"


def test_schema_alias_migration_reports_ambiguity_and_is_dry_run_first(tmp_path):
    module_path = REPO / "scripts/normalize_vocabulary.py"
    spec = importlib.util.spec_from_file_location("normalize_estimative", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types: {finding: {required: [type]}}\n",
                                           encoding="utf-8")
    probable = tmp_path / "wiki/probable.md"
    possible = tmp_path / "wiki/possible.md"
    probable.write_text("---\ntype: finding\nestimative_probability: Roughly Even Odds\n---\n",
                        encoding="utf-8")
    possible.write_text("---\ntype: finding\nestimative_probability: possible\n---\n",
                        encoding="utf-8")
    before = probable.read_text(encoding="utf-8")

    state = module.scan(tmp_path, "estimative_probability", {}, False, None, "wiki", True)

    assert state["changed"] == 1
    assert state["unmapped"] == {"possible": 1}
    assert probable.read_text(encoding="utf-8") == before
    report = module.report(state)
    assert "dry run" in report and "UNPARSEABLE / AMBIGUOUS" in report
    assert "possible" in report
