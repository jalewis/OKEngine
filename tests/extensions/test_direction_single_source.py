"""Cross-surface contract: the evidence[].direction vocabulary has ONE source (okengine#217/#218).

Source of truth: extensions/okengine.predictions/schema/predictions.schema.yaml (field_items).
Derivations pinned here so they can never drift (the D1 class — the enum used to live only in
prompt text while the cockpit laundered drifted values through a synonym map):

  1. the composer folds the fragment (write-path enforcement sees it — mechanism red-tested
     in tests/test_field_items.py);
  2. the regrade digest renders its vocabulary line from the composed schema, and its literal
     FALLBACK equals the fragment;
  3. the cockpit's fallback set and legacy-synonym map stay consistent with the fragment —
     parsed from app.py SOURCE (ast), not imported, so this contract runs on system python
     (make check has no fastapi; an importorskip here would be a vacuous pass).

Per the standing rule: a surface that cannot be read fails loudly — never silently skipped.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FRAGMENT = REPO / "extensions" / "okengine.predictions" / "schema" / "predictions.schema.yaml"
EXT_YAML = REPO / "extensions" / "okengine.predictions" / "extension.yaml"
SELECTOR = REPO / "extensions" / "okengine.predictions" / "select_regrade_batch.py"
COCKPIT = REPO / "okengine-cockpit" / "app.py"
SCHEMA_LIB = REPO / "scripts" / "cron" / "schema_lib.py"


def _fragment_enum() -> set[str]:
    doc = yaml.safe_load(FRAGMENT.read_text(encoding="utf-8"))
    return set(doc["field_items"]["evidence"]["direction"]["enum"])


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cockpit_const(name: str):
    """Evaluate a module-level constant from app.py SOURCE without importing it (no fastapi
    needed). Handles literals and frozenset({...}) calls. Raises if absent — a renamed
    constant must break this test, not silently pass."""
    tree = ast.parse(COCKPIT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            v = node.value
            if isinstance(v, ast.Call) and getattr(v.func, "id", "") == "frozenset":
                return frozenset(ast.literal_eval(v.args[0]))
            return ast.literal_eval(v)
    raise AssertionError(f"constant {name} not found in okengine-cockpit/app.py — "
                         f"renamed? update this contract test alongside it")


CANONICAL = {"reinforces", "contradicts", "partial", "neutral"}


def test_fragment_is_wired_and_canonical():
    assert _fragment_enum() == CANONICAL
    ext = yaml.safe_load(EXT_YAML.read_text(encoding="utf-8"))
    assert "schema/predictions.schema.yaml" in (ext.get("schema") or []), \
        "fragment not wired into extension.yaml `schema:` — orphaned, never composed"


def test_composer_folds_fragment_and_write_path_sees_it(tmp_path):
    """Fragment -> compose_schema -> item_rules: the exact chain the write path enforces."""
    sl = _load("schema_lib_ds", SCHEMA_LIB)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "types:\n  prediction:\n    required: [type]\n", encoding="utf-8"
    )
    frag = yaml.safe_load(FRAGMENT.read_text(encoding="utf-8"))
    composed, errors = sl.compose_schema(
        tmp_path, fragments=[("ext:okengine.predictions", frag)]
    )
    assert errors == []
    rules = sl.item_rules(composed)
    assert rules["evidence"]["direction"]["enum"] == CANONICAL
    assert composed["owners"]["field_items"]["evidence"] == "ext:okengine.predictions"


def test_digest_derives_from_schema_and_fallback_matches_fragment(tmp_path):
    sel = _load("select_regrade_batch_ds", SELECTOR)
    # 3a. the literal fallback is pinned to the fragment — the one copy that may exist
    assert set(sel._DIRECTION_FALLBACK) == _fragment_enum()
    # 3b. a vault whose governing schema EXTENDS the vocabulary flows into the digest line
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "types:\n  prediction:\n    required: [type]\n"
        "field_items:\n  evidence:\n"
        "    direction: {enum: [reinforces, contradicts, partial, neutral, speculative]}\n",
        encoding="utf-8",
    )
    got = sel.direction_enum(tmp_path)
    assert got == ["reinforces", "contradicts", "partial", "neutral", "speculative"]
    # 3c. no declaration anywhere -> the pinned fallback, in canonical order
    plain = tmp_path / "plain"
    (plain / "wiki").mkdir(parents=True)
    (plain / "schema.yaml").write_text("types:\n  prediction:\n    required: [type]\n",
                                       encoding="utf-8")
    assert sel.direction_enum(plain) == list(sel._DIRECTION_FALLBACK)


def test_cockpit_constants_consistent_with_fragment():
    fragment = _fragment_enum()
    fallback = _cockpit_const("_EV_DIR_FALLBACK")
    legacy = _cockpit_const("_EV_DIR_LEGACY")
    # the cockpit fallback is pinned to the fragment
    assert set(fallback) == fragment
    # legacy synonyms bucket INTO sanctioned values only
    assert set(legacy.values()) <= fragment, \
        f"legacy map buckets into unsanctioned values: {set(legacy.values()) - fragment}"
    # a sanctioned value must never appear as a legacy KEY (identity mapping belongs to the
    # enum pass-through, and a key here would shadow a future enum change)
    assert not (set(legacy.keys()) & fragment), \
        f"sanctioned values shadowed as legacy keys: {set(legacy.keys()) & fragment}"


def test_cockpit_runtime_loader_reads_composed_artifact(tmp_path, monkeypatch):
    """Behavioral half (needs fastapi — the cockpit venv suite runs it; system python skips
    THIS test only, the source-pinned contract above still guards)."""
    import pytest
    pytest.importorskip("fastapi")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    (tmp_path / ".okengine").mkdir()
    (tmp_path / ".okengine" / "composed-schema.yaml").write_text(
        "field_items:\n  evidence:\n    direction: {enum: [reinforces, contradicts, up]}\n",
        encoding="utf-8",
    )
    sys.modules.pop("app", None)
    app = _load("app", COCKPIT)
    assert app._ev_direction_enum() == frozenset({"reinforces", "contradicts", "up"})
    assert app._ev_bucket("up") == "up"                # schema-sanctioned passes through
    assert app._ev_bucket("confirms") == "reinforces"  # legacy history still buckets
    assert app._ev_bucket("filed") is None             # unknown = surfaced raw, never absorbed
