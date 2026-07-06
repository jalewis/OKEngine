"""okengine#90 P2 — engine-owned core schema.

The engine base provides the universal core (types + namespaces) as DEFAULTS: a pack that declares
one overrides the core copy (single-pack deploys unchanged); a pack that omits it inherits the core;
and under composition the core is owned by `engine`, so a pack that still OWNS a core id collides —
the signal to strip it from `owns`.
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "cron"))
import schema_lib  # noqa: E402

CORE_TYPES = {"source", "concept", "prediction", "finding", "dashboard", "briefing", "trend"}


def _pack(d: Path, types: dict, namespaces=None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "schema.yaml").write_text(yaml.safe_dump({
        "types": types,
        "partitioning": {"namespaces": namespaces or {}}}))
    return d


def test_core_is_in_base():
    base = schema_lib.base_schema()
    assert CORE_TYPES <= set(base.get("types") or {})
    assert all(base["types"][t].get("extensible") for t in CORE_TYPES)   # packs can extend core


def test_pack_def_overrides_core(tmp_path):
    # a pack still declaring `source` with extra required fields keeps its own def (safety gate)
    p = _pack(tmp_path / "p", {"source": {"required": ["type", "published", "source_kind"]}})
    m = schema_lib._merge_base_pack(p)
    assert m["types"]["source"]["required"] == ["type", "published", "source_kind"]


def test_pack_inherits_core_when_omitted(tmp_path):
    # a pack that declares only a domain type inherits the whole core
    p = _pack(tmp_path / "p", {"widget": {"required": ["type"]}})
    m = schema_lib._merge_base_pack(p)
    assert CORE_TYPES <= set(m["types"])           # inherited
    assert "widget" in m["types"]                  # plus its domain type
    assert {"entities", "sources", "concepts"} <= set(m["partitioning"]["namespaces"])


def test_core_ships_cross_cutting_optionals_and_tlp_standard():
    base = schema_lib.base_schema()
    for f in ("tlp", "source_kind", "publisher", "reliability", "credibility", "severity"):
        assert f in base["common_optional"], f
    assert base["enums"]["tlp"] == ["CLEAR", "GREEN", "AMBER", "AMBER+STRICT", "RED"]   # standard, baked


def test_pack_extends_base_enum_by_union(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "schema.yaml").write_text(yaml.safe_dump(
        {"types": {"w": {"required": ["type"]}}, "enums": {"source_kind": ["lab-post"]}}))
    m = schema_lib._merge_base_pack(d)
    sk = m["enums"]["source_kind"]
    assert "paper" in sk and "lab-post" in sk   # base ∪ pack, not replaced


def test_pack_extends_core_optional_only(tmp_path):
    # a pack adds an OPTIONAL field to a core type via `extends` — and does NOT tighten it
    d = tmp_path / "p"
    d.mkdir()
    (d / "schema.yaml").write_text(yaml.safe_dump({
        "types": {"widget": {"required": ["type"]}},
        "extends": {"source": {"fields": {"vendor": {"optional": True}}}}}))
    m = schema_lib._merge_base_pack(d)
    assert "vendor" in (m["types"]["source"].get("fields") or {})
    assert m["types"]["source"]["required"] == ["type", "published"]   # core required unchanged


def test_core_owned_by_engine_under_composition(tmp_path):
    p = _pack(tmp_path / "p", {"widget": {"required": ["type"]}})
    # a fragment (another pack) that OWNS a core type collides with engine ownership
    _composed, errs = schema_lib.compose_schema(
        p, fragments=[("pack:other", {"owns": {"types": {"source": {"required": ["type"]}}}})])
    assert any("source" in e and "engine" in e for e in errs), errs
