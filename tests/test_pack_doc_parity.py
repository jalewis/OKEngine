"""Doc/code parity guard for the pack grammar (mirrors the extension guard).

Keeps authoring-a-pack.md self-maintaining: the pack.yaml keys, and the top-level
schema.yaml keys the engine actually READS (the contract), must be documented. Adding an
engine-read schema key or a pack.yaml key without documenting it fails CI.

schema.yaml is intentionally EXTENSIBLE (packs add domain-specific config the engine never
reads) — so this guards only the engine-contract keys, not every key a pack might carry.
"""
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACK_META = REPO / "scripts" / "pack_meta.py"
SCHEMA_VALIDATOR = REPO / "tools" / "schema_validator.py"
SCHEMA_LIB = REPO / "scripts" / "cron" / "schema_lib.py"
GUIDE = REPO / "docs" / "authoring-a-pack.md"

pytestmark = pytest.mark.skipif(not PACK_META.is_file() or not GUIDE.is_file(),
                                reason="pack modules / guide absent")

# Other top-level schema keys the engine reads outside schema_validator's `schema.get`
# (base-schema merge, schema_lib compose, maintenance crons). Keep documented in §2.
_EXTRA_CONTRACT_KEYS = {
    "hot_set", "tier", "strict_types", "common_optional", "owners",
    "operational_types", "depth_critical_types", "classify_hints", "classify_catchall",
}


def _pack_meta_mod():
    spec = importlib.util.spec_from_file_location("pack_meta", PACK_META)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _schema_contract_keys() -> set[str]:
    """Top-level schema.yaml keys the engine READS — auto-discovered from `schema.get("x")`
    / `schema["x"]` in the validator, so the set can't silently drift from the code."""
    src = SCHEMA_VALIDATOR.read_text(encoding="utf-8")
    found = set(re.findall(r"\bschema(?:\.get\(|\[)\s*[\"']([a-z_]+)[\"']", src))
    return found | _EXTRA_CONTRACT_KEYS


def test_pack_yaml_keys_documented():
    m = _pack_meta_mod()
    doc = GUIDE.read_text(encoding="utf-8")
    missing = sorted(k for k in m.PACK_YAML_KEYS if k not in doc)
    assert not missing, (
        f"pack.yaml keys undocumented in authoring-a-pack.md: {missing}")


def test_engine_contract_schema_keys_documented():
    doc = GUIDE.read_text(encoding="utf-8")
    keys = _schema_contract_keys()
    assert len(keys) >= 12, f"contract-key discovery looks broken (found {len(keys)})"
    missing = sorted(k for k in keys if k not in doc)
    assert not missing, (
        f"engine-read schema.yaml keys undocumented in authoring-a-pack.md: {missing} — "
        "document them in §2 (or, if no longer read by the engine, drop the read)")


def test_pack_yaml_constant_matches_loader():
    """PACK_YAML_KEYS is the documented grammar; it must cover what load_pack_meta consumes
    (guards the constant itself from drifting from the reader)."""
    m = _pack_meta_mod()
    src = PACK_META.read_text(encoding="utf-8")
    read = set(re.findall(r"data\.get\(\s*[\"']([a-z_]+)[\"']", src))
    missing = sorted(read - set(m.PACK_YAML_KEYS))
    assert not missing, f"load_pack_meta reads keys not in PACK_YAML_KEYS: {missing}"
