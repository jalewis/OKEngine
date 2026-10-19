"""Regression: importer_guard — write-path guards for no_agent direct writers (okengine#237).

The D10 class: importers bypass the enforced write path, so its guards never fire on their
output (3,898 lowercase tlp pages re-minted hours after a vault-wide backfill). This locks:
the guard's coerce/report semantics, its fail-open behavior, and — the #218 pattern — a
CROSS-IMPLEMENTATION contract pinning its item-check verdicts to write_server's, so the thin
twin can never drift from the boundary it mirrors."""
from __future__ import annotations

import importlib.util
import datetime as dt
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


def test_remaining_shape_date_and_fail_open_edges(tmp_path, monkeypatch):
    ig = _load("importer_guard_edges", REPO / "scripts" / "cron" / "importer_guard.py")
    monkeypatch.setattr(ig.schema_lib, "list_fields", lambda _schema: {"tags"})
    monkeypatch.setattr(ig.schema_lib, "int_fields", lambda _schema: {"count", "bad"})
    fm = {"tags": "one", "count": " 12 ", "bad": True}
    problems = ig._coerce_shapes(fm, {})
    assert fm["tags"] == ["one"] and fm["count"] == 12
    assert any("bad" in problem for problem in problems)

    schema = {"field_items": {"items": {
        "date": {"shape": "date"},
        "name": {"shape": "str"},
        "meta": {"shape": "dict"},
        "number": {"shape": "number"},
    }}}
    fm = {"items": [{"date": dt.date(2026, 7, 31), "name": 7,
                      "meta": [], "number": 3}]}
    problems = ig._guard_items(fm, schema)
    assert any("name" in p and "string" in p for p in problems)
    assert any("meta" in p and "object" in p for p in problems)
    assert not any("date" in p for p in problems)

    assert ig.guard([], vault=tmp_path) == []
    monkeypatch.setattr(
        ig.schema_lib, "merged_schema",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("schema unavailable")),
    )
    assert ig.guard({"type": "source"}, vault=tmp_path) == []

    monkeypatch.setattr(ig.schema_lib, "merged_schema", lambda *_a: {})
    monkeypatch.setattr(
        ig.schema_lib, "canonical_type",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("bad schema")),
    )
    assert ig.guard({"type": "source"}, vault=tmp_path) == []


# --- okengine#1012: a direct writer creating an actor gets the write path's grounding ---------------

ACTOR_SCHEMA = SCHEMA.replace("  prediction:\n", "  actor:\n    required: [type]\n  prediction:\n")


def _source_page(v: Path, ref: str, body: str) -> str:
    page = v / "wiki" / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: source\ntitle: Report\n---\n\n{body}\n", encoding="utf-8")
    return ref


def test_guard_reports_an_actor_its_sources_do_not_ground_on_create(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA)
    ref = _source_page(v, "sources/a", "ANY.RUN has identified Wazza, a new phishkit.")
    fm = {"type": "actor", "title": "ANY.RUN", "sources": [ref],
          "review_state": "approved", "authority": "x"}
    problems = ig.guard(fm, vault=v, creating=True)
    assert any("not grounded" in p and "reporting organization" in p for p in problems), problems
    assert fm["needs_review"] is True and "review_state" not in fm, "approval must be revoked"


def test_guard_accepts_an_affirmed_or_curated_actor_on_create(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA)
    ref = _source_page(v, "sources/b", "LockBit claimed the attack on the bank.")
    assert ig.guard({"type": "actor", "title": "LockBit", "sources": [ref]}, vault=v,
                    creating=True) == []
    # the importer of a curated list IS that list's mirror: its provenance label vouches
    assert ig.guard({"type": "actor", "title": "APT28", "sources": ["MITRE ATT&CK"]}, vault=v,
                    creating=True) == []


def test_guard_judges_actor_grounding_only_on_a_create(tmp_path):
    """An enrichment rewrite of a stored actor is not re-litigated, and other types never are."""
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA)
    assert ig.guard({"type": "actor", "title": "Unsourced"}, vault=v) == []
    assert ig.guard({"type": "source", "title": "Unsourced"}, vault=v, creating=True) == []
    assert any("unsourced" in p for p in ig.guard({"type": "actor", "title": "Unsourced"},
                                                  vault=v, creating=True))


def test_guard_says_so_when_the_grounding_library_cannot_load(tmp_path, monkeypatch):
    """Never fail-open on identity: an unchecked record is reported, not silently admitted."""
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA)
    monkeypatch.setitem(sys.modules, "okengine.actor_grounding", None)
    problems = ig.guard({"type": "actor", "title": "LockBit", "sources": ["MITRE ATT&CK"]},
                        vault=v, creating=True)
    assert any("grounding unavailable" in p for p in problems), problems


def test_actor_grounding_options_are_keyword_only(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA)
    for call in (lambda: ig.actor_identity_problems({}, v), lambda: ig.guard({}, v)):
        try:
            call()
        except TypeError:
            continue
        raise AssertionError("a positional vault must be refused")


def test_only_an_actor_is_judged_whatever_its_spelling_sorts_as(tmp_path):
    """A type that sorts before or after "actor" is not one; an actor built at runtime is."""
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA)
    assert ig.actor_identity_problems({"type": "account", "title": "X"}, vault=v) == []
    assert ig.actor_identity_problems({"type": "zone", "title": "X"}, vault=v) == []
    runtime_actor = "".join(["act", "or"])
    assert ig.actor_identity_problems({"type": runtime_actor, "title": "X"}, vault=v)


def test_guard_canonicalizes_a_type_alias(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, ACTOR_SCHEMA + "type_aliases: {threat-actor: actor, a-actor: actor}\n")
    for alias in ("threat-actor", "a-actor"):
        fm = {"type": alias, "title": "APT28", "sources": ["MITRE ATT&CK"]}
        assert ig.guard(fm, vault=v, creating=True) == []
        assert fm["type"] == "actor", alias


def test_guard_item_shapes_hold_for_well_formed_values(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    schema = SCHEMA + """\
  records:
    _item: {shape: dict}
    tags: {shape: list}
    meta: {shape: dict}
    label: {shape: str}
    flag: {shape: bool}
"""
    v = _vault(tmp_path, schema)
    good = {"type": "prediction", "records": [{"tags": ["a"], "meta": {"k": 1}, "label": "x",
                                               "flag": True}]}
    assert ig.guard(good, vault=v) == []
    bad = {"type": "prediction", "records": [{"tags": "a", "meta": "k", "label": 3, "flag": "y"}]}
    problems = ig.guard(bad, vault=v)
    for expected in ("must be a list", "must be an object", "must be a string", "must be a boolean"):
        assert any(expected in p for p in problems), (expected, problems)


# --- okengine#1020: every declared actor type, not the literal `actor` ------------------------------

def test_guard_grounds_a_declared_actor_type_on_create(tmp_path):
    """Red before okengine#1020: an importer creating `intrusion-set` pages skipped grounding."""
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, SCHEMA.replace(
        "  prediction:\n", "  intrusion-set:\n    required: [type]\n    role: actor\n  prediction:\n"))
    ref = _source_page(v, "sources/a", "ANY.RUN has identified Wazza, a new phishkit.")
    problems = ig.guard({"type": "intrusion-set", "title": "ANY.RUN", "sources": [ref]},
                        vault=v, creating=True)
    assert any("not grounded" in p for p in problems), problems
    assert ig.guard({"type": "intrusion-set", "title": "ANY.RUN", "sources": [ref]}, vault=v) == []


def test_guard_does_not_ground_an_undeclared_type(tmp_path):
    ig = _load("importer_guard", REPO / "scripts" / "cron" / "importer_guard.py")
    v = _vault(tmp_path, SCHEMA.replace(
        "  prediction:\n", "  intrusion-set:\n    required: [type]\n  prediction:\n"))
    assert ig.guard({"type": "intrusion-set", "title": "ANY.RUN"}, vault=v, creating=True) == []
