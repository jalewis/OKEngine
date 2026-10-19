"""okengine#980: corpus_audit reports curated-authority attestation with no legitimate producer.

The write path now refuses attestation fields from every MCP caller, so a page carrying them was
written by a direct importer. Three shapes cannot have come from one: trust assertions on a page
that is not a source record (a merge folded a record into an entity), trust assertions on a page a
model lane created, and an ``operator_evidence_refs`` entry naming a page with no attestation.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from okengine.attestation import AttestationAudit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "corpus_audit.py"
SCHEMA = "types:\n  source:\n    required: [type]\n  actor:\n    required: [type]\n"
TRUST = "local_only: true\nexport_policy: deny\nrecord_checksum: sha256:1\nbounded_auto_accept: true"


def _load():
    spec = importlib.util.spec_from_file_location("corpus_audit_980", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def _page(vault: Path, rel: str, fm: str, body: str = "x") -> None:
    path = vault / "wiki" / f"{rel}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(SCHEMA, encoding="utf-8")
    return vault


def _integrity(vault: Path) -> dict:
    return _load().audit(vault)["body_integrity"]


def test_a_clean_importer_corpus_reports_nothing(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "sources/private/r1", f"type: source\n{TRUST}")
    _page(vault, "entities/a/apt-x",
          "type: actor\noperator_evidence_refs: ['[[sources/private/r1]]', wiki/sources/private/r1.md]")
    state = _integrity(vault)
    assert state["misplaced_attestation_pages"] == 0
    assert state["lane_created_attestation_pages"] == 0
    assert state["unattested_operator_ref_pages"] == 0


def test_attestation_folded_into_an_entity_is_reported(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "entities/o/olympic", f"type: malware\n{TRUST}")
    _page(vault, "entities/u/untyped", "local_only: false")
    _page(vault, "entities/w/wiper", "type: tool\nrecord_checksum: sha256:2")   # sorts after source
    state = _integrity(vault)
    assert state["misplaced_attestation_pages"] == 3
    assert state["misplaced_attestation_examples"][0] == "entities/o/olympic (type 'malware')"


def test_attestation_on_a_page_a_model_lane_created_is_reported(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "sources/2026/10/blog", f"type: source\nproducer_lane: raw-backfill\n{TRUST}")
    _page(vault, "sources/2026/10/plain", "type: source\nproducer_lane: raw-backfill")
    state = _integrity(vault)
    assert state["lane_created_attestation_pages"] == 1
    assert state["lane_created_attestation_examples"] == [
        "sources/2026/10/blog (producer_lane raw-backfill)"]


def test_an_operator_reference_to_an_unattested_page_is_reported_once_per_page(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "sources/private/r1", f"type: source\n{TRUST}")
    _page(vault, "sources/2026/07/news", "type: source")
    _page(vault, "sources/2026/07/other", "type: source")
    _page(vault, "entities/r/royal",
          "type: malware\noperator_evidence_refs: [sources/private/r1, sources/2026/07/news, "
          "sources/2026/07/other, sources/missing/page, {path: sources/2026/07/news}]")
    _page(vault, "entities/s/scalar", "type: actor\noperator_evidence_refs: sources/2026/07/news")
    _page(vault, "entities/w/wikilinked",
          "type: actor\noperator_evidence_refs: ['[[sources/2026/07/other|Other]]', '', 7]")
    _page(vault, "entities/e/empty", "type: actor\noperator_evidence_refs: []")
    state = _integrity(vault)
    # a missing target is the dangling row's finding, not this one
    assert state["unattested_operator_ref_pages"] == 3
    assert state["unattested_operator_ref_examples"] == [
        "entities/r/royal → sources/2026/07/news", "entities/s/scalar → sources/2026/07/news",
        "entities/w/wikilinked → sources/2026/07/other"]


def test_the_rows_render_with_counts_and_examples(tmp_path):
    vault = _vault(tmp_path)
    _page(vault, "entities/o/olympic", f"type: malware\n{TRUST}")
    _page(vault, "sources/2026/10/blog", f"type: source\nproducer_lane: page-quality-enrich\n{TRUST}")
    _page(vault, "sources/2026/07/news", "type: source")
    _page(vault, "entities/r/royal", "type: actor\noperator_evidence_refs: [sources/2026/07/news]")
    mod = _load()
    report = mod.render(mod.audit(vault), "2026-10-03")
    rows = {line.split("|")[1].strip(): line for line in report.splitlines()
            if line.startswith("| ") and "attestation" in line}
    assert "| 1 | 1 | `entities/o/olympic (type 'malware')` |" in rows[
        "Curated-authority attestation on a non-source page"]
    assert "`sources/2026/10/blog (producer_lane page-quality-enrich)`" in rows[
        "Curated-authority attestation on a page a model lane created"]
    assert "`entities/r/royal → sources/2026/07/news`" in rows[
        "`operator_evidence_refs` naming a page with no attestation"]


def test_examples_are_capped_while_counts_stay_exact():
    audit = AttestationAudit(1)
    for name in ("a", "b"):
        audit.observe(f"entities/{name}", {"type": "actor", "local_only": True})
    state = audit.result(set())
    assert state["misplaced_attestation_pages"] == 2
    assert state["misplaced_attestation_examples"] == ["entities/a (type 'actor')"]


def test_an_operator_reference_that_names_no_page_by_exact_path_is_dangling(tmp_path):
    """okengine#980 D1: the resolver no longer falls back to an id or URL for this channel, so a
    reference that is not an exact page path attests nothing -- and was the slot a lookup-key
    write onto an attested record could fill. One row per page, however many such refs it has."""
    vault = _vault(tmp_path)
    _page(vault, "sources/private/r1", f"type: source\nauthority_id: G9999\n{TRUST}")
    _page(vault, "entities/f/fin8",
          "type: actor\noperator_evidence_refs: [G9999, sources/gone/one, sources/private/r1]")
    _page(vault, "entities/a/angry",
          "type: actor\noperator_evidence_refs: [{path: \"sources/x/y MISP galaxy'\"}]")
    _page(vault, "entities/o/ok",
          "type: actor\noperator_evidence_refs: [{source_ref: sources/private/r1}, {note: x}]")
    state = _integrity(vault)
    assert state["dangling_operator_ref_pages"] == 2
    assert state["dangling_operator_ref_examples"] == [
        "entities/a/angry → sources/x/y MISP galaxy'", "entities/f/fin8 → G9999"]
    assert state["unattested_operator_ref_pages"] == 0
    mod = _load()
    row = [line for line in mod.render(mod.audit(vault), "2026-10-03").splitlines()
           if "naming no page by exact path" in line][0]
    assert row.startswith("| `operator_evidence_refs` naming no page by exact path | 2 | 2 |")
