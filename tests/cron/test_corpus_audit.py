"""Regression tests for scripts/cron/corpus_audit.py — the standing corpus-integrity
detector (graduation of the 2026-07-15 capability-review classes D1-drift and D6-dead-field).

Red-tests the two failure classes AND the vacuous-pass guard (a vault with no field_enums
must report UNDETECTABLE, never a clean pass — the missing-key = WARN rule)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "corpus_audit.py"


def _load():
    spec = importlib.util.spec_from_file_location("corpus_audit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))  # for its `import schema_lib`
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def _page(path: Path, fm: str, body: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


def _vault(tmp_path: Path, schema: str) -> Path:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(schema, encoding="utf-8")
    return vault


SCHEMA_WITH_ENUMS = """
types:
  prediction:
    required: [type]
field_enums:
  severity: [low, medium, high]
"""

def test_enum_drift_detected(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "a.md", "type: entity\nseverity: high")
    _page(vault / "wiki" / "entities" / "b.md", "type: entity\nseverity: bogus")
    state = mod.audit(vault)
    assert "severity" in state["drift"], "out-of-enum value must be flagged"
    assert state["drift"]["severity"]["bogus"]["count"] == 1
    assert state["drift"]["severity"]["bogus"]["examples"] == ["entities/b.md"]
    # the in-enum value must NOT be flagged
    assert "high" not in state["drift"]["severity"]


def test_nested_evidence_direction_drift(tmp_path):
    """The D1 class: drifted direction values in the evidence list must surface."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(
        vault / "wiki" / "predictions" / "p1.md",
        "type: prediction\n"
        "evidence:\n"
        "  - {direction: reinforces, date: 2026-07-01}\n"
        "  - {direction: confirms, date: 2026-07-02}\n"
        "  - {direction: strongly_reinforces, date: 2026-07-03}\n",
    )
    state = mod.audit(vault)
    key = mod.EVIDENCE_DIRECTION_KEY
    assert state["drift"][key]["confirms"]["count"] == 1
    assert state["drift"][key]["strongly_reinforces"]["count"] == 1
    assert "reinforces" not in state["drift"].get(key, {})


def test_dead_field_degraded_and_ok(tmp_path):
    """The D6 class: candidates exist, zero population -> DEGRADED; populated -> OK."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "sources" / "s1.md", "type: source\ntitle: a")
    _page(vault / "wiki" / "sources" / "s2.md", "type: source\ntitle: b")
    _page(
        vault / "wiki" / "predictions" / "p1.md",
        "type: prediction\nevidence:\n  - {direction: reinforces}\n",
    )
    state = mod.audit(vault)
    # signal_class: 2 source candidates, 0 populated -> dead
    assert state["candidates"]["signal_class"] == 2
    assert state["populated"]["signal_class"] == 0
    # evidence: 1 prediction candidate, populated -> alive
    assert state["candidates"]["evidence"] == 1
    assert state["populated"]["evidence"] == 1
    text = mod.render(state, "2026-07-15")
    assert "DEGRADED" in text
    # and the rendered DEGRADED row is signal_class's, not evidence's
    row = [l for l in text.splitlines() if "`signal_class`" in l][0]
    assert "DEGRADED" in row
    row = [l for l in text.splitlines() if "`evidence`" in l and "|" in l][0]
    assert "OK" in row


def test_local_evidence_optional_trust_fields_are_dead_field_audited(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "sources" / "operator.md", "type: source\ntitle: Operator record")

    state = mod.audit(vault)
    report = mod.render(state, "2026-09-16")

    for field in ("local_only", "export_policy", "record_checksum", "bounded_auto_accept"):
        assert state["candidates"][field] == 1, f"{field} must audit the source corpus"
        assert state["populated"][field] == 0, f"{field} should be reported as unwired"
        row = [line for line in report.splitlines() if f"`{field}`" in line][0]
        assert "DEGRADED" in row, f"{field} silently escaped dead-field detection"


def test_prediction_feedback_loop_metrics(tmp_path):
    """The review graduation: coverage and terminal waste remain standing corpus rows."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(
        vault / "wiki" / "predictions" / "open-touched.md",
        "type: prediction\nstatus: open\nmade_on: 2026-01-01\nresolves_by: 2026-12-31\n"
        "measurement_method: tier-1 announcement\n"
        "evidence:\n  - {direction: reinforces, confidence_before: 0.5, confidence_after: 0.6}",
    )
    _page(
        vault / "wiki" / "predictions" / "open-untouched.md",
        "type: prediction\nstatus: open\nmade_on: 2026-01-01\nresolves_by: 2026-12-31",
    )
    _page(
        vault / "wiki" / "predictions" / "expired.md",
        "type: prediction\nstatus: expired-ungraded\nmade_on: 2025-01-01\nresolves_by: 2025-12-31",
    )
    _page(
        vault / "wiki" / "predictions" / "confirmed.md",
        "type: prediction\nstatus: confirmed\nmade_on: 2025-01-01\nresolves_by: 2025-12-31",
    )

    state = mod.audit(vault)
    assert state["prediction_loop"] == {
        "total": 4,
        "with_evidence": 1,
        "terminal": 2,
        "terminal_ungraded": 1,
        "open_primary": 2,
        "open_primary_missing_measurement_method": 1,
    }
    text = mod.render(state, "2026-07-15")
    assert "| Predictions carrying evidence | 1 | 4 | 25.0% |" in text
    assert "| Terminal predictions left ungraded | 1 | 2 | 50.0% |" in text
    assert "| Open primary predictions missing `measurement_method` | 1 | 2 | 50.0% |" in text


def test_flat_vs_daily_source_collision_detected(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    fm = (
        "type: source\n"
        "title: Same Article\n"
        "publisher: Example News\n"
        "published: 2026-07-15"
    )
    _page(vault / "wiki" / "sources" / "2026" / "07" / "article.md", fm)
    _page(vault / "wiki" / "sources" / "2026" / "07" / "15" / "article-copy.md", fm)
    state = mod.audit(vault)
    assert state["source_partition_collisions"] == [[
        "sources/2026/07/article.md",
        "sources/2026/07/15/article-copy.md",
    ]]
    assert "Flat-vs-sharded source collisions" in mod.render(state, "2026-07-16")


def test_review_queue_health_and_malformed_slug_metrics(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(
        vault / "wiki" / "entities" / "a" / "flagged.md",
        "type: entity\nneeds_review: true\ncreated: 2026-07-01",
        "substantive " * 30,
    )
    _page(
        vault / "wiki" / "entities" / "b" / ("run-on-" + "x" * 81 + ".md"),
        "type: entity",
    )
    state = mod.audit(vault)
    assert state["review_queue"]["total"] == 1
    assert state["review_queue"]["substantive"] == 1
    assert state["review_queue"]["fraction"] == 0.5
    assert state["review_queue"]["median_age_days"] is not None
    assert state["malformed_slugs"]["count"] == 1
    text = mod.render(state, "2026-07-16")
    assert "Human-review queue health" in text
    assert "| 1 | 50.0% | 1 |" in text
    assert "Malformed page basenames" in text


def test_body_integrity_detects_malformed_and_reader_derived_headings(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(
        vault / "wiki" / "entities" / "broken.md",
        "type: entity",
        "# Broken\n\n## ## Recent activity\n\n- item\n\n"
        "## Incoming backlinks\n\n- frozen\n\n## References\n\n- frozen too\n",
    )
    _page(
        vault / "wiki" / "entities" / "clean.md",
        "type: entity",
        "# Clean\n\n## Recent activity\n\n- item\n",
    )

    state = mod.audit(vault)

    assert state["body_integrity"] == {
        "malformed_heading_occurrences": 1,
        "malformed_heading_pages": 1,
        "malformed_heading_examples": ["entities/broken.md"],
        "derived_panel_occurrences": 2,
        "derived_panel_pages": 1,
        "derived_panel_examples": ["entities/broken.md"],
        "leaked_frontmatter_pages": 0,
        "leaked_frontmatter_examples": [],
        "aggregator_as_source_pages": 0,
        "aggregator_as_source_examples": [],
        "stale_reliability_pages": 0,
        "stale_reliability_examples": [],
        "malformed_field_key_pages": 0,
        "malformed_field_key_examples": [],
        "retracted_without_successor": 0,
        "retracted_examples": [],
        "judgments_left_silent": 0,
        "subjects_left_silent": 0,
        "judgment_kind_field": "",
        "typed_shape_violations": 0,
        "typed_shape_examples": [],
        "typed_shape_rules": 0,
        "overmatching_identity_pages": 0,
        "overmatching_identity_examples": [],
            "actor_is_a_publisher_pages": 0,
            "actor_is_a_publisher_examples": [],
            "actor_class_contradiction_pages": 0,
            "actor_class_contradiction_examples": [],
        "source_descriptor_pages": 0,
        "source_descriptor_examples": [],
        "overbroad_alias_pages": 0,
        "overbroad_alias_terms": [],
        "overbroad_alias_examples": [],
        "scalar_alias_pages": 0,
    }
    rendered = mod.render(state, "2026-07-16")
    assert "## Body integrity" in rendered
    assert "| Malformed `## ##` headings | 1 | 1 | `entities/broken.md` |" in rendered
    assert "| Reader-derived panels authored as body H2 | 1 | 2 |" in rendered


def test_body_integrity_detects_frontmatter_fragment_leaked_by_substring_split(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "dragonforce.md", "type: entity",
          "---2\n  published_at: 2026-06-22\n- id: leaked\n\n# DragonForce\n")

    state = mod.audit(vault)

    integrity = state["body_integrity"]
    assert integrity["leaked_frontmatter_pages"] == 1
    assert integrity["leaked_frontmatter_examples"] == ["entities/dragonforce.md"]
    assert "Front-matter fragment leaked into body" in mod.render(state, "2026-07-20")


def test_body_integrity_ignores_headings_inside_fenced_examples(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(
        vault / "wiki" / "entities" / "example.md",
        "type: entity",
        "# Example\n\n```markdown\n## ## Recent activity\n## References\n```\n",
    )

    state = mod.audit(vault)

    assert state["body_integrity"]["malformed_heading_pages"] == 0
    assert state["body_integrity"]["derived_panel_pages"] == 0


def test_extensible_enum_is_novel_not_drift(tmp_path):
    """Write-path semantics: extensible enums are legal to extend — out-of-enum values
    there report as NOVEL vocabulary, not drift (mirrors schema_validator's skip)."""
    mod = _load()
    vault = _vault(
        tmp_path,
        """
types:
  source:
    required: [type]
enums:
  stance: [bull, bear]
field_enums:
  stance: {enum: stance, extensible: true}
""",
    )
    _page(vault / "wiki" / "entities" / "a.md", "type: entity\nstance: crab")
    state = mod.audit(vault)
    assert "stance" in state["novel"] and state["novel"]["stance"]["crab"]["count"] == 1
    assert "stance" not in state["drift"]
    text = mod.render(state, "2026-07-15")
    assert "Novel values on extensible vocabularies" in text


def test_indirect_strict_enum_resolves_through_enums_map(tmp_path):
    """The real base-schema shape: field_enums -> {enum: name} -> enums[name] list.
    An in-enum value passes; out-of-enum on a STRICT (non-extensible) rule = drift."""
    mod = _load()
    vault = _vault(
        tmp_path,
        """
types:
  source:
    required: [type]
enums:
  lane: [fast, slow]
field_enums:
  lane: {enum: lane}
""",
    )
    _page(vault / "wiki" / "entities" / "ok.md", "type: entity\nlane: fast")
    _page(vault / "wiki" / "entities" / "bad.md", "type: entity\nlane: sideways")
    state = mod.audit(vault)
    assert state["drift"]["lane"]["sideways"]["count"] == 1
    assert "fast" not in state["drift"].get("lane", {})


def test_no_field_enums_renders_undetectable_not_pass():
    """Missing key = WARN 'undetectable', never a vacuous pass (standing rule). The state
    is near-unreachable on a healthy vault (base-schema always ships field_enums), so the
    render branch is unit-tested directly — the guard must exist and must not read as clean."""
    mod = _load()
    state = {
        "pages": 1, "parse_errors": 0, "drift": {}, "novel": {},
        "populated": {f: 0 for f in mod.CONSUMED_FIELDS},
        "candidates": {f: 0 for f in mod.CONSUMED_FIELDS},
        "enums_declared": False,
    }
    text = mod.render(state, "2026-07-15")
    assert "UNDETECTABLE" in text
    assert "None — every audited value" not in text


def test_operational_namespaces_skipped(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "dashboards" / "x.md", "type: entity\nseverity: bogus")
    _page(vault / "wiki" / "sources" / "_archived" / "old.md", "type: source\nseverity: bogus")
    state = mod.audit(vault)
    assert not state["drift"], "operational/_archived pages must not be audited"
    assert state["candidates"]["signal_class"] == 0, "_archived sources are not candidates"


def test_recent_drift_flags_active_producer_regression(tmp_path):
    """okengine#237 standing lint: drift on a recently created/updated page renders the
    ACTIVE PRODUCER REGRESSION alert + per-row recent count; legacy drift does not."""
    from datetime import date
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    today = date.today().isoformat()
    _page(vault / "wiki" / "entities" / "new.md",
          f"type: entity\nseverity: bogus\ncreated: '{today}'")
    _page(vault / "wiki" / "entities" / "old.md",
          "type: entity\nseverity: bogus\ncreated: '2020-01-01'\nupdated: '2020-01-02'")
    state = mod.audit(vault)
    assert state["drift"]["severity"]["bogus"]["count"] == 2
    assert state["drift"]["severity"]["bogus"]["recent"] == 1
    text = mod.render(state, today)
    assert "ACTIVE PRODUCER REGRESSION" in text
    # legacy-only drift: no alert
    state["drift"]["severity"]["bogus"]["recent"] = 0
    assert "ACTIVE PRODUCER REGRESSION" not in mod.render(state, today)


# --- off-taxonomy types + entity fragmentation (the Gentlemen / Storm-2697 repro) ---

SCHEMA_ACTORS = """
types:
  actor:
    required: [type]
  malware:
    required: [type]
"""


def test_off_taxonomy_type_flagged(tmp_path):
    """A `type` outside base ∪ pack (STIX-style `threat-actor_group`) is flagged; a declared
    pack type (`actor`) and a base type (`source`) are not."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_ACTORS)
    _page(vault / "wiki" / "entities" / "a" / "good.md", "type: actor\nname: Good")
    _page(vault / "wiki" / "entities" / "t" / "bad.md", "type: threat-actor_group\nname: Bad")
    _page(vault / "wiki" / "sources" / "2026" / "07" / "01" / "s.md",
          "type: source\npublished: 2026-07-01")
    state = mod.audit(vault)
    assert state["off_taxonomy"]["threat-actor_group"]["count"] == 1
    assert state["off_taxonomy"]["threat-actor_group"]["examples"] == ["entities/t/bad.md"]
    assert "actor" not in state["off_taxonomy"], "a declared pack type must not be flagged"
    assert "source" not in state["off_taxonomy"], "a base type must not be flagged"
    assert "Entity types outside the schema taxonomy" in mod.render(state, "2026-07-16")


def test_alias_fragmentation_clusters_shared_alias(tmp_path):
    """Entity pages sharing a normalized alias cluster together; an unrelated entity does not."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_ACTORS)
    _page(vault / "wiki" / "entities" / "g" / "gentlemen-storm-2698.md",
          "type: actor\nname: The Gentlemen\naliases:\n  - Gentlemen\n  - Storm-2697")
    _page(vault / "wiki" / "entities" / "g" / "gentlemen-ransomware-group.md",
          "type: actor\nname: Gentlemen Ransomware Group\naliases:\n  - The Gentlemen")
    _page(vault / "wiki" / "entities" / "t" / "the-gentlemen.md",
          "type: actor\naliases:\n  - The Gentlemen")
    _page(vault / "wiki" / "entities" / "l" / "lazarus.md", "type: actor\nname: Lazarus Group")
    state = mod.audit(vault)
    g = [c for c in state["fragmentation"] if "gentlemen" in c["shared"]]
    assert g, state["fragmentation"]
    assert len(g[0]["members"]) == 3
    assert not any("lazarus" in m for c in state["fragmentation"] for m in c["members"])
    assert "Same-entity fragmentation" in mod.render(state, "2026-07-16")


def test_fragmentation_is_not_transitively_over_merged(tmp_path):
    """An alias-rich BRIDGE page (listing two distinct actors' aliases) must NOT collapse those
    actors into one blob — per-shared-key clustering keeps them separate (the union-find bug)."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_ACTORS)
    _page(vault / "wiki" / "entities" / "o" / "oilrig.md", "type: actor\nname: OilRig")
    _page(vault / "wiki" / "entities" / "a" / "apt34.md", "type: actor\naliases:\n  - OilRig")
    _page(vault / "wiki" / "entities" / "w" / "winnti.md", "type: actor\nname: Winnti")
    _page(vault / "wiki" / "entities" / "a" / "apt41.md", "type: actor\naliases:\n  - Winnti")
    _page(vault / "wiki" / "entities" / "b" / "bridge.md",
          "type: actor\naliases:\n  - OilRig\n  - Winnti")
    state = mod.audit(vault)
    oil = [c for c in state["fragmentation"] if "oilrig" in c["shared"]][0]
    win = [c for c in state["fragmentation"] if "winnti" in c["shared"]][0]
    assert not any("winnti.md" in m for m in oil["members"]), oil
    assert not any("oilrig.md" in m for m in win["members"]), win


def test_short_identity_tokens_do_not_cluster(tmp_path):
    """Tokens under MIN_IDENTITY_LEN are too generic to join on (no false 'apt' mega-cluster)."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_ACTORS)
    _page(vault / "wiki" / "entities" / "a" / "a1.md", "type: actor\naliases:\n  - APT")
    _page(vault / "wiki" / "entities" / "a" / "a2.md", "type: actor\naliases:\n  - APT")
    state = mod.audit(vault)
    assert not any("apt" in c["shared"] for c in state["fragmentation"])


def test_tombstoned_pages_excluded_from_both_detectors(tmp_path):
    """A tombstoned page is the RESOLUTION of these defects (a merged dup pointing at its
    canonical), not an instance — so it must not keep the cluster alive or flag its old type,
    or tombstoning could never clear the signal."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_ACTORS)
    _page(vault / "wiki" / "entities" / "g" / "canonical.md",
          "type: actor\nname: The Gentlemen\naliases:\n  - Gentlemen")
    # tombstoned dup: shares the alias AND carries an out-of-taxonomy type — both must be ignored.
    _page(vault / "wiki" / "entities" / "t" / "old-dup.md",
          "type: threat-actor_group\nname: The Gentlemen\nstatus: tombstoned\n"
          "redirect_to: entities/g/canonical")
    state = mod.audit(vault)
    assert not any("gentlemen" in c["shared"] for c in state["fragmentation"]), \
        "a tombstoned dup must not keep the fragmentation cluster alive"
    assert "threat-actor_group" not in state["off_taxonomy"], \
        "a tombstoned page's out-of-taxonomy type must not be flagged"


SCHEMA_WITH_COVERAGE = """
types:
  cve:
    required: [type]
field_enums:
  severity: [low, medium, high, critical]
coverage_fields:
  - {type: cve, field: cvss_base, min: 0.8}
  - {type: cve, field: severity}
"""


def test_field_coverage_ratio_floor_and_tombstone_exclusion(tmp_path):
    """okengine#264: schema-declared coverage_fields yield a population ratio per (type, field);
    below `min` flags BELOW FLOOR; tombstoned pages are excluded from the denominator."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_COVERAGE)
    for n in "abc":                                   # 3 fully-enriched CVE pages
        _page(vault / "wiki" / "cves" / f"{n}.md", f"type: cve\ncvss_base: 9.8\nseverity: critical")
    _page(vault / "wiki" / "cves" / "d.md", "type: cve")        # missing both -> drags coverage
    _page(vault / "wiki" / "cves" / "z.md",                     # tombstone: excluded from totals
          "type: cve\nstatus: tombstoned\ntombstone_reason: merged")
    state = mod.audit(vault)
    assert state["coverage_declared"] is True
    cov = state["coverage"]
    assert cov["cve.cvss_base"]["have"] == 3 and cov["cve.cvss_base"]["total"] == 4   # tombstone excluded
    assert abs(cov["cve.cvss_base"]["ratio"] - 0.75) < 1e-9
    assert cov["cve.cvss_base"]["min"] == 0.8 and cov["cve.severity"]["min"] is None
    assert cov["cve.severity"]["have"] == 3
    text = mod.render(state, "2026-07-17")
    assert "## Field coverage" in text
    row = [l for l in text.splitlines() if "`cve.cvss_base`" in l][0]
    assert "BELOW FLOOR" in row and "1 page(s) missing" in row   # 0.75 < 0.80 floor
    sev_row = [l for l in text.splitlines() if "`cve.severity`" in l][0]
    assert "BELOW FLOOR" not in sev_row                          # no floor declared -> never flagged


def test_field_coverage_undetectable_without_schema_key(tmp_path):
    """The vacuous-pass guard: a vault whose schema declares no coverage_fields reports UNDETECTABLE
    for coverage (a WARN, not a silent clean pass) — the missing-key = WARN rule."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)        # has field_enums, NO coverage_fields
    _page(vault / "wiki" / "cves" / "a.md", "type: cve\ncvss_base: 9.8")
    state = mod.audit(vault)
    assert state["coverage_declared"] is False and state["coverage"] == {}
    text = mod.render(state, "2026-07-17")
    assert "no governing schema declares `coverage_fields`" in text   # coverage section says UNDETECTABLE


def test_wake_sentinel_is_valid_json(tmp_path, capsys):
    """HIGH #8: corpus_audit's no_agent sentinel must be JSON the Hermes wake-gate can parse, not a
    bare 'wakeAgent=false' string (which json.loads rejects → the gate fails open and delivers an
    unwanted output doc). Exercises the no-wiki early return; the completion path uses the same call."""
    import json
    mod = _load()
    mod.VAULT = tmp_path
    mod.WIKI = tmp_path / "wiki"          # absent -> the early-return sentinel path
    rc = mod.main()
    assert rc == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last) == {"wakeAgent": False}


def test_dangling_path_reference_detected(tmp_path):
    """okengine#336: a bare frontmatter path (e.g. an assessment `subject:`) that no longer
    resolves to a page — the reshard-orphan class — must surface as a standing row."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    # entity now lives a shard deeper (post-reshard)
    _page(vault / "wiki" / "entities" / "a" / "d" / "admin-338.md", "type: entity")
    # assessment still points at the PRE-reshard path via a bare subject: scalar -> dangling
    _page(vault / "wiki" / "assessments" / "a" / "x.md",
          "type: assessment\nsubject: entities/a/admin-338\nsubject_ref: G0018")
    # a second assessment points at the CURRENT path -> must NOT be flagged
    _page(vault / "wiki" / "assessments" / "a" / "y.md",
          "type: assessment\nsubject: entities/a/d/admin-338")
    state = mod.audit(vault)
    d = state["dangling_refs"]
    assert "subject" in d, "stale bare subject: path must be flagged"
    assert d["subject"]["count"] == 1
    assert d["subject"]["examples"] == ["assessments/a/x.md → entities/a/admin-338"]
    # the resolvable subject, and the non-path subject_ref (G0018), must NOT be flagged
    assert "subject_ref" not in d
    text = mod.render(state, "2026-07-15")
    assert "Dangling path references" in text
    assert "okengine#336" in text


def test_no_dangling_when_all_paths_resolve(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "h" / "hafnium.md", "type: entity")
    _page(vault / "wiki" / "assessments" / "h" / "x.md",
          "type: assessment\nsubject: entities/h/hafnium")
    state = mod.audit(vault)
    assert state["dangling_refs"] == {}
    assert "None — every bare" in mod.render(state, "2026-07-15")


def test_parser_and_schema_helper_edges(tmp_path, monkeypatch):
    mod = _load()
    assert mod._enum_rules({"field_enums": {"direct": ["a", "b"], "bad": "x"}}) == {
        "direct": ({"a", "b"}, False),
    }
    specs = mod._coverage_specs({"coverage_fields": [
        "bad", {}, {"type": "x", "field": "f", "min": "bad"},
    ]})
    assert specs == [("x", "f", None)]
    assert list(mod._iter_pathrefs({
        "one": "entities/a/item.md", "many": [3, "https://x", "entities/b/other"],
    })) == [("one", "entities/a/item"), ("many", "entities/b/other")]

    missing = tmp_path / "missing.md"
    assert mod._frontmatter(missing) is None
    plain = tmp_path / "plain.md"
    plain.write_text("body")
    assert mod._frontmatter(plain) is None
    bad = tmp_path / "bad.md"
    bad.write_text("---\n[bad\n---\n")
    assert mod._frontmatter(bad) == {}


def test_audit_skips_parse_failures_races_and_irrelevant_coverage_specs(tmp_path, monkeypatch):
    mod = _load()
    vault = _vault(tmp_path, """
types: {entity: {}, other: {}}
coverage_fields:
  - {type: other, field: score, min: 0.5}
""")
    wiki = vault / "wiki"
    (wiki / "plain.md").write_text("body")
    (wiki / "bad.md").write_text("---\n[bad\n---\n")
    page = wiki / "entities" / "a.md"
    _page(page, "type: entity\nname: Alpha\nevidence: [scalar]\nsubject: external/path")
    original = mod.Path.read_text
    reads = {page: 0}
    def race_on_second(self, *args, **kwargs):
        if self == page:
            reads[page] += 1
            if reads[page] == 2:
                raise OSError("moved")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(mod.Path, "read_text", race_on_second)
    state = mod.audit(vault)
    assert state["parse_errors"] == 1
    assert state["pages"] == 2
    assert state["coverage_declared"] is True and state["coverage"] == {}
    assert state["dangling_refs"] == {}, "unknown top-level namespaces are not wiki refs"


def test_render_declared_empty_coverage_and_off_taxonomy_overflow(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, "types: {}\ncoverage_fields: [{type: x, field: f}]\n")
    state = mod.audit(vault)
    state["coverage_declared"] = True
    state["coverage"] = {}
    state["off_taxonomy"] = {
        f"unknown-{i}": {"count": 1, "recent": 0, "examples": []}
        for i in range(mod.MAX_CLUSTERS + 1)
    }
    text = mod.render(state, "2026-08-01")
    assert "no pages of the declared type" in text
    assert "more type value" in text


def test_main_completion_writes_dashboard_and_json_sentinel(tmp_path, capsys):
    import json
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "a.md", "type: entity\nname: Alpha")
    mod.VAULT = vault
    mod.WIKI = vault / "wiki"
    mod.DASH_DIR = vault / "wiki" / "dashboards"
    assert mod.main() == 0
    assert (mod.DASH_DIR / "corpus-audit.md").is_file()
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"wakeAgent": False}


def test_example_caps_invalid_review_dates_and_duplicate_shared_identity_keys(tmp_path):
    mod = _load()
    schema = """
types: {entity: {}}
enums: {status_values: [open]}
field_enums:
  status: {enum: status_values, extensible: false}
"""
    vault = _vault(tmp_path, schema)
    count = mod.MAX_EXAMPLES + 1
    for i in range(count):
        _page(
            vault / "wiki" / "entities" / f"bad slug {i}.md",
            "type: unknown-type\nname: Shared Identity\naliases: [Shared Identity, Shared Alias]\n"
            "status: drift\nneeds_review: true\ncreated: not-a-date\n"
            f"subject: entities/missing-{i}\nevidence: [scalar, {{direction: sideways}}]",
            "---2\n leaked: yes\n## ## Broken\n## References\n",
        )
    state = mod.audit(vault)
    integrity = state["body_integrity"]
    assert len(integrity["leaked_frontmatter_examples"]) == mod.MAX_EXAMPLES
    assert len(integrity["malformed_heading_examples"]) == mod.MAX_EXAMPLES
    assert len(integrity["derived_panel_examples"]) == mod.MAX_EXAMPLES
    assert len(state["malformed_slugs"]["examples"]) == mod.MAX_EXAMPLES
    assert len(state["off_taxonomy"]["unknown-type"]["examples"]) == mod.MAX_EXAMPLES
    assert len(state["dangling_refs"]["subject"]["examples"]) == mod.MAX_EXAMPLES
    assert any(len(cluster["shared"]) >= 2 for cluster in state["fragmentation"])
    assert state["review_queue"]["median_age_days"] is None


def test_fence_and_empty_enum_rule_false_sides(tmp_path, monkeypatch):
    mod = _load()
    assert mod._body_integrity_counts("````\n```\n## References\n") == (0, 0)
    assert mod._enum_rules({
        "enums": {}, "field_enums": {"missing": {"enum": "absent"}},
    }) == {}
    vault = _vault(tmp_path, "types: {entity: {}}\n")
    monkeypatch.setattr(mod.schema_lib, "merged_schema", lambda *_a: {"types": {"entity": {}}})
    _page(vault / "wiki" / "entities" / "short.md",
          "type: entity\nneeds_review: true\ncreated: 2026-08-01", "short")
    state = mod.audit(vault)
    assert state["review_queue"]["substantive"] == 0


# --- okengine#563: an aggregator credited as the source -------------------------------------------
def _mkpage(root, rel, fm_lines, body=""):
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + fm_lines + "---\n\n" + body, encoding="utf-8")


def test_aggregator_credited_as_source_is_flagged(tmp_path):
    """A repository that CARRIES data does not own it: the record models that already
    (`publisher` = originator, `retrieved_via` = carrier). Body prose crediting the carrier
    misattributes the originator and invents a dataset that does not exist."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    _mkpage(tmp_path, "sources/s1.md",
            "type: source\nid: s:1\npublisher: Malpedia\nretrieved_via: SOMEHUB\n")
    _mkpage(tmp_path, "entities/a/bad.md", "type: actor\nid: e:bad\nname: Bad\n",
            "Bad is a threat actor identified in the SOMEHUB dataset.\n")
    # a real dataset that is NOT a carrier must NOT be flagged
    _mkpage(tmp_path, "entities/a/ok.md", "type: actor\nid: e:ok\nname: Ok\n",
            "Ok is a threat actor described in the Malpedia database.\n")

    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["aggregator_as_source_pages"] == 1, bi
    assert any("bad.md" in e for e in bi["aggregator_as_source_examples"]), bi
    assert not any("ok.md" in e for e in bi["aggregator_as_source_examples"]), (
        "a non-carrier dataset mentioned in prose must not be flagged")


def test_carrier_names_are_learned_from_the_corpus_not_hardcoded(tmp_path):
    """The engine ships no domain knowledge: with no `retrieved_via` anywhere, nothing is a carrier
    and the same sentence is not a finding."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _mkpage(tmp_path, "entities/a/bad.md", "type: actor\nid: e:bad\nname: Bad\n",
            "Bad is a threat actor identified in the SOMEHUB dataset.\n")
    assert m.audit(tmp_path)["body_integrity"]["aggregator_as_source_pages"] == 0


def test_page_reliability_worse_than_its_own_evidence_is_flagged(tmp_path):
    """Admiralty `reliability` grades a SOURCE. Stamped onto a knowledge page it is a snapshot of
    whatever produced that page and never moves as evidence accumulates — so the trust strip shows
    a stale, LOWER grade beside a basis naming A-grade authorities. The page argues with itself and
    the reader is shown the weaker claim (72 pages on one live vault; an A-sourced nation-state
    actor displaying Rel C)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    _mkpage(tmp_path, "entities/a/stale.md",
            "type: actor\nid: e:stale\nname: Stale\nreliability: C\n"
            'auto_verified_basis: "2 A-grade sources: MITRE ATT&CK, Microsoft"\n')
    _mkpage(tmp_path, "entities/a/agrees.md",
            "type: actor\nid: e:ag\nname: Agrees\nreliability: A\n"
            'auto_verified_basis: "1 A-grade source: Microsoft"\n')
    _mkpage(tmp_path, "entities/a/better.md",
            "type: actor\nid: e:bt\nname: Better\nreliability: A\n"
            'auto_verified_basis: "2 B-grade sources: X, Y"\n')

    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["stale_reliability_pages"] == 1, bi
    assert any("stale.md" in e for e in bi["stale_reliability_examples"]), bi


def test_reliability_without_a_basis_is_not_flagged(tmp_path):
    """No basis means nothing to contradict — flagging it would fire on every unverified page."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _mkpage(tmp_path, "entities/a/nobasis.md", "type: actor\nid: e:nb\nname: NB\nreliability: C\n")
    assert m.audit(tmp_path)["body_integrity"]["stale_reliability_pages"] == 0


def test_malformed_frontmatter_field_keys_are_flagged(tmp_path):
    """A frontmatter KEY is an identifier, not prose. Deliberately NOT keyed on "undeclared": the
    schema declares 223 field names while the corpus uses 1169, so undeclared-alone is ~1000
    findings that are mostly schema gaps (`url` on 25k pages). These two shapes are wrong
    regardless of how complete the schema is."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    _mkpage(tmp_path, "entities/a/charset.md", 'type: actor\nid: e:c\nname: C\n"slug\\"": x\n')
    _mkpage(tmp_path, "entities/a/prose.md",
            "type: actor\nid: e:p\nname: P\n"
            "confidence_numeric_approximately_zero_point_seven_five: 0.75\n")
    _mkpage(tmp_path, "entities/a/clean.md", "type: actor\nid: e:ok\nname: Ok\naliases: [x]\n")

    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["malformed_field_key_pages"] == 2, bi
    assert not any("clean.md" in e for e in bi["malformed_field_key_examples"]), bi


def test_an_ordinary_undeclared_field_is_not_flagged(tmp_path):
    """`url` is undeclared on 25k pages and perfectly legitimate — a schema gap, not invention.
    Flagging it would bury the real signal under a thousand false positives."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    _mkpage(tmp_path, "entities/a/u.md",
            "type: actor\nid: e:u\nname: U\nurl: https://example.invalid/x\nmalware_family: y\n")
    assert m.audit(tmp_path)["body_integrity"]["malformed_field_key_pages"] == 0


def test_identity_and_storage_fields_are_not_reported_as_dangling_refs(tmp_path):
    """`id` is an IDENTITY (path-style ids resolve through the id-index, not the filesystem) and
    `raw` is a storage location. Counting them as graph edges made the dangling-ref metric 87%
    noise on one live vault — 496 of 568 findings, `id` alone contributing 397 — which buried the
    72 real ones."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    # a real page in each namespace, so the namespace itself is recognised by the dangling check
    _mkpage(tmp_path, "concepts/c/real.md", "type: concept\nid: c:real\nname: Real\n")
    _mkpage(tmp_path, "entities/a/x.md",
            "type: actor\nid: entities/a/does-not-exist\nname: X\n"
            "raw: raw/2026/does-not-exist\nsee_also: concepts/also-missing\n")
    d = m.audit(tmp_path).get("dangling_refs") or {}
    assert "id" not in d, "an identity is not a graph edge"
    assert "raw" not in d, "a storage path is not a graph edge"
    assert d.get("see_also", {}).get("count") == 1, "a real reference must still be reported"


def _shape_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "corpus_audit", Path(__file__).resolve().parents[2] / "scripts" / "cron" / "corpus_audit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_per_type_field_shape_is_checked(tmp_path):
    """okengine#563: a field can mean different things on different types — `confidence` is a
    numeric probability on an assessment (804/804 on one live vault) and a qualitative band
    elsewhere — so ONE global shape cannot fit. Consumers guard with isinstance(), so a drifted
    string does not crash: it silently renders as "—" and drops out of the scoring math."""
    (tmp_path / "schema.yaml").write_text(
        "types:\n  assessment: {required: [type]}\n  source: {required: [type]}\n"
        "field_shapes:\n  confidence:\n    by_type:\n      assessment: number\n", encoding="utf-8")
    _mkpage(tmp_path, "assessments/a/good.md", "type: assessment\nid: a:g\nconfidence: 0.65\n")
    _mkpage(tmp_path, "assessments/a/bad.md", "type: assessment\nid: a:b\nconfidence: moderate\n")
    # the same value on a type with NO rule is untouched — that is the whole point of by_type
    _mkpage(tmp_path, "sources/s.md", "type: source\nid: s:1\nconfidence: high\n")
    bi = _shape_mod().audit(tmp_path)["body_integrity"]
    assert bi["typed_shape_rules"] == 1
    assert bi["typed_shape_violations"] == 1, bi
    assert any("bad.md" in e for e in bi["typed_shape_examples"]), bi


def test_no_shape_rules_reports_undetectable_not_zero(tmp_path):
    """The missing-key rule: with nothing declared the section must say UNDETECTABLE rather than
    print a clean zero that looks like a pass."""
    (tmp_path / "schema.yaml").write_text("types:\n  actor: {required: [type]}\n", encoding="utf-8")
    _mkpage(tmp_path, "entities/a/x.md", "type: actor\nid: e:x\nconfidence: whatever\n")
    m = _shape_mod()
    state = m.audit(tmp_path)
    assert state["body_integrity"]["typed_shape_rules"] == 0
    assert "undetectable" in m.render(state, "2026-08-07")


def test_a_judgment_retracted_with_no_successor_is_flagged(tmp_path):
    """okengine#563: a judgment is retired only BY another judgment — attribution changes when
    EVIDENCE changes, and the superseded_by chain IS that reviewable history. A record marked
    superseded with no successor retracted a live claim on the strength of nothing. One lane run
    did exactly that to 344 subjects (created 0, reported "success") and nothing noticed, because
    no detector watched the resulting state."""
    m = _shape_mod()
    _mkpage(tmp_path, "entities/a/x.md", "type: actor\nid: e:x\nname: X\n")
    # retracted by nothing, and its subject has nothing else -> the subject falls SILENT
    _mkpage(tmp_path, "assessments/a/gone.md",
            "type: assessment\nid: a:g\nsubject: entities/a/x\nstatus: superseded\n"
            "superseded_by: ''\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 1, bi
    assert bi["subjects_left_silent"] == 1, bi


def test_a_list_valued_subject_matches_its_scalar_spelling(tmp_path):
    """Both spellings of `subject` are live in the corpus (a live vault carries
    `subject: ['entities:x']` alongside `subject: entities/a/z`). Stringifying the list yields
    "['entities/a/z']", which can never equal the scalar form of the SAME subject — so a subject
    retracted in one spelling and re-asserted in the other reads as permanently silent. Caught by
    running the detector against the real vault before shipping it, not by a fixture."""
    m = _shape_mod()
    _mkpage(tmp_path, "entities/a/z.md", "type: actor\nid: e:z\nname: Z\n")
    _mkpage(tmp_path, "assessments/a/old2.md",
            "type: assessment\nid: a:o2\nsubject: [entities/a/z]\nstatus: superseded\n"
            "superseded_by: ''\n")
    # the live claim uses the SCALAR spelling of that same subject
    _mkpage(tmp_path, "assessments/a/live2.md",
            "type: assessment\nid: a:l2\nsubject: entities/a/z\nstatus: active\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 1, bi      # the successorless retraction still counts
    assert bi["subjects_left_silent"] == 0, bi             # but the subject is NOT silent
    assert "['entities/a/z']" not in " ".join(bi["retracted_examples"]), bi


def test_silence_is_measured_per_judgment_kind_not_per_subject(tmp_path, monkeypatch):
    """A subject carries SEVERAL independent judgments. An actor whose ORIGIN judgment was
    retracted still holds a live identity judgment — per-subject counting reads that as covered
    while the origin is in fact gone. On the live vault the coarse grain reported 1 and the true
    grain reported 38, including five actors left with no origin attribution at all.

    The kind FIELD is pack vocabulary, so the engine takes it as config (the engine layer ships no
    domain field names) and reports the coarser grain honestly when it has none."""
    monkeypatch.setenv("CORPUS_AUDIT_JUDGMENT_KIND_FIELD", "assessment_kind")
    m = _shape_mod()
    _mkpage(tmp_path, "entities/a/w.md", "type: actor\nid: e:w\nname: W\n")
    # origin judgment: retracted, nothing replaces it -> SILENT
    _mkpage(tmp_path, "assessments/a/origin.md",
            "type: assessment\nid: a:or\nsubject: entities/a/w\n"
            "assessment_kind: actor-country-linkage\nstatus: superseded\nsuperseded_by: ''\n")
    # a DIFFERENT kind of judgment on the same subject is alive — it must not mask the above
    _mkpage(tmp_path, "assessments/a/identity.md",
            "type: assessment\nid: a:id\nsubject: entities/a/w\n"
            "assessment_kind: actor-identity-scope\nstatus: active\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 1, bi
    assert bi["judgments_left_silent"] == 1, bi          # the origin judgment IS silent
    assert bi["judgment_kind_field"] == "assessment_kind"
    assert "judgment_grain_warning" not in bi           # configured -> no caveat needed
    assert "actor-country-linkage" in " ".join(bi["retracted_examples"]), bi


def test_unconfigured_kind_field_reports_the_coarse_grain_as_coarse(tmp_path, monkeypatch):
    """The missing-key rule: with no kind field the audit still produces a number, but it must
    declare that the number is a floor. A silent coarse count is how "1 silent" gets read as "the
    corpus is fine" when 38 judgments are actually unasserted."""
    monkeypatch.delenv("CORPUS_AUDIT_JUDGMENT_KIND_FIELD", raising=False)
    m = _shape_mod()
    _mkpage(tmp_path, "entities/a/v.md", "type: actor\nid: e:v\nname: V\n")
    _mkpage(tmp_path, "assessments/a/o2.md",
            "type: assessment\nid: a:o3\nsubject: entities/a/v\n"
            "assessment_kind: actor-country-linkage\nstatus: superseded\nsuperseded_by: ''\n")
    _mkpage(tmp_path, "assessments/a/i2.md",
            "type: assessment\nid: a:i3\nsubject: entities/a/v\n"
            "assessment_kind: actor-identity-scope\nstatus: active\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 1, bi
    assert bi["judgments_left_silent"] == 0, bi          # genuinely indistinguishable at this grain
    assert "under-report" in bi["judgment_grain_warning"]   # ...and it SAYS so


def test_a_real_supersession_chain_is_not_flagged(tmp_path):
    """The history the model WANTS: an older judgment superseded by a named newer one, with the
    subject still holding a live claim. That must never read as a defect."""
    m = _shape_mod()
    _mkpage(tmp_path, "entities/a/y.md", "type: actor\nid: e:y\nname: Y\n")
    _mkpage(tmp_path, "assessments/a/old.md",
            "type: assessment\nid: a:o\nsubject: entities/a/y\nstatus: superseded\n"
            "superseded_by: assessments/a/new\n")
    _mkpage(tmp_path, "assessments/a/new.md",
            "type: assessment\nid: a:n\nsubject: entities/a/y\nstatus: active\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 0, bi
    assert bi["subjects_left_silent"] == 0, bi


def _partition_vault(tmp_path, strategy="by-letter"):
    """A vault whose `things` namespace is partitioned, so canonical placement is defined."""
    (tmp_path / "schema.yaml").write_text(
        "types: {thing: {}}\n"
        f"partitioning:\n  reshard_over: 500\n  namespaces:\n    things: {{strategy: {strategy}}}\n",
        encoding="utf-8")
    return tmp_path


def test_duplicates_are_left_to_the_check_that_already_owns_them(tmp_path):
    """DUPLICATE detection is deployment_checks.check_partition_dups() — same namespaces, same stem
    grouping, and it additionally skips tombstoned pages (intentionally left at a stale path).

    A second implementation of one check is how the two drift: this file's first attempt lacked the
    structural-page skip the existing lane had carried all along, and a converge step built on that
    reading deleted ~1,600 generated INDEX pages in one pass. So this audit reports only the signal
    the other check CANNOT see — see the misfiled tests below."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/a/abc123.md", "type: thing\ntitle: A\n")        # the canonical seat
    _mkpage(v, "things/kind/abc123.md", "type: thing\ntitle: A\n")     # a hand-built path
    r = m.audit(v)
    assert "partition_collisions" not in r and "partition_collision_count" not in r
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]   # a dup is not "misfiled"


def test_a_page_off_its_canonical_seat_is_caught_before_it_duplicates(tmp_path):
    """`misfiled` is the EARLIER signal — the same defect one drain-run before it becomes a
    collision. Catching it here is the difference between a move and a merge."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/kind/zebra.md", "type: thing\ntitle: Z\n")      # belongs at things/z/zebra
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 1, r["partition_misfiled"]
    assert "things/z/zebra" in r["partition_misfiled"][0]


def test_a_reshard_refinement_is_not_misfiled(tmp_path):
    """canonical_key returns the BASE seat; a namespace over `reshard_over` is legitimately refined
    deeper by `reshard_by`. Without this, the check called 13,214 correctly-resharded pages defects
    on one live vault — a number confidently wrong enough to bury the 74 real collisions."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/z/e/zebra.md", "type: thing\ntitle: Z\n")       # second-letter refinement
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]


def test_a_flat_namespace_may_carry_sub_segments(tmp_path):
    """`is_partitioned`'s own contract: flat namespaces 'may legitimately carry sub-segments'.
    Reporting those as misfiled would make the check fire on every correctly-built pack layout."""
    m = _shape_mod()
    (tmp_path / "schema.yaml").write_text(
        "types: {thing: {}}\n"
        "partitioning:\n  namespaces:\n    things: {strategy: flat}\n", encoding="utf-8")
    _mkpage(tmp_path, "things/kind/abc.md", "type: thing\ntitle: A\n")
    r = m.audit(tmp_path)
    assert r["partition_misfiled_count"] == 0


def test_reserved_meta_pages_are_never_sharded(tmp_path):
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/_about.md", "type: thing\ntitle: About\n")
    assert m.audit(v)["partition_misfiled_count"] == 0


def test_frontmatter_dependent_strategies_are_declared_unverified_not_passed(tmp_path):
    """by-date reads frontmatter this walk does not retain, so canonical_key would fall back and
    call every dated page misfiled. The check names what it could not verify rather than emitting a
    confidently wrong zero (the missing-key = WARN rule)."""
    m = _shape_mod()
    v = _partition_vault(tmp_path, strategy="by-date")
    _mkpage(v, "things/2026/07/abc.md", "type: thing\ntitle: A\npublished: 2026-07-01\n")
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0
    assert r["partition_misfiled_unchecked"] == ["things (by-date)"]


def test_per_directory_structural_pages_are_never_a_collision(tmp_path):
    """build_index_tree writes an INDEX.md at EVERY directory level by design — one per directory
    is the contract, not a duplicate. Grouping them by basename makes every shard's INDEX look like
    one enormous collision set.

    This is not a cosmetic false positive. A converge step keyed on that reading deleted ~1,600
    index pages across every namespace of a live vault in a single pass, because a group of 30
    INDEX.md files read as 'one record with 30 copies'."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    for shard in ("a", "b", "c"):
        _mkpage(v, f"things/{shard}/INDEX.md", "type: dashboard\ntitle: Index\n")
        _mkpage(v, f"things/{shard}/INDEX-p02.md", "type: dashboard\ntitle: Index p2\n")
        _mkpage(v, f"things/{shard}/HEALTH.md", "type: dashboard\ntitle: Health\n")
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]


def test_a_real_duplicate_is_still_caught_alongside_structural_pages(tmp_path):
    """The exclusion must not become a blanket amnesty — real content duplicates still fire."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    for shard in ("a", "k"):
        _mkpage(v, f"things/{shard}/INDEX.md", "type: dashboard\ntitle: Index\n")
    _mkpage(v, "things/kind/zebra.md", "type: thing\ntitle: Z\n")      # misfiled, not duplicated
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 1, r["partition_misfiled"]


def test_a_resharded_subject_path_is_the_same_subject(tmp_path):
    """A partitioned namespace re-files pages as it grows: `entities/b/x` becomes `entities/b/l/x`
    on a second-letter reshard. Records written either side of that move cite different spellings
    of the SAME page, and compared literally they read as two subjects — so an actor with a live
    judgment under one spelling and a retraction under the other looks silent while nothing is
    missing. A live vault reported exactly that for an actor holding two live judgments."""
    m = _shape_mod()
    _mkpage(tmp_path, "entities/b/l/blackcat.md", "type: actor\nid: e:b\nname: BlackCat\n")
    # retraction cites the RESHARDED spelling...
    _mkpage(tmp_path, "assessments/a/old.md",
            "type: assessment\nid: a:o\nsubject: entities/b/l/blackcat\n"
            "assessment_kind: actor-identity-scope\nstatus: superseded\nsuperseded_by: ''\n")
    # ...the live judgment cites the pre-reshard spelling of the same page
    _mkpage(tmp_path, "assessments/a/live.md",
            "type: assessment\nid: a:l\nsubject: entities/b/blackcat\n"
            "assessment_kind: actor-identity-scope\nstatus: active\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 1, bi
    assert bi["judgments_left_silent"] == 0, bi     # NOT silent — one subject, one live judgment


def test_distinct_subjects_are_still_distinct(tmp_path):
    """Normalizing to (namespace, slug) must not merge genuinely different subjects."""
    m = _shape_mod()
    _mkpage(tmp_path, "assessments/a/one.md",
            "type: assessment\nid: a:1\nsubject: entities/a/alpha\n"
            "assessment_kind: k\nstatus: superseded\nsuperseded_by: ''\n")
    _mkpage(tmp_path, "assessments/a/two.md",
            "type: assessment\nid: a:2\nsubject: entities/b/bravo\n"
            "assessment_kind: k\nstatus: active\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["judgments_left_silent"] == 1, bi     # alpha IS silent; bravo does not cover it


def test_a_subject_ref_with_no_namespace_is_still_a_subject(tmp_path):
    """Subject identity is (namespace, slug) with the shard segments in between ignored. A ref that
    carries no namespace at all has no segments to ignore — it must still identify a subject, not
    collapse to the empty key and quietly merge with every other malformed ref."""
    m = _shape_mod()
    _mkpage(tmp_path, "entities/a/x.md", "type: actor\nid: e:x\nname: X\n")
    _mkpage(tmp_path, "assessments/a/bare.md",
            "type: assessment\nid: a:b\nsubject: blackcat\nstatus: superseded\n"
            "superseded_by: ''\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["retracted_without_successor"] == 1, bi
    assert bi["subjects_left_silent"] == 1, bi


def test_an_underscore_slug_is_treated_as_reserved_not_misfiled(tmp_path):
    """`_`-prefixed pages are meta, not content — they have no canonical seat to be off. Only
    `_about` is in the structural set, so anything else underscore-prefixed reaches the seat check
    and must be recognised there too."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/_draft.md", "type: thing\ntitle: Draft\n")
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]


def test_a_page_already_on_its_canonical_seat_is_not_misfiled(tmp_path):
    """The negative case the whole check rests on. If a correctly-filed page reported as misfiled,
    the count would be the size of the namespace and the real defects unreadable inside it."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/z/zebra.md", "type: thing\ntitle: Z\n")
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]


def test_an_unreadable_partition_config_is_treated_as_flat_not_as_a_defect(tmp_path, monkeypatch):
    """The strategy lookup reads a private helper in a sibling module. If that ever raises, the
    audit must fall back to the conservative reading — flat namespaces are never seat-checked — and
    keep auditing. Guessing "partitioned" instead would report every page in the namespace as
    misfiled on the strength of an exception."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/kind/zebra.md", "type: thing\ntitle: Z\n")

    def boom(*a, **kw):
        raise RuntimeError("schema unreadable")

    monkeypatch.setattr(m.okf_migrate, "_governing_schema", boom)
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]


def test_a_seat_that_cannot_be_computed_is_skipped_not_reported(tmp_path, monkeypatch):
    """No canonical seat means no basis for saying a page is off it. Reporting the page anyway would
    name a defect the audit cannot substantiate — and the operator's fix would be a guess."""
    m = _shape_mod()
    v = _partition_vault(tmp_path)
    _mkpage(v, "things/kind/zebra.md", "type: thing\ntitle: Z\n")

    def boom(*a, **kw):
        raise RuntimeError("cannot compute seat")

    monkeypatch.setattr(m.okf_migrate, "canonical_key", boom)
    r = m.audit(v)
    assert r["partition_misfiled_count"] == 0, r["partition_misfiled"]


def test_every_example_list_is_capped_while_the_counts_stay_complete(tmp_path):
    """Examples exist to make a count actionable, not to reproduce the corpus. The COUNT is the
    measurement and stays exact; the example list is a bounded sample. Letting either drift — an
    uncapped list on a vault with thousands of offenders, or a count that stops at the cap — turns
    the dashboard into something an operator cannot act on or cannot trust."""
    m = _shape_mod()
    cap = m.MAX_EXAMPLES
    n = cap + 2
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n  assessment: {required: [type]}\n"
        "  source: {required: [type]}\n"
        "field_shapes:\n  confidence:\n    by_type:\n      assessment: number\n", encoding="utf-8")
    _mkpage(tmp_path, "sources/carrier.md",
            "type: source\nid: s:c\npublisher: Malpedia\nretrieved_via: SOMEHUB\n")
    for i in range(n):
        _mkpage(tmp_path, f"entities/a/bad-key-{i}.md",
                f"type: actor\nid: e:k{i}\nname: K{i}\n'not a field key!': x\n")
        _mkpage(tmp_path, f"entities/a/stale-{i}.md",
                f"type: actor\nid: e:s{i}\nname: S{i}\nreliability: C\n"
                'auto_verified_basis: "2 A-grade sources: MITRE ATT&CK, Microsoft"\n')
        _mkpage(tmp_path, f"assessments/a/shape-{i}.md",
                f"type: assessment\nid: a:s{i}\nconfidence: moderate\n")
        _mkpage(tmp_path, f"entities/a/carrier-{i}.md",
                f"type: actor\nid: e:c{i}\nname: C{i}\n",
                "Identified in the SOMEHUB dataset.\n")

    bi = m.audit(tmp_path)["body_integrity"]
    for count_key, example_key in (
            ("malformed_field_key_pages", "malformed_field_key_examples"),
            ("stale_reliability_pages", "stale_reliability_examples"),
            ("typed_shape_violations", "typed_shape_examples"),
            ("aggregator_as_source_pages", "aggregator_as_source_examples")):
        assert bi[count_key] == n, f"{count_key} must count every offender, got {bi[count_key]}"
        assert len(bi[example_key]) == cap, f"{example_key} must stop at {cap}"


def test_an_unrecognised_declared_shape_does_not_hide_the_rules_beside_it(tmp_path):
    """A shape name the audit has no checker for cannot be enforced — but it must be skipped
    individually, not treated as the end of the field's rules. Silently dropping the rest would make
    a real, checkable rule undetectable because an unrelated typo sat above it."""
    m = _shape_mod()
    (tmp_path / "schema.yaml").write_text(
        "types:\n  assessment: {required: [type]}\n  source: {required: [type]}\n"
        "field_shapes:\n  confidence:\n    by_type:\n"
        "      source: nonsense-shape\n      assessment: number\n", encoding="utf-8")
    _mkpage(tmp_path, "assessments/a/bad.md", "type: assessment\nid: a:b\nconfidence: moderate\n")
    _mkpage(tmp_path, "sources/s.md", "type: source\nid: s:1\nconfidence: whatever\n")
    bi = m.audit(tmp_path)["body_integrity"]
    assert bi["typed_shape_rules"] == 1, "only the checkable rule is counted as a rule"
    assert bi["typed_shape_violations"] == 1, bi
    assert any("bad.md" in e for e in bi["typed_shape_examples"]), bi


def test_a_raw_capture_with_no_url_is_stepped_over_not_stopped_on(tmp_path):
    """The unpromoted-capture scan keys on url. A capture without one contributes nothing and must
    not end the scan — the captures after it are the ones the count is for."""
    m = _shape_mod()
    (tmp_path / "schema.yaml").write_text("types:\n  source: {required: [type]}\n", encoding="utf-8")
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    (raw / "a-no-url.md").write_text("---\ntype: raw\ntitle: No URL\n---\n\nBody.\n",
                                     encoding="utf-8")
    (raw / "b-has-url.md").write_text(
        "---\ntype: raw\nurl: https://example.com/unpromoted\n---\n\nBody.\n", encoding="utf-8")
    r = m.raw_capture_health(tmp_path)
    assert r["unpromoted_urls"] == 1, r
    assert any("example.com/unpromoted" in str(u) for u in r["unpromoted_examples"]), r


# --- okengine#589: a source's identity stamped on the thing it reports on ------------------------

SCHEMA_TYPED = """
types:
  source: {required: [type]}
  actor: {required: [type]}
type_aliases:
  threat-actor: actor
  article: source
"""


def test_a_publisher_promoted_to_an_actor_is_flagged(tmp_path):
    """`publisher` + `source_kind` describe a SOURCE RECORD. On a page that is not a source they are
    the report's identity stamped onto the thing the report is ABOUT.

    Both live shapes are here: a company minted as an actor by its own byline, and a genuine tracked
    actor wearing a news outlet as its publisher. The second is the worse one -- the actor is real,
    so nothing about the page looks wrong until you read the field.
    """
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md",
          "type: actor\ntitle: Vendorco\npublisher: Vendorco\nsource_kind: report")
    _page(vault / "wiki" / "entities" / "b.md",
          "type: actor\ntitle: Real Actor\npublisher: A News Outlet\nsource_kind: report")
    state = mod.audit(vault)
    integrity = state["body_integrity"]
    assert integrity["source_descriptor_pages"] == 2
    assert any("entities/a.md" in e for e in integrity["source_descriptor_examples"])
    assert "| Source descriptor on a non-source page | 2 | 2 |" in mod.render(state, "2026-08-14")


def test_a_real_source_record_carrying_its_own_descriptors_is_not_flagged(tmp_path):
    """The fields are CORRECT on a source. Flagging them there would report every source page in the
    corpus, and a detector that fires on the normal case gets switched off.

    The alias case is the trap: a pack that spells its source type `article` must not have every one
    of its source records reported, so the type alias has to be resolved before the comparison.
    """
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "sources" / "a.md",
          "type: source\ntitle: A report\npublisher: A News Outlet\nsource_kind: report")
    _page(vault / "wiki" / "sources" / "b.md",
          "type: article\ntitle: B report\npublisher: A News Outlet\nsource_kind: report")
    assert mod.audit(vault)["body_integrity"]["source_descriptor_pages"] == 0


def test_publisher_alone_is_not_the_signal(tmp_path):
    """A vendor entity legitimately has a publisher -- 97 pages on the live vault carry `publisher`
    with no `source_kind`. Requiring BOTH is what keeps the signal (10 pages) out of that noise."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: X\npublisher: Some Vendor")
    _page(vault / "wiki" / "entities" / "b.md", "type: actor\ntitle: Y\nsource_kind: report")
    assert mod.audit(vault)["body_integrity"]["source_descriptor_pages"] == 0


def test_a_tombstoned_page_is_not_reported(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md",
          "type: actor\ntitle: X\nstatus: tombstoned\npublisher: X\nsource_kind: report\n"
          "aliases: [AI]")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["source_descriptor_pages"] == 0
    assert integrity["overbroad_alias_pages"] == 0


# --- okengine#589: an alias is a match term ------------------------------------------------------

def test_short_generic_aliases_are_flagged_but_catalogue_ids_are_not(tmp_path):
    """The calibration IS the detector. `AI` matched 10.6% of source pages on the live vault; a
    floor of 6 (what one consumer uses) would report 179 of 1,342 actor pages, nearly all of them
    legitimate -- `ALPHV`, `ZINC`, `Qilin`. A detector reporting 179 pages gets switched off.

    So structured catalogue IDs are exempt by SHAPE, not by a list of vendor prefixes: the engine
    ships no numbering scheme, only "a prefix and a number".
    """
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: X\naliases: [AI, LLM]")
    _page(vault / "wiki" / "entities" / "b.md",
          "type: actor\ntitle: Y\naliases: [APT31, TA412, G0035, BE2, ATK7]")
    _page(vault / "wiki" / "entities" / "c.md", "type: actor\ntitle: Z\naliases: [ALPHV, Qilin]")
    state = mod.audit(vault)
    integrity = state["body_integrity"]
    assert integrity["overbroad_alias_pages"] == 1, "only the AI/LLM page"
    assert integrity["overbroad_alias_terms"] == ["AI", "LLM"]
    # The terms must reach the dashboard: a count alone tells the operator a problem exists but not
    # which token to remove, and the whole point is that one term is one decision.
    rendered = mod.render(state, "2026-08-14")
    assert "| Over-broad alias (< 4 chars) | 1 | 2 distinct term(s) — `AI`, `LLM` |" in rendered


def test_a_short_non_ascii_alias_is_not_short(tmp_path):
    """Character count proxies for specificity only within one script: three CJK characters are a
    full name, not an acronym. `狼毒草` is on the live vault and must not be reported."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: X\naliases: [狼毒草]")
    assert mod.audit(vault)["body_integrity"]["overbroad_alias_pages"] == 0


def test_distinct_terms_are_counted_separately_from_pages(tmp_path):
    """One bad alias on nine pages is ONE decision, not nine. Reporting only a page count would make
    a single term look like a widespread problem, and vice versa."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    for name in "abc":
        _page(vault / "wiki" / "entities" / f"{name}.md", f"type: actor\ntitle: {name}\naliases: [AI]")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["overbroad_alias_pages"] == 3
    assert integrity["overbroad_alias_terms"] == ["AI"], "deduplicated across pages"


def test_a_scalar_aliases_field_is_read_as_one_alias_not_as_characters(tmp_path):
    """`aliases` is declared `list`, but a scalar lands on pages written around the write path.

    Iterating a scalar string yields its CHARACTERS -- `aliases: raw-alias` becomes seven one-letter
    aliases, which is exactly what a probe written for this issue reported before it was caught. The
    opposite reading is just as bad: skipping non-lists (what the fragmentation index did) makes the
    malformed page invisible to every alias check, exempting precisely the pages most likely broken.
    """
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: X\naliases: raw-alias")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["scalar_alias_pages"] == 1
    assert integrity["overbroad_alias_pages"] == 0, "'raw-alias' is 9 characters, not 9 aliases"
    assert integrity["overbroad_alias_terms"] == []


def test_a_scalar_alias_still_enters_the_fragmentation_index(tmp_path):
    """Two entities sharing an identity token are the fragmentation signal. It must not be defeated
    by one of them spelling `aliases` as a scalar."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: Alpha\naliases: Shared Token")
    _page(vault / "wiki" / "entities" / "b.md", "type: actor\ntitle: Beta\naliases: [Shared Token]")
    clusters = mod.audit(vault)["fragmentation"]
    assert any({"entities/a.md", "entities/b.md"} <= set(c["members"]) for c in clusters), (
        "a scalar-aliased page must still cluster with the page it shares a token with")


def test_the_new_example_lists_cap_while_their_counts_stay_complete(tmp_path):
    """Examples are a sample; the counts are the measurement. A cap that also truncated the count
    would under-report the defect and make a spreading problem look contained -- the dashboard is
    read for the number, not the three paths beside it."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_TYPED)
    for i in range(5):
        _page(vault / "wiki" / "entities" / f"d{i}.md",
              f"type: actor\ntitle: D{i}\npublisher: Outlet\nsource_kind: report")
        _page(vault / "wiki" / "entities" / f"a{i}.md", f"type: actor\ntitle: A{i}\naliases: [AI]")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["source_descriptor_pages"] == 5
    assert len(integrity["source_descriptor_examples"]) == mod.MAX_EXAMPLES
    assert integrity["overbroad_alias_pages"] == 5
    assert len(integrity["overbroad_alias_examples"]) == mod.MAX_EXAMPLES
    assert integrity["overbroad_alias_terms"] == ["AI"], "the term list is deduplicated, not capped"


# --- okengine#589 follow-up: a detector that survives the repair for #589 ------------------------

SCHEMA_PUB = """
types:
  source: {required: [type]}
  actor: {required: [type]}
  publisher: {required: [type]}
type_aliases:
  article: source
"""


def test_an_actor_the_corpus_publishes_under_is_flagged(tmp_path):
    """The repair for #589 removes the SIGNATURE, not the defect.

    `entities/a/anthropic` was minted `type: actor` with `publisher: Anthropic` + `source_kind:
    report` — a company as a threat actor. Stripping those two fields (the correct repair for the
    fields) left the page an actor and dropped the descriptor detector to 0: the defect became
    invisible to the check built for it.

    This asks a question the repair cannot erase. A name the corpus PUBLISHES under is an outlet or
    a vendor; an adversary does not file its own advisories.
    """
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    # The post-repair shape exactly: no publisher, no source_kind — just an actor page.
    _page(vault / "wiki" / "entities" / "vendorco.md", "type: actor\ntitle: Vendorco")
    _page(vault / "wiki" / "sources" / "s1.md",
          "type: source\ntitle: A report\npublisher: Vendorco\nsource_kind: report")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["source_descriptor_pages"] == 0, "the #589 signature is gone — that is the point"
    assert integrity["actor_is_a_publisher_pages"] == 1
    assert any("vendorco" in e for e in integrity["actor_is_a_publisher_examples"])


def test_a_publisher_ENTITY_page_counts_as_publishing_too(tmp_path):
    """Packs model this either way: a `type: publisher` entity page, or the name used as
    `publisher:` on source records. Catching only one spelling would miss half the corpus."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: Intruder Group\nname: Intruder")
    _page(vault / "wiki" / "entities" / "b.md", "type: publisher\ntitle: Intruder")
    assert mod.audit(vault)["body_integrity"]["actor_is_a_publisher_pages"] == 1


def test_an_ordinary_actor_is_not_flagged(tmp_path):
    """The corpus is full of actors and full of publishers; only the COLLISION is the signal. If
    this ever fires broadly the join key is wrong and the row becomes noise."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: Fancy Bear")
    _page(vault / "wiki" / "entities" / "b.md", "type: publisher\ntitle: Some Vendor")
    _page(vault / "wiki" / "sources" / "s.md",
          "type: source\ntitle: R\npublisher: Some Vendor\nsource_kind: report")
    assert mod.audit(vault)["body_integrity"]["actor_is_a_publisher_pages"] == 0


def test_actor_evidence_contradiction_is_a_repair_finding(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    for index in range(mod.MAX_EXAMPLES + 2):
        title = f"TerminalFix{index}"
        _page(
            vault / "wiki" / "entities" / "t" / f"terminalfix-{index}.md",
            f"type: actor\ntitle: {title}\nactor_type: cybercriminal\nstatus: active",
            f"A new ClickFix variant, dubbed {title}, tricks users into running commands.",
        )
    _page(
        vault / "wiki" / "entities" / "s" / "shinyhunters.md",
        "type: actor\ntitle: ShinyHunters\nactor_type: cybercriminal",
        "ShinyHunters is a cybercriminal group associated with data theft.",
    )
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["actor_class_contradiction_pages"] == mod.MAX_EXAMPLES + 2
    assert len(integrity["actor_class_contradiction_examples"]) == mod.MAX_EXAMPLES
    assert "terminalfix" in integrity["actor_class_contradiction_examples"][0]
    rendered = mod.render(mod.audit(vault), "2026-09-01")
    total = mod.MAX_EXAMPLES + 2
    assert f"| Actor page whose evidence defines a non-actor | {total} | {total} |" in rendered


def test_a_tombstoned_actor_is_not_flagged(tmp_path):
    """A tombstone is the RESOLUTION of this defect — counting it would mean fixing it never
    clears the signal."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md",
          "type: actor\ntitle: Vendorco\nstatus: tombstoned")
    _page(vault / "wiki" / "sources" / "s.md",
          "type: source\ntitle: R\npublisher: Vendorco\nsource_kind: report")
    assert mod.audit(vault)["body_integrity"]["actor_is_a_publisher_pages"] == 0


def test_a_short_identity_does_not_join(tmp_path):
    """Same floor as the fragmentation index. Joining on a 3-character token would collide unrelated
    entities and report an actor as a publisher because both happen to be called `ACT`."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: ACT")
    _page(vault / "wiki" / "entities" / "b.md", "type: publisher\ntitle: ACT")
    assert mod.audit(vault)["body_integrity"]["actor_is_a_publisher_pages"] == 0


def test_the_finding_is_order_independent(tmp_path):
    """The join resolves after the walk. Deciding during it would make the result depend on whether
    the publisher page happened to be read before the actor page — a detector that reports different
    numbers on the same corpus is worse than none."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    # 'z' sorts after 'a', so the publisher side is seen LAST.
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: Vendorco")
    _page(vault / "wiki" / "entities" / "z.md", "type: publisher\ntitle: Vendorco")
    assert mod.audit(vault)["body_integrity"]["actor_is_a_publisher_pages"] == 1
    rendered = mod.render(mod.audit(vault), "2026-08-15")
    assert "| Actor whose identity is also a publisher | 1 | 1 |" in rendered


def test_a_short_publisher_value_does_not_enter_the_join(tmp_path):
    """Symmetry with the actor side. A source published by `IBM` must not make every entity called
    `IBM` a finding — and more to the point, a 3-character publisher joins far too much."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: ABC")
    _page(vault / "wiki" / "sources" / "s.md",
          "type: source\ntitle: R\npublisher: ABC\nsource_kind: report")
    assert mod.audit(vault)["body_integrity"]["actor_is_a_publisher_pages"] == 0


def test_the_actor_publisher_examples_cap_while_the_count_stays_complete(tmp_path):
    """Same contract as every other example list here: the count is the measurement, the examples
    are a sample. A cap that truncated the count would make a spreading problem look contained."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    for i in range(5):
        _page(vault / "wiki" / "entities" / f"a{i}.md", f"type: actor\ntitle: Vendorco{i}")
        _page(vault / "wiki" / "sources" / f"s{i}.md",
              f"type: source\ntitle: R{i}\npublisher: Vendorco{i}\nsource_kind: report")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["actor_is_a_publisher_pages"] == 5
    assert len(integrity["actor_is_a_publisher_examples"]) == mod.MAX_EXAMPLES


# --- okengine#591: a match count is not evidence -------------------------------------------------

def test_an_identity_matched_far_more_than_it_is_cited_for_is_flagged(tmp_path):
    """`recent_news` counts articles whose TEXT contains the name. Nobody checked they are about it.

    A common noun therefore scores highest precisely because it means least: on the live corpus
    `Attacker` carried 18 news against 1 cited source, while `Scattered Spider` -- a heavily
    reported real actor -- carried 9 against 35. Every genuine actor measured below ratio 1.0; every
    generic noun at or above 2.0.

    This would be a curiosity if nothing consumed it. Both the cockpit actor panels AND the
    `actor-assessment-priority` work queue rank on it, so the number steers what the analysts and
    the pipeline look at first.
    """
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "junk.md",
          "type: actor\ntitle: Attacker\nrecent_news: 18\nsources: [sources/a]")
    _page(vault / "wiki" / "entities" / "real.md",
          "type: actor\ntitle: Scattered Spider\nrecent_news: 9\n"
          "sources: [sources/a, sources/b, sources/c, sources/d, sources/e]")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["overmatching_identity_pages"] == 1
    assert any("junk.md" in e and "18 news / 1 cited" in e
               for e in integrity["overmatching_identity_examples"])


def test_a_well_cited_entity_is_never_flagged_however_much_news_it_has(tmp_path):
    """The real actors are the ones with the MOST reporting. If heavy coverage alone tripped this,
    the detector would report exactly the pages that are working."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md",
          "type: actor\ntitle: Busy Actor\nrecent_news: 40\nsources: ["
          + ", ".join(f"sources/s{i}" for i in range(20)) + "]")
    assert mod.audit(vault)["body_integrity"]["overmatching_identity_pages"] == 0


def test_a_thin_new_page_is_not_over_matching(tmp_path):
    """A page with 1 news and no sources is THIN, not over-matching. Reporting it would bury the
    signal under every freshly-created entity."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md", "type: actor\ntitle: Brand New\nrecent_news: 1")
    assert mod.audit(vault)["body_integrity"]["overmatching_identity_pages"] == 0


def test_a_non_numeric_news_count_is_not_evidence_of_anything(tmp_path):
    """`recent_news` has been hand-set to a LIST before (the field_shapes incident). A parser that
    crashed would take the whole audit down; one that coerced would invent a ratio."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md",
          "type: actor\ntitle: Odd\nrecent_news: [a, b, c]\nsources: [sources/a]")
    assert mod.audit(vault)["body_integrity"]["overmatching_identity_pages"] == 0


def test_it_is_not_limited_to_actors(tmp_path):
    """The worst offenders on the live corpus were `actor-assessment-priority` records, not actor
    pages -- the work queue inherits the subject's count and so schedules the junk first. Scoping
    this to `type: actor` would have missed the consumer that matters most."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "hypotheses" / "p1.md",
          "type: actor-assessment-priority\ntitle: Threat Actor — operating model\n"
          "recent_news: 58\nsources: [sources/a]")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["overmatching_identity_pages"] == 1


def test_a_tombstoned_page_is_not_over_matching(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    _page(vault / "wiki" / "entities" / "a.md",
          "type: actor\ntitle: Attacker\nstatus: tombstoned\nrecent_news: 18\nsources: [sources/a]")
    assert mod.audit(vault)["body_integrity"]["overmatching_identity_pages"] == 0


def test_the_overmatch_examples_cap_while_the_count_stays_complete(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_PUB)
    for i in range(5):
        _page(vault / "wiki" / "entities" / f"a{i}.md",
              f"type: actor\ntitle: Noun{i}\nrecent_news: 18\nsources: [sources/a]")
    integrity = mod.audit(vault)["body_integrity"]
    assert integrity["overmatching_identity_pages"] == 5
    assert len(integrity["overmatching_identity_examples"]) == mod.MAX_EXAMPLES
    rendered = mod.render(mod.audit(vault), "2026-08-15")
    assert "| Identity matched far more than it is cited for | 5 | 5 |" in rendered


# ---------------------------------------------------------------------------
# One subject, two spellings (#591). Every partition check compares slugs for
# EQUALITY, so `agent-tesla` and `agenttesla` both sit at their own correct seat
# and no existing detector can see the pair. The live corpus held 47 of them.
# ---------------------------------------------------------------------------

def test_two_spellings_of_one_name_are_reported_as_one_pair(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "a" / "agent-tesla.md", "type: entity\ntitle: Agent Tesla")
    _page(vault / "wiki" / "entities" / "a" / "g" / "agenttesla.md", "type: entity\ntitle: AgentTesla")
    state = mod.audit(vault)
    assert state["near_duplicate_slug_count"] == 1
    # shallowest seat first, so the report hints at which copy is likely the older one
    assert state["near_duplicate_slugs"] == [
        ["entities/a/agent-tesla", "entities/a/g/agenttesla"],
    ]


def test_underscores_case_and_spacing_are_all_the_same_name(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    for slug in ("winnti-group", "winnti_group", "Winnti Group"):
        _page(vault / "wiki" / "entities" / f"{slug}.md", "type: entity")
    state = mod.audit(vault)
    assert state["near_duplicate_slug_count"] == 1
    assert len(state["near_duplicate_slugs"][0]) == 3


def test_a_distinct_name_is_never_paired(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "agent-tesla.md", "type: entity")
    _page(vault / "wiki" / "entities" / "agent-smith.md", "type: entity")
    assert mod.audit(vault)["near_duplicate_slug_count"] == 0


def test_the_same_spelling_in_two_namespaces_is_not_a_duplicate(tmp_path):
    """`entities/apt1` and `concepts/apt1` are different subjects, not one written twice."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "apt-1.md", "type: entity")
    _page(vault / "wiki" / "concepts" / "apt1.md", "type: concept")
    assert mod.audit(vault)["near_duplicate_slug_count"] == 0


def test_separators_are_the_meaning_in_a_numeric_identifier(tmp_path):
    """`110-37-3-251` and `110-37-32-51` are two addresses, not one name written twice."""
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "indicators" / "110-37-3-251.md", "type: indicator")
    _page(vault / "wiki" / "indicators" / "110-37-32-51.md", "type: indicator")
    assert mod.audit(vault)["near_duplicate_slug_count"] == 0
    assert mod._slug_identity("110-37-3-251") == ""
    assert mod._slug_identity("cve-2026-11405") == "cve202611405"


def test_a_slug_with_no_alphanumerics_at_all_is_ignored(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "---.md", "type: entity")
    _page(vault / "wiki" / "entities" / "===.md", "type: entity")
    assert mod._slug_identity("---") == ""
    assert mod.audit(vault)["near_duplicate_slug_count"] == 0


def test_the_pair_examples_cap_while_the_count_stays_complete(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    for i in range(mod.MAX_EXAMPLES + 2):
        _page(vault / "wiki" / "entities" / f"name-{i}-x.md", "type: entity")
        _page(vault / "wiki" / "entities" / f"name{i}x.md", "type: entity")
    state = mod.audit(vault)
    assert state["near_duplicate_slug_count"] == mod.MAX_EXAMPLES + 2
    assert len(state["near_duplicate_slugs"]) == mod.MAX_EXAMPLES


def test_the_report_states_the_count_and_the_pairs(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "inc-ransom.md", "type: entity")
    _page(vault / "wiki" / "entities" / "incransom.md", "type: entity")
    rendered = mod.render(mod.audit(vault), "2026-08-15")
    assert "## One subject, two spellings" in rendered
    assert "**1** name(s) exist at more than one spelling" in rendered
    assert "`entities/inc-ransom` ↔ `entities/incransom`" in rendered


def test_a_corpus_with_no_near_duplicates_says_zero(tmp_path):
    mod = _load()
    vault = _vault(tmp_path, SCHEMA_WITH_ENUMS)
    _page(vault / "wiki" / "entities" / "apt1.md", "type: entity")
    rendered = mod.render(mod.audit(vault), "2026-08-15")
    assert "**0** name(s) exist at more than one spelling" in rendered
