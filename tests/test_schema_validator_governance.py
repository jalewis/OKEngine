"""P0 regression (invariant-audit #1, #4): the enforced write gate must apply engine-BASE
governance (core-type `required` floors, CLOSED base enums) and exempt engine-GENERATED structural
files — REGARDLESS of whether a composed-schema artifact exists.

Before the fix, `_evaluate` read `types`/`required`/`enums` straight from the resolved schema and
only hand-merged base for `okf.required`/`strict_types`. So on a single-pack deployment with no
schema-bringing extension (→ no `.okengine/composed-schema.yaml` → the raw pack schema.yaml is the
governing schema), base governance was silently NOT enforced, and enforcement flipped on/off with
any unrelated extension toggle. Separately, the narrow `_OKF_RESERVED_DEFAULT` flagged the engine's
own regenerated `HOT.md`/`HEALTH.md`/`BUNDLE.md`/`INDEX*` as non-conformant.
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

# A raw pack schema that OMITS the engine core types and declares NO field_enums — exactly a fresh
# single-pack deployment with no schema-bringing extension enabled (so no composed artifact exists).
RAW_PACK = "types:\n  entity: {required: [type]}\n"


def _load():
    spec = importlib.util.spec_from_file_location("schema_validator", SV)
    m = importlib.util.module_from_spec(spec)
    sys.modules["schema_validator"] = m
    spec.loader.exec_module(m)
    os.environ["OKENGINE_BASE_SCHEMA"] = str(BASE)   # deterministic engine base
    m._base_cache.clear(); m._dir_to_schema.clear(); m._schema_cache.clear()
    return m


def _vault(tmp: Path, schema: str) -> Path:
    w = tmp / "wiki"
    (w / "entities").mkdir(parents=True)
    (w / "schema.yaml").write_text(schema)
    return w


def _rr(m, pg):
    return m.schema_reject_reason(str(pg.resolve()), pg.read_text())


def test_base_type_required_floor_enforced_without_composed_artifact(tmp_path):
    m = _load()
    w = _vault(tmp_path, RAW_PACK)
    pg = w / "entities" / "p.md"
    # `prediction` is a BASE core type the pack omits; the page is missing its base-required
    # status/confidence/resolves_by. Pre-fix: type not in the raw pack `types`, strict_types off ->
    # ACCEPTED. Post-fix: base prediction floor binds -> rejected.
    pg.write_text("---\ntype: prediction\nid: 'prediction:p'\nsubject: x\n---\nbody\n")
    r = _rr(m, pg)
    assert r and "prediction" in r and "status" in r
    # all base-required fields present -> conformant
    pg.write_text("---\ntype: prediction\nid: 'prediction:p'\nstatus: open\n"
                  "confidence: 0.6\nsubject: x\nresolves_by: 2027-01-01\n---\nbody\n")
    assert _rr(m, pg) is None


def test_base_closed_enum_enforced_without_composed_artifact(tmp_path):
    m = _load()
    w = _vault(tmp_path, RAW_PACK)
    pg = w / "entities" / "e.md"
    # base `tlp` is a CLOSED enum; the pack declares no field_enums. Pre-fix: no enum iterated ->
    # ACCEPTED. Post-fix: base tlp enum binds -> PURPLE rejected.
    pg.write_text("---\ntype: entity\nid: 'entity:e'\ntlp: PURPLE\n---\nbody\n")
    r = _rr(m, pg)
    assert r and "tlp" in r
    pg.write_text("---\ntype: entity\nid: 'entity:e'\ntlp: RED\n---\nbody\n")
    assert _rr(m, pg) is None


def test_generated_structural_files_are_conformance_exempt(tmp_path):
    m = _load()
    w = _vault(tmp_path, RAW_PACK)
    # engine-regenerated dashboards + the INDEX tree + `_`-scaffold: non-page artifacts written each
    # cron run (type without an authored page's fields, or no id). All must be exempt.
    for name in ("HOT.md", "HEALTH.md", "BUNDLE.md", "INDEX.md", "INDEX-p02.md", "_review-queue.md"):
        pg = w / name
        pg.write_text("---\ntype: dashboard\n---\ngenerated\n")
        assert _rr(m, pg) is None, f"{name} should be conformance-exempt (engine-generated)"
    # a genuinely authored page is still validated (missing id -> rejected), i.e. the exemption is
    # scoped to the generated names, not a blanket pass.
    pg = w / "entities" / "real.md"
    pg.write_text("---\ntype: entity\n---\nx\n")
    assert _rr(m, pg) is not None
