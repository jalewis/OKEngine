"""L1 regression: which prediction statuses count as OPEN is read from the schema's
tier.namespaces.predictions.open_values (the single config-driven contract tier_lib/build_hot_set
use), not a bare hardcoded literal in select_daily_brief that silently forks."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "select_daily_brief.py"


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ["OKENGINE_BASE_SCHEMA"] = str(REPO / "config" / "base-schema.yaml")
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("select_daily_brief", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_daily_brief"] = m
    spec.loader.exec_module(m)
    return m


def test_open_values_read_from_schema(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "wiki" / "schema.yaml").write_text(
        "tier:\n  namespaces:\n    predictions:\n      open_values: [open, active, proposed]\n")
    m = _load(tmp_path)
    assert m._open_prediction_values() == {"open", "active", "proposed"}   # schema-driven, not the literal


def test_open_values_defaults_when_no_schema(tmp_path):
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path)
    assert m._open_prediction_values() == {"open", "active"}               # safe fallback


def test_movement_uses_composed_knowledge_namespaces(tmp_path):  # invariant-audit #27
    """The brief's movement section must iterate the vault's DECLARED knowledge namespaces (composed
    schema), not a hardcoded ('entities','concepts') that silently skipped every pack namespace on a
    composed vault (okcti: threat-actors, cves, detections, …)."""
    (tmp_path / "wiki").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    actors: {strategy: by-letter}\n    cves: {strategy: by-date}\n")
    m = _load(tmp_path)
    nss = set(m._knowledge_namespaces())
    assert "actors" in nss and "cves" in nss, nss     # pack namespaces included, not the bare pair
