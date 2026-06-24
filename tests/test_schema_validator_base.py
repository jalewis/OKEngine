"""P0 regression: the engine base schema floors okf.required and surfaces the
`okf.should` WARN tier through the validator — without changing existing verdicts.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
SV = REPO / "tools" / "schema_validator.py"
BASE = REPO / "config" / "base-schema.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("schema_validator", SV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["schema_validator"] = m
    spec.loader.exec_module(m)
    os.environ["OKENGINE_BASE_SCHEMA"] = str(BASE)   # deterministic base
    m._base_cache.clear(); m._dir_to_schema.clear(); m._schema_cache.clear()
    return m


def _vault(tmp: Path, schema: str) -> Path:
    w = tmp / "wiki"
    (w / "entities").mkdir(parents=True)
    (w / "schema.yaml").write_text(schema)
    return w


def test_base_floors_required_type_when_pack_omits_okf(tmp_path):
    m = _load()
    w = _vault(tmp_path, "types:\n  entity: {required: [type]}\n")  # NO okf: block
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: entity\nid: 'entity:a'\n---\nx\n")
    assert m.schema_reject_reason(str(pg.resolve()), pg.read_text()) is None
    # a page with no `type` is rejected by the base floor even though the pack
    # declared no okf block
    pg.write_text("---\ntitle: notype\n---\nx\n")
    r = m.schema_reject_reason(str(pg.resolve()), pg.read_text())
    assert r and "type" in r


def test_id_required_after_promotion_should_tier_empty(tmp_path):
    """`id` was promoted WARN->MUST after the P1 id backfill stamped every page: a
    page missing `id` now HARD-rejects (it was a should/WARN flag), and the engine
    `okf.should` WARN tier is empty."""
    m = _load()
    w = _vault(tmp_path, "types:\n  entity: {required: [type]}\n")
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: entity\n---\nx\n")          # no id -> now a hard reject
    r = m.schema_reject_reason(str(pg.resolve()), pg.read_text())
    assert r and "id" in r
    assert m.missing_should(str(pg.resolve()), pg.read_text()) == []   # should tier now empty
    pg.write_text("---\ntype: entity\nid: 'entity:a'\n---\nx\n")        # with id -> conformant
    assert m.schema_reject_reason(str(pg.resolve()), pg.read_text()) is None


def test_strict_types_is_engine_owned_pack_value_ignored(tmp_path):
    """#23: strict_types is engine-owned. A pack setting strict_types: true does
    NOT cause unknown-type rejection — the engine base (default false) governs."""
    m = _load()  # real base ships strict_types: false
    w = _vault(tmp_path, "strict_types: true\ntypes:\n  entity: {required: [type]}\n")
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: wildcat\nid: 'x:wildcat'\n---\nx\n")   # unknown type (+id so strict_types is isolated)
    assert m.schema_reject_reason(str(pg.resolve()), pg.read_text()) is None


def test_strict_types_enforced_when_engine_base_sets_it(tmp_path, monkeypatch):
    """When the ENGINE base sets strict_types: true, unknown types are rejected
    regardless of what the pack declares."""
    base = tmp_path / "strict-base.yaml"
    base.write_text("okf: {required: [type]}\nstrict_types: true\n")
    m = _load()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(base))
    m._base_cache.clear()
    w = _vault(tmp_path, "types:\n  entity: {required: [type]}\n")  # pack omits strict_types
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: wildcat\n---\nx\n")
    r = m.schema_reject_reason(str(pg.resolve()), pg.read_text())
    assert r and "unknown type" in r
    pg.write_text("---\ntype: entity\n---\nx\n")              # a declared type still passes
    assert m.schema_reject_reason(str(pg.resolve()), pg.read_text()) is None


def test_missing_base_degrades_to_legacy_behavior(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setenv("OKENGINE_BASE_SCHEMA", str(tmp_path / "nope.yaml"))
    m._base_cache.clear()
    w = _vault(tmp_path, "okf: {required: [type]}\ntypes:\n  entity: {required: [type]}\n")
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: entity\n---\nx\n")
    # no base file → identical to pre-base behaviour (pack's own okf governs)
    assert m.schema_reject_reason(str(pg.resolve()), pg.read_text()) is None
    assert m.missing_should(str(pg.resolve()), pg.read_text()) == []


# ── #22: strict (fail-closed) conformance profile ──────────────────────────

def test_strict_fails_on_missing_schema_runtime_passes(tmp_path):
    """No schema.yaml in ancestry: runtime is OFF (None); strict FAILs."""
    m = _load()
    pg = tmp_path / "loose.md"                      # no schema.yaml up the tree
    pg.write_text("---\ntype: entity\n---\nx\n")
    p, c = str(pg.resolve()), pg.read_text()
    assert m.schema_reject_reason(p, c) is None
    r = m.conformance_reject_reason(p, c)
    assert r and "no governing schema" in r


def test_strict_fails_on_unparsable_schema(tmp_path):
    """A broken schema.yaml: runtime passes (fail-open); strict FAILs."""
    m = _load()
    w = tmp_path / "wiki"
    (w / "entities").mkdir(parents=True)
    (w / "schema.yaml").write_text("types: [this is: not valid\n::: broken yaml")
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: entity\n---\nx\n")
    p, c = str(pg.resolve()), pg.read_text()
    assert m.schema_reject_reason(p, c) is None
    r = m.conformance_reject_reason(p, c)
    assert r and ("unparsable" in r or "empty" in r)


def test_strict_fails_on_validator_exception(tmp_path, monkeypatch):
    """An internal validator error: runtime passes (a bug never bricks a write);
    strict FAILs (a release can't pass on a crashed check)."""
    m = _load()
    monkeypatch.setattr(m, "_find_schema", lambda _p: (_ for _ in ()).throw(RuntimeError("kaboom")))
    p = str((tmp_path / "x.md").resolve())
    assert m.schema_reject_reason(p, "---\ntype: x\n---\n") is None
    r = m.conformance_reject_reason(p, "---\ntype: x\n---\n")
    assert r and "validator error" in r


def test_strict_and_runtime_agree_on_real_violation(tmp_path):
    """A genuine conformance violation rejects in BOTH profiles."""
    m = _load()
    w = _vault(tmp_path, "types:\n  entity: {required: [type]}\n")
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntitle: notype\n---\nx\n")
    p, c = str(pg.resolve()), pg.read_text()
    assert "type" in (m.schema_reject_reason(p, c) or "")
    assert "type" in (m.conformance_reject_reason(p, c) or "")


def test_strict_skips_out_of_scope(tmp_path):
    """Out-of-scope files (not .md) PASS strict — strict != everything-is-a-page."""
    m = _load()
    w = _vault(tmp_path, "types:\n  entity: {required: [type]}\n")
    txt = w / "entities" / "note.txt"
    txt.write_text("not a page")
    p = str(txt.resolve())
    assert m.schema_reject_reason(p, "not a page") is None
    assert m.conformance_reject_reason(p, "not a page") is None     # skip, not error


def test_strict_passes_conformant_page(tmp_path):
    m = _load()
    w = _vault(tmp_path, "types:\n  entity: {required: [type]}\n")
    pg = w / "entities" / "a.md"
    pg.write_text("---\ntype: entity\nid: 'entity:a'\n---\nx\n")
    p, c = str(pg.resolve()), pg.read_text()
    assert m.schema_reject_reason(p, c) is None
    assert m.conformance_reject_reason(p, c) is None


def test_field_enum_rejects_closed_values(tmp_path):
    m = _load()
    w = _vault(tmp_path, """\
types:
  source: {required: [type, source_kind]}
enums:
  source_kind: [news, blog]
field_enums:
  source_kind: {enum: source_kind}
""")
    (w / "sources").mkdir()
    pg = w / "sources" / "item.md"
    pg.write_text("---\ntype: source\nsource_kind: feed\nid: 'source:item'\n---\nx\n")
    r = m.schema_reject_reason(str(pg.resolve()), pg.read_text())
    assert r and "source_kind='feed'" in r
    pg.write_text("---\ntype: source\nsource_kind: news\nid: 'source:item'\n---\nx\n")
    assert m.schema_reject_reason(str(pg.resolve()), pg.read_text()) is None
