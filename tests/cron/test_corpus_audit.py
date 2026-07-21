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
