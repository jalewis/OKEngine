"""Regression: importer_guard — write-path guards for no_agent direct writers (okengine#237).

The D10 class: importers bypass the enforced write path, so its guards never fire on their
output (3,898 lowercase tlp pages re-minted hours after a vault-wide backfill). This locks:
the guard's coerce/report semantics, its fail-open behavior, and — the #218 pattern — a
CROSS-IMPLEMENTATION contract pinning its item-check verdicts to write_server's, so the thin
twin can never drift from the boundary it mirrors."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    if name == "write_server":
        sys.modules[name] = m
    sys.path.insert(0, str(Path(path).parent))
    try:
        spec.loader.exec_module(m)
    finally:
        sys.path.pop(0)
    return m


def _vault(tmp_path, schema: str) -> Path:
    v = tmp_path / "vault"
    (v / "wiki").mkdir(parents=True)
    (v / "schema.yaml").write_text(schema, encoding="utf-8")
    return v


SCHEMA = """\
okf:
  required: [type]
types:
  source:
    required: [type]
  prediction:
    required: [type]
strict_types: false
field_items:
  evidence:
    direction: {enum: [reinforces, contradicts, partial, neutral]}
    confidence_before: {shape: number}
    date: {shape: date}
"""


def test_guard_coerces_and_reports(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, SCHEMA)
    # tlp rides the BASE schema enum (strict): case coerces, junk reports
    fm = {"type": "source", "tlp": "clear", "aliases": "A, B",
          "evidence": [{"direction": "Confirms"}]}
    problems = ig.guard(fm, vault=v)
    assert fm["tlp"] == "CLEAR"                       # enum case coerced
    assert fm["aliases"] == ["A", "B"]                # list shape coerced
    # 'Confirms' -> case-fold matches nothing sanctioned ('confirms' is legacy, not enum)
    assert any("direction" in p and "Confirms" in p for p in problems)
    fm2 = {"type": "source", "tlp": "chartreuse"}
    problems2 = ig.guard(fm2, vault=v)
    assert fm2["tlp"] == "chartreuse"                 # unknown left for the report
    assert any("tlp" in p for p in problems2), problems2


def test_guard_item_coercions(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, SCHEMA)
    fm = {"type": "prediction",
          "evidence": [{"direction": "Reinforces", "confidence_before": "0.55",
                        "date": "2026-07-15"}]}
    problems = ig.guard(fm, vault=v)
    assert problems == [], problems
    assert fm["evidence"][0]["direction"] == "reinforces"       # case coerced
    assert fm["evidence"][0]["confidence_before"] == 0.55       # number coerced


def test_guard_strict_item_objects_required_keys_and_container_shapes(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    schema = SCHEMA + """\
  adversarial_evidence:
    _item: {shape: dict, required: [observation, deception_possible, alternatives]}
    observation: {shape: str}
    deception_possible: {shape: bool}
    alternatives: {shape: list}
    deception_hypothesis: {shape: dict}
"""
    v = _vault(tmp_path, schema)
    fixtures = [
        ({"adversarial_evidence": ["flattened"]}, "must be an object"),
        ({"adversarial_evidence": [{"observation": "x", "alternatives": []}]},
         "deception_possible"),
        ({"adversarial_evidence": [{"observation": "x", "deception_possible": "yes",
                                     "alternatives": []}]}, "boolean"),
        ({"adversarial_evidence": [{"observation": "x", "deception_possible": False,
                                     "alternatives": "none"}]}, "must be a list"),
    ]
    for fields, expected in fixtures:
        problems = ig.guard({"type": "prediction", **fields}, vault=v)
        assert any(expected in p for p in problems), problems


def test_guard_fail_open_on_broken_vault(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    fm = {"type": "source", "tlp": "clear"}
    assert ig.guard(fm, vault=tmp_path / "nope") == [] or fm["tlp"] in ("clear", "CLEAR")
    # never raises, fm never half-mutated into invalidity


def test_guard_canonicalizes_type_alias_and_reports_unknown_strict_type(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    schema = SCHEMA.replace("strict_types: false", "strict_types: true") + \
        "type_aliases: {report: source}\n"
    v = _vault(tmp_path, schema)
    aliased = {"type": "report"}
    assert ig.guard(aliased, vault=v) == []
    assert aliased["type"] == "source"
    problems = ig.guard({"type": "threat_actor"}, vault=v)
    assert any("unknown type 'threat_actor'" in p for p in problems)


def test_guard_failure_revokes_contradictory_authority_approval(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, SCHEMA)
    fm = {
        "type": "source", "tlp": "chartreuse", "needs_review": False,
        "review_state": "approved", "reviewed_by": "policy:test", "reviewed_at": "2026-07-19T12:00:00Z",
        "review_method": "authority-auto-disposition", "review_policy": "test",
        "authority": "Example Authority", "authority_source_url": "https://example.test/record",
        "authority_verified_fields": ["title"], "authority_import": "direct-authority",
    }
    problems = ig.guard(fm, vault=v)
    assert problems
    assert fm["needs_review"] is True
    assert fm["authority_import"] == "direct-authority"  # retain provenance for a corrected retry
    for field in ig._AUTHORITY_APPROVAL_FIELDS:
        assert field not in fm


def test_clean_authority_approval_survives_guard(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, SCHEMA)
    fm = {"type": "source", "tlp": "CLEAR", "needs_review": False,
          "review_state": "approved", "reviewed_by": "policy:test"}
    assert ig.guard(fm, vault=v) == []
    assert fm["review_state"] == "approved"


def test_cross_implementation_contract_with_write_server(tmp_path, monkeypatch):
    """The #218 pattern: importer_guard's item semantics == write_server's, on shared
    fixtures — coerced results identical, accept/reject verdicts identical."""
    v = _vault(tmp_path, SCHEMA)
    monkeypatch.setenv("WIKI_PATH", str(v))
    sys.modules.pop("write_server", None)
    ws = _load("write_server", REPO / "okengine-mcp" / "write_server.py")
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")

    fixtures = [
        {"evidence": [{"direction": "Reinforces"}]},               # case -> coerce
        {"evidence": [{"direction": "confirms"}]},                 # junk -> reject/report
        {"evidence": [{"direction": "neutral", "confidence_before": "0.4"}]},   # number coerce
        {"evidence": [{"direction": "neutral", "confidence_before": "half"}]},  # junk number
        {"evidence": [{"direction": "partial", "date": "2026-01-01"}]},         # date ok
        {"evidence": [{"direction": "partial", "date": "last week"}]},          # junk date
        {"evidence": ["legacy prose entry", {"direction": "reinforces"}]},      # non-dict pass
    ]
    p = ws._safe("predictions/contract-probe")
    for i, fx in enumerate(fixtures):
        ws_fm = {"type": "prediction", **{k: [dict(x) if isinstance(x, dict) else x for x in vs]
                                          for k, vs in fx.items()}}
        ig_fm = {"type": "prediction", **{k: [dict(x) if isinstance(x, dict) else x for x in vs]
                                          for k, vs in fx.items()}}
        ws_reject = ws._item_shape_reject(p, ws_fm)
        ig_problems = [pr for pr in ig.guard(ig_fm, vault=v)
                       if "evidence[" in pr]                        # item-scope only
        assert bool(ws_reject) == bool(ig_problems), \
            f"fixture {i}: write_server={ws_reject!r} vs guard={ig_problems!r}"
        if not ws_reject:
            assert ws_fm["evidence"] == ig_fm["evidence"], \
                f"fixture {i}: coerced results diverge"
