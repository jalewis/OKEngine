"""Regression: wiki_schema_audit must classify against the MERGED schema, not the raw pack schema.

Reading `governing_schema(VAULT)` (pack-only) flagged every engine-owned base type
(dashboard/prediction/source/concept/…) as DRIFT on a norm-following vault — advising the operator
to canonize a type the engine already owns (which breaks composition). It also miscounted STIX/legacy
aliases as unsanctioned types. This pins `merged_schema` + `type_aliases`.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "wiki_schema_audit.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    spec = importlib.util.spec_from_file_location("wiki_schema_audit", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["wiki_schema_audit"] = m
    spec.loader.exec_module(m)               # CANONICAL_TYPES/TYPE_ALIASES resolve at import
    return m


def test_base_types_are_canonical_not_drift(tmp_path):
    # a pack that (correctly) declares only its own type + a STIX alias, NOT the engine base types
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n"
        "type_aliases:\n  threat-actor: actor\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path)
    # merged_schema folds the engine base taxonomy in → base types are canonical, not DRIFT
    for base in ("dashboard", "prediction", "source", "concept"):
        assert base in m.CANONICAL_TYPES, f"{base} should be canonical via merged_schema"
    assert "actor" in m.CANONICAL_TYPES                 # the pack type too
    assert m.TYPE_ALIASES.get("threat-actor") == "actor"   # alias resolves, not counted as drift
