"""Regression: schema-declared ITEM contracts for list-of-dict fields (okengine#211).

The D1 class: the `evidence[].direction` vocabulary lived only in prompt text, so
agent-authored values drifted (18 entries) and the cockpit tally silently mis-bucketed
them. This locks the mechanism: `field_items` composes through schema_lib (base ⊕ pack ⊕
extension fragments, no shadowing) and the write path REJECTS out-of-enum / wrong-shape
item values on every mutating op.

Write-path tests call the plain `_create`/`_update` helpers directly (the @mcp.tool()
wrappers merely delegate, and `mcp` may be absent in the host test env).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WS_MOD = REPO / "okengine-mcp" / "write_server.py"
SL_MOD = REPO / "scripts" / "cron" / "schema_lib.py"


def _load_schema_lib():
    spec = importlib.util.spec_from_file_location("schema_lib_fi_test", SL_MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_write_server():
    spec = importlib.util.spec_from_file_location("write_server", WS_MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["write_server"] = m
    spec.loader.exec_module(m)
    return m


# ── schema_lib: normalization + composition ───────────────────────────────────────────

def test_item_rules_inline_enum_and_shapes():
    sl = _load_schema_lib()
    rules = sl.item_rules({
        "field_items": {
            "evidence": {
                "_item": {"shape": "dict", "required": ["direction", "source"]},
                "direction": {"enum": ["reinforces", "contradicts", "partial", "neutral"]},
                "confidence_before": {"shape": "number"},
                "date": {"shape": "date"},
                "source": {"shape": "str"},
                "deception_possible": {"shape": "bool"},
                "alternatives": {"shape": "list"},
                "deception_hypothesis": {"shape": "dict"},
                "note": {"comment": "no enforceable rule"},   # skipped, not fatal
            }
        }
    })
    ev = rules["evidence"]
    assert ev["_item"] == {"shape": "dict", "required": {"direction", "source"}}
    assert ev["direction"]["enum"] == {"reinforces", "contradicts", "partial", "neutral"}
    assert ev["confidence_before"]["shape"] == "number"
    assert ev["date"]["shape"] == "date"
    assert ev["source"]["shape"] == "str"
    assert ev["deception_possible"]["shape"] == "bool"
    assert ev["alternatives"]["shape"] == "list"
    assert ev["deception_hypothesis"]["shape"] == "dict"
    assert "note" not in ev


def test_item_rules_enum_name_indirection():
    """`enum: <name>` resolves through the schema's enums: map — the same single-source
    indirection field_enums uses, so #217 can declare the vocabulary exactly once."""
    sl = _load_schema_lib()
    rules = sl.item_rules({
        "enums": {"direction": ["reinforces", "contradicts"]},
        "field_items": {"evidence": {"direction": {"enum": "direction"}}},
    })
    assert rules["evidence"]["direction"]["enum"] == {"reinforces", "contradicts"}
    # unknown name -> rule skipped (never crash the write path on a schema typo)
    assert sl.item_rules({"field_items": {"evidence": {"direction": {"enum": "nope"}}}}) == {}


def test_merge_base_pack_field_items_pack_wins(tmp_path):
    sl = _load_schema_lib()
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "types:\n  source:\n    required: [type]\n"
        "field_items:\n  evidence:\n    direction: {enum: [up, down]}\n",
        encoding="utf-8",
    )
    merged = sl.merged_schema(tmp_path)
    assert merged["field_items"]["evidence"]["direction"]["enum"] == ["up", "down"]


def test_compose_fragment_field_items_and_no_shadowing(tmp_path):
    sl = _load_schema_lib()
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(
        "types:\n  source:\n    required: [type]\n", encoding="utf-8"
    )
    frag = {"field_items": {"evidence": {"direction": {"enum": ["reinforces", "contradicts"]}}}}
    composed, errors = sl.compose_schema(tmp_path, fragments=[("ext:okengine.predictions", frag)])
    assert errors == []
    assert composed["field_items"]["evidence"]["direction"]["enum"] == ["reinforces", "contradicts"]
    assert composed["owners"]["field_items"]["evidence"] == "ext:okengine.predictions"
    # a second fragment claiming the same field is a HARD conflict, not a silent override
    frag2 = {"field_items": {"evidence": {"direction": {"enum": ["other"]}}}}
    composed, errors = sl.compose_schema(
        tmp_path,
        fragments=[("ext:okengine.predictions", frag), ("ext:rogue", frag2)],
    )
    assert any("already declared" in e for e in errors), errors
    # the first owner's contract survives
    assert composed["field_items"]["evidence"]["direction"]["enum"] == ["reinforces", "contradicts"]


def test_compose_fragment_owned_enums_shapes_and_field_enums(tmp_path):
    sl = _load_schema_lib()
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n", encoding="utf-8")
    frag = {
        "enums": {"diagnosticity": ["low", "high"]},
        "field_enums": {"confidence_band": {"enum": "diagnosticity"}},
        "field_shapes": {"adversarial_evidence": "list"},
        "field_items": {"adversarial_evidence": {
            "diagnosticity": {"enum": "diagnosticity"},
        }},
    }
    composed, errors = sl.compose_schema(tmp_path, fragments=[("ext:assessments", frag)])
    assert errors == []
    assert composed["enums"]["diagnosticity"] == ["low", "high"]
    assert composed["field_enums"]["confidence_band"]["enum"] == "diagnosticity"
    assert composed["field_shapes"]["adversarial_evidence"] == "list"
    assert sl.item_rules(composed)["adversarial_evidence"]["diagnosticity"]["enum"] == {"low", "high"}
    assert composed["owners"]["enums"]["diagnosticity"] == "ext:assessments"

    _, errors = sl.compose_schema(tmp_path, fragments=[("ext:a", frag), ("ext:b", frag)])
    assert any("enum 'diagnosticity' already owned" in e for e in errors)
    assert any("field_enums 'confidence_band' already declared" in e for e in errors)
    assert any("field_shapes 'adversarial_evidence' already declared" in e for e in errors)


# ── write path: enforcement on the plain helpers ──────────────────────────────────────

_SCHEMA = """\
okf:
  required: [type]
types:
  prediction:
    required: [type]
strict_types: false
field_items:
  evidence:
    direction: {enum: [reinforces, contradicts, partial, neutral]}
    confidence_before: {shape: number}
    date: {shape: date}
"""


_STRICT_SCHEMA = _SCHEMA + """\
  adversarial_evidence:
    _item: {shape: dict, required: [observation, deception_possible, alternatives]}
    observation: {shape: str}
    deception_possible: {shape: bool}
    alternatives: {shape: list}
    deception_hypothesis: {shape: dict}
"""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-15")
    sys.modules.pop("write_server", None)
    m = _load_write_server()
    return m, tmp_path


def _fm(direction="reinforces", extra=""):
    return {
        "type": "prediction",
        "evidence": [{"direction": direction, "confidence_before": 0.5}],
    }


def test_create_out_of_enum_item_rejected(vault):
    m, root = vault
    res = m._create("predictions/p-drift", _fm(direction="confirms"), "# P\n\nBody.\n")
    assert res.startswith("rejected:"), res
    assert "evidence[0].direction" in res and "confirms" in res and "reinforces" in res
    assert not (root / "wiki" / "predictions" / "p-drift.md").exists()


def test_create_sanctioned_item_passes(vault):
    m, root = vault
    res = m._create("predictions/p-ok", _fm(direction="contradicts"), "# P\n\nBody.\n")
    assert not res.startswith("rejected:"), res
    assert (root / "wiki" / "predictions" / "p-ok.md").exists()


def test_strict_item_object_required_fields_and_container_shapes(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text(_STRICT_SCHEMA, encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.modules.pop("write_server", None)
    m = _load_write_server()

    base = {"type": "prediction", "adversarial_evidence": [{
        "observation": "artifact observed", "deception_possible": True,
        "alternatives": ["natural", "staged"], "deception_hypothesis": {"audience": "analysts"},
    }]}
    assert not m._create("predictions/strict-ok", base, "# P\n").startswith("rejected:")

    fixtures = [
        (["prose"], "must be an object"),
        ([{"observation": "x", "alternatives": []}], "deception_possible"),
        ([{"observation": "x", "deception_possible": "yes", "alternatives": []}], "boolean"),
        ([{"observation": "x", "deception_possible": False, "alternatives": "none"}], "list"),
    ]
    for i, (items, expected) in enumerate(fixtures):
        res = m._create(f"predictions/strict-bad-{i}",
                        {"type": "prediction", "adversarial_evidence": items}, "# P\n")
        assert res.startswith("rejected:") and expected in res, res


def test_update_out_of_enum_item_rejected_file_untouched(vault):
    m, root = vault
    m._create("predictions/p1", _fm(), "# P\n\nBody.\n")
    before = (root / "wiki" / "predictions" / "p1.md").read_text(encoding="utf-8")
    res = m._update(
        "predictions/p1",
        {"evidence": [{"direction": "strongly_reinforces"}]},
    )
    assert res.startswith("rejected:"), res
    assert (root / "wiki" / "predictions" / "p1.md").read_text(encoding="utf-8") == before


def test_number_shape_coerces_string_rejects_junk(vault):
    m, root = vault
    fm = {"type": "prediction",
          "evidence": [{"direction": "neutral", "confidence_before": "0.55"}]}
    res = m._create("predictions/p-num", fm, "# P\n\nBody.\n")
    assert not res.startswith("rejected:"), res
    page = (root / "wiki" / "predictions" / "p-num.md").read_text(encoding="utf-8")
    assert "confidence_before: 0.55" in page          # coerced to a number, not a quoted string
    fm_bad = {"type": "prediction",
              "evidence": [{"direction": "neutral", "confidence_before": "about half"}]}
    res = m._create("predictions/p-num-bad", fm_bad, "# P\n\nBody.\n")
    assert res.startswith("rejected:") and "must be a number" in res


def test_date_shape_iso_passes_junk_rejects(vault):
    m, _ = vault
    ok = {"type": "prediction", "evidence": [{"direction": "neutral", "date": "2026-07-01"}]}
    assert not m._create("predictions/p-date", ok, "# P\n\nBody.\n").startswith("rejected:")
    bad = {"type": "prediction", "evidence": [{"direction": "neutral", "date": "last Tuesday"}]}
    res = m._create("predictions/p-date-bad", bad, "# P\n\nBody.\n")
    assert res.startswith("rejected:") and "ISO date" in res


def test_legacy_prose_item_and_undeclared_fields_pass(vault):
    """Non-dict items (legacy prose strings in old evidence lists) and fields with no
    declared contract must pass — the guard enforces vocabulary/shape, nothing more."""
    m, root = vault
    fm = {"type": "prediction",
          "evidence": ["2026-06-01 seeded from lacuna", {"direction": "partial"}],
          "some_other_list": [{"anything": "goes"}]}
    res = m._create("predictions/p-legacy", fm, "# P\n\nBody.\n")
    assert not res.startswith("rejected:"), res
    assert (root / "wiki" / "predictions" / "p-legacy.md").exists()
