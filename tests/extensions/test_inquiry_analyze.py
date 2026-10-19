"""okengine.inquiry:inquiry-analyze regressions (okengine#854).

The lane turns an inquiry's standing questions into assessments. The defects these reject are the
ones that make it silently useless or silently wrong: a question that is never selected, a rerun
that duplicates the page instead of updating it, a reassessment that never fires, and a citation
the model invented being accepted by the write path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "extensions/okengine.inquiry"


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _load("okengine_inquiry_analyze_lib", EXT / "inquiry_lib.py")
sel = _load("okengine_inquiry_select", EXT / "select_inquiry_questions.py")

PAGE = """---
type: inquiry
question: Does the thing happen?
status: open
opened: 2026-09-01
connector: test.search
terms: [alpha]
{extra}---

Body.
"""


# ── standing questions ──────────────────────────────────────────────────────

def test_an_inquiry_without_questions_answers_its_headline_question():
    inq = lib.Inquiry(slug="s", path=Path("x"), question="Q?", status="open", terms=[])
    assert [(q.key, q.text) for q in inq.standing_questions] == [("main", "Q?")]
    assert inq.assessment_slug(inq.standing_questions[0]) == "s-main"


def test_declared_questions_replace_the_headline_and_derive_keys():
    questions, errors = lib.normalize_questions(
        ["Do safety frameworks bind in practice?", {"key": "testing", "question": "Is it?"}])
    assert not errors
    assert [q.key for q in questions] == ["do-safety-frameworks-bind-in-practice", "testing"]


def test_a_derived_key_is_capped_without_a_trailing_hyphen():
    key = lib.question_key("word " * 30)
    assert len(key) <= lib.MAX_KEY and not key.endswith("-")


def test_absent_questions_are_not_an_error():
    assert lib.normalize_questions(None) == ([], [])


def test_a_scalar_question_is_accepted_as_one():
    questions, errors = lib.normalize_questions("Only one?")
    assert not errors and [q.text for q in questions] == ["Only one?"]


@pytest.mark.parametrize("raw,fragment", [
    ([{"key": "k"}], "`question` is required"),
    ([42], "must be a string or a mapping"),
    ([{"question": "q", "key": "Not Kebab"}], "lowercase kebab-case"),
    ([{"question": "q", "key": "x" * 41}], "lowercase kebab-case"),
    (["Same question", "same question!"], "duplicate question key"),
])
def test_malformed_questions_are_rejected(raw, fragment):
    """NEGATIVE: two questions on one key would be answered by one page, and one of them
    silently never would."""
    _, errors = lib.normalize_questions(raw)
    assert errors and fragment in errors[0]


def test_question_errors_surface_through_the_page_loader(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "inquiry").mkdir(parents=True)
    (wiki / "inquiry/does-it-happen.md").write_text(
        PAGE.format(extra="questions: [{key: BAD, question: q}]\n"), encoding="utf-8")
    inquiries, errors = lib.load_inquiries(wiki)
    assert any("questions[0]" in e for e in errors)
    assert inquiries[0].questions == []


def test_the_schema_fragment_declares_questions_as_an_optional_list():
    """The loader and the write path must agree, or a valid page is rejected at one boundary."""
    frag = yaml.safe_load((EXT / "schema/inquiry.schema.yaml").read_text())
    assert "questions" in frag["owns"]["types"]["inquiry"]["optional"]
    assert frag["field_shapes"]["questions"] == "list"
    assert set(frag["field_items"]["questions"]) == {"question", "key"}


# ── selector fixtures ───────────────────────────────────────────────────────

def _vault(tmp_path, monkeypatch, *, sources=6, extra="", created="2026-09-10"):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    (wiki / "inquiry").mkdir(parents=True)
    (wiki / "inquiry/does-it-happen.md").write_text(PAGE.format(extra=extra), encoding="utf-8")
    ledger = {"records": {}}
    for i in range(sources):
        rd = f"raw/connectors/t/records/r{i}"
        page = wiki / f"sources/2026/09/s{i}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\ntype: source\ntitle: Source {i}\npublisher: Pub\n"
                        f"published: 2026-09-0{i % 9 + 1}\nreliability: B\ncredibility: 2\n"
                        f"created: '{created}T00:00:00Z'\nraw: {rd}/f.json\n---\n"
                        f"Body of <b>source</b> {i}.\n", encoding="utf-8")
        capture = vault / f"raw/captured/r{i}.md"
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text("---\nsource_channel: feed\ncapture_object: objects/test.html\n"
                           "capture_revision: revisions/test.json\ncontent_hash: test\n---\n"
                           f"## Captured content\nBody of <b>source</b> {i}.\n")
        text = page.read_text().replace(f"raw: {rd}/f.json",
                                       f"raw: [{rd}/f.json, raw/captured/r{i}.md]")
        page.write_text(text)
        lib.attribute(ledger, rd, connector="c", native_id=f"r{i}",
                      term="does-it-happen::alpha", today="2026-09-10")
    # The deployment's composed schema defines the type the lane writes (okengine.assessments on).
    (vault / ".okengine").mkdir(parents=True, exist_ok=True)
    (vault / ".okengine/composed-schema.yaml").write_text(
        "types:\n  assessment: {required: [type]}\n  inquiry: {required: [type]}\n")
    monkeypatch.setenv("HERMES_HOME", str(vault / ".hermes-data"))
    monkeypatch.delenv("OKENGINE_SELECTION_MANIFEST", raising=False)
    for target in {lib, sel.lib}:
        monkeypatch.setattr(target, "VAULT", vault)
        monkeypatch.setattr(target, "WIKI", wiki)
    sel.lib.save_ledger(ledger)
    return vault, wiki


def _assessment(wiki, slug, *, last_updated="2026-09-12", shard="d"):
    page = wiki / "assessments" / shard / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"---\ntype: assessment\nclaim: It happens.\nconfidence: medium\n"
                    f"confidence_band: '45-80'\nlast_updated: '{last_updated}T00:00:00Z'\n---\n"
                    "Body.\n", encoding="utf-8")
    return page


def _run(capsys):
    assert sel.main([]) == 0
    out = capsys.readouterr().out
    return out, json.loads(out.strip().splitlines()[-1])


# ── selection ───────────────────────────────────────────────────────────────

def test_an_unassessed_question_with_enough_evidence_wakes_the_lane(tmp_path, monkeypatch, capsys):
    vault, _ = _vault(tmp_path, monkeypatch)
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True}
    assert "`does-it-happen-main`" in out and "MODE: CREATE" in out
    assert "`assessments/does-it-happen-main`" in out
    manifest = json.loads((vault / ".hermes-data/cron-plus/selections/"
                           "okengine.inquiry:inquiry-analyze.json").read_text())
    assert manifest["selected"] == ["does-it-happen-main"]


def test_below_the_evidence_floor_the_lane_stays_asleep(tmp_path, monkeypatch, capsys):
    """NEGATIVE: a first judgment from a handful of articles is extrapolation."""
    _vault(tmp_path, monkeypatch, sources=sel.MIN_SOURCES - 1)
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": False} and "SKIP" in out


def test_an_assessed_question_with_no_new_evidence_is_not_reselected(tmp_path, monkeypatch, capsys):
    """NEGATIVE: without this the lane rewrites the same page every day on the same evidence."""
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    _, gate = _run(capsys)
    assert gate == {"wakeAgent": False}


def test_new_evidence_since_the_assessment_triggers_a_reassessment(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-20")
    page = _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True}
    assert "MODE: UPDATE" in out and "current claim: It happens." in out
    assert f"[[{page.relative_to(wiki).as_posix()[:-3]}]]" in out
    assert out.count("- NEW [[sources/") == 6


def test_the_existing_page_is_found_wherever_the_partition_filed_it(tmp_path, monkeypatch, capsys):
    """The #54 loop: a selector that looked only at a hand-built path would call a resharded page
    missing and have the model create a second copy."""
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-20")
    _assessment(wiki, "does-it-happen-main", shard="d/o")
    out, _ = _run(capsys)
    assert "MODE: UPDATE [[assessments/d/o/does-it-happen-main]]" in out


def test_an_assessment_without_a_date_is_reassessed_on_any_new_evidence(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch)
    page = wiki / "assessments/d/does-it-happen-main.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: assessment\n---\n", encoding="utf-8")
    picked = sel.candidates(wiki, lib.load_inquiries(wiki)[0], lib.load_ledger())
    assert len(picked) == 1 and len(picked[0].new) == 6


def test_unassessed_questions_outrank_reassessments(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-20",
                     extra="questions: [{key: first, question: 'A?'}, {key: second, question: 'B?'}]\n")
    _assessment(wiki, "does-it-happen-first", last_updated="2026-09-12")
    picked = sel.candidates(wiki, lib.load_inquiries(wiki)[0], lib.load_ledger())
    assert [c.slug for c in picked] == ["does-it-happen-second", "does-it-happen-first"]


def test_the_batch_caps_what_one_run_takes(tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch,
           extra="questions: [{key: first, question: 'A?'}, {key: second, question: 'B?'}]\n")
    monkeypatch.setattr(sel, "BATCH", 1)
    out, _ = _run(capsys)
    assert "`does-it-happen-first`" in out and "`does-it-happen-second`" not in out


def test_a_closed_inquiry_is_never_analyzed(tmp_path, monkeypatch, capsys):
    vault, wiki = _vault(tmp_path, monkeypatch)
    page = wiki / "inquiry/does-it-happen.md"
    page.write_text(page.read_text().replace("status: open", "status: closed"))
    _, gate = _run(capsys)
    assert gate == {"wakeAgent": False}


def test_a_non_assessment_page_in_the_namespace_is_ignored(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch)
    for name, body in (("INDEX.md", "---\ntype: assessment\n---\n"),
                       ("_about.md", "---\ntype: assessment\n---\n"),
                       ("does-it-happen-main.md", "---\ntype: dashboard\n---\n")):
        (wiki / "assessments").mkdir(exist_ok=True)
        (wiki / "assessments" / name).write_text(body, encoding="utf-8")
    assert sel.existing_assessments(wiki) == {}


def test_no_assessments_namespace_means_nothing_is_assessed(tmp_path):
    assert sel.existing_assessments(tmp_path / "wiki") == {}


def test_malformed_inquiries_are_warned_about(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch)
    (wiki / "inquiry/Bad_Slug.md").write_text(PAGE.format(extra=""), encoding="utf-8")
    out, _ = _run(capsys)
    assert "WARN  inquiry/Bad_Slug" in out


# ── the digest the model receives ───────────────────────────────────────────

def test_the_digest_strips_collector_markup_and_caps_excerpts(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch)
    long = wiki / "sources/2026/09/s0.md"
    long.write_text(long.read_text().replace("Body of", "x " * 400 + "Body of"))
    out, _ = _run(capsys)
    assert "<b>" not in out and "Body of source 1." in out
    assert "…" in out


def test_the_digest_caps_listed_evidence_but_reports_the_total(tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(sel, "MAX_EVIDENCE", 2)
    out, _ = _run(capsys)
    assert "Evidence (2 of 6 attributed source(s)" in out
    assert out.count("- [[sources/") == 2


def test_source_metadata_that_is_missing_is_omitted_not_printed_as_none(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch)
    for page in (wiki / "sources").rglob("*.md"):
        text = page.read_text()
        for field in ("title: ", "publisher: ", "reliability: ", "credibility: "):
            text = "\n".join(l for l in text.splitlines() if not l.startswith(field)) + "\n"
        page.write_text(text)
    out, _ = _run(capsys)
    assert "None" not in out and "reliability" not in out


def test_the_receipt_template_names_every_selected_key(tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch)
    out, _ = _run(capsys)
    receipt = json.loads(out.split("```okengine-receipt", 1)[1].split("```", 1)[0])
    assert [item["key"] for item in receipt["items"]] == ["does-it-happen-main"]
    assert "insufficient-evidence" in receipt["items"][0]["disposition"]


def test_manifest_path_honors_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    assert sel.manifest_path() == tmp_path / "hh/cron-plus/selections/" \
        "okengine.inquiry:inquiry-analyze.json"


def test_manifest_path_defaults_under_the_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(sel.lib, "VAULT", tmp_path)
    assert sel.manifest_path().parent == tmp_path / ".hermes-data/cron-plus/selections"


# ── the output contract: the write path rejects what the prompt forbids ─────

def _contract():
    manifest = yaml.safe_load((EXT / "extension.yaml").read_text())
    return manifest["operations"]["inquiry-analyze"]["output_contract"]


def _enforce():
    return _load("okengine_output_contract_enforce", ROOT / "okengine-mcp/output_contract_enforce.py")


def test_a_fabricated_source_is_rejected_by_the_contract(tmp_path):
    """NEGATIVE: the prompt says cite only pages you read; the contract is what makes that true."""
    wiki = tmp_path / "wiki"
    (wiki / "inquiry").mkdir(parents=True)
    (wiki / "inquiry/does-it-happen.md").write_text("---\ntype: inquiry\n---\n")
    (wiki / "sources").mkdir()
    (wiki / "sources/real.md").write_text("---\ntype: source\n---\n")
    fm = {"subject_ref": "[[inquiry/does-it-happen]]",
          "sources": ["[[sources/real]]", "[[sources/invented-by-the-model]]"]}
    findings = _enforce().relationship_findings(fm, _contract(), wiki)
    assert [kind for kind, _ in findings] == ["relationship_unresolved"]
    assert "invented-by-the-model" in findings[0][1]


def test_an_assessment_citing_nothing_is_rejected(tmp_path):
    findings = _enforce().relationship_findings(
        {"subject_ref": "[[inquiry/x]]"}, _contract(), tmp_path / "wiki")
    assert ("required_relationship_missing", "required relationship 'sources' is absent") \
        in findings


def test_the_contract_confines_the_lane_to_assessments_and_enforces_receipts():
    contract = _contract()
    assert contract["allowed_namespaces"] == ["assessments"]
    assert contract["allowed_types"] == ["assessment"]
    assert contract["completion"] == "per-selected-item"
    assert {"sources", "subject_ref"} <= set(contract["required_relationships"])
    assert "tombstone" not in contract["operations"]


def test_the_selector_names_the_manifest_env_var_the_runner_sets():
    """The runner verifies the receipt against the manifest this env var names."""
    assert "OKENGINE_SELECTION_MANIFEST" in (EXT / "select_inquiry_questions.py").read_text()


def test_two_inquiries_sharing_evidence_are_both_selected(tmp_path, monkeypatch):
    """One record returned by two inquiries' terms is evidence for both; its date is read once."""
    _, wiki = _vault(tmp_path, monkeypatch)
    (wiki / "inquiry/second-inquiry.md").write_text(
        PAGE.format(extra="").replace("2026-09-01", "2026-09-05"), encoding="utf-8")
    ledger = lib.load_ledger()
    for record_dir in list(ledger["records"]):
        lib.attribute(ledger, record_dir, connector="c", native_id="n",
                      term="second-inquiry::alpha", today="2026-09-10")
    picked = sel.candidates(wiki, lib.load_inquiries(wiki)[0], ledger)
    assert [c.slug for c in picked] == ["does-it-happen-main", "second-inquiry-main"]
    assert picked[0].sources == picked[1].sources


# ── reassessment when CITED evidence moves (the CHE loop, via okengine.reevaluation) ──

def _edges(wiki, page, cites):
    """Write the reevaluation sidecar: `cites` maps cited page -> via list for `page`."""
    edges = {cited: [{"page": page, "status": "active", "via": via}] for cited, via in cites.items()}
    (wiki / ".reevaluation-edges.json").write_text(json.dumps({"edges": edges}))


def _touch(wiki, ref, last_updated):
    page = wiki / f"{ref}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    body = page.read_text() if page.exists() else "---\ntype: source\n---\nx\n"
    page.write_text(body.replace("---\n", f"---\nlast_updated: '{last_updated}'\n", 1))


def test_a_changed_cited_source_triggers_reassessment_without_new_evidence(
        tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")      # nothing NEW
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    _touch(wiki, "sources/2026/09/s1", "2026-09-15T08:00:00Z")
    _edges(wiki, "assessments/d/does-it-happen-main", {"sources/2026/09/s1": ["sources"]})
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True}
    assert "CITED SOURCE CHANGED since this assessment: [[sources/2026/09/s1]]" in out
    assert "new source(s) compiled" not in out


def test_a_source_rewritten_before_the_assessment_is_not_a_change(tmp_path, monkeypatch, capsys):
    """NEGATIVE: the assessment already saw that version."""
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    _touch(wiki, "sources/2026/09/s1", "2026-09-11T08:00:00Z")
    _edges(wiki, "assessments/d/does-it-happen-main", {"sources/2026/09/s1": ["sources"]})
    assert _run(capsys)[1] == {"wakeAgent": False}


def test_a_removed_cited_source_triggers_reassessment(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    _edges(wiki, "assessments/d/does-it-happen-main",
           {"sources/2026/09/retracted": ["body", "sources"]})
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True}
    assert "CITED SOURCE REMOVED: [[sources/2026/09/retracted]]" in out


def test_a_body_link_that_never_resolved_does_not_trigger_forever(tmp_path, monkeypatch, capsys):
    """NEGATIVE: a body link may never have resolved; treating it as removed would reselect the
    question every run. Only `sources`, resolved by the write path at write time, counts."""
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    _edges(wiki, "assessments/d/does-it-happen-main", {"concepts/never-existed": ["body"]})
    assert _run(capsys)[1] == {"wakeAgent": False}


def test_editing_the_inquiry_page_is_not_evidence(tmp_path, monkeypatch, capsys):
    """NEGATIVE: rewording the question must not reassess every answer on the next run."""
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    _touch(wiki, "inquiry/does-it-happen", "2026-09-20T00:00:00Z")
    _edges(wiki, "assessments/d/does-it-happen-main",
           {"inquiry/does-it-happen": ["body"],
            "assessments/d/does-it-happen-main": ["body"]})
    assert _run(capsys)[1] == {"wakeAgent": False}


def test_an_undated_assessment_cannot_be_judged_stale_by_a_changed_source(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    page = wiki / "assessments/d/does-it-happen-main.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: assessment\n---\n", encoding="utf-8")
    _touch(wiki, "sources/2026/09/s1", "2026-09-15T08:00:00Z")
    changed, gone = sel.moved_evidence(wiki, {}, {"sources/2026/09/s1": ["sources"]})
    assert (changed, gone) == ([], [])


def test_moved_evidence_outranks_merely_new_evidence(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-20",
                     extra="questions: [{key: first, question: 'A?'}, {key: second, question: 'B?'}]\n")
    _assessment(wiki, "does-it-happen-first", last_updated="2026-09-12")
    _assessment(wiki, "does-it-happen-second", last_updated="2026-09-25")
    _touch(wiki, "sources/2026/09/s1", "2026-09-26T00:00:00Z")
    deps = {"assessments/d/does-it-happen-second": {"sources/2026/09/s1": ["sources"]}}
    picked = sel.candidates(wiki, lib.load_inquiries(wiki)[0], lib.load_ledger(), deps)
    assert [c.slug for c in picked] == ["does-it-happen-second", "does-it-happen-first"]


def test_without_the_dependency_sidecar_the_digest_says_changes_are_undetectable(
        tmp_path, monkeypatch, capsys):
    """An absent index is `unknown`, never `nothing changed`."""
    _vault(tmp_path, monkeypatch)
    out, _ = _run(capsys)
    assert "cited-source changes are UNDETECTABLE" in out


@pytest.mark.parametrize("body", ["{ not json", "[]", '{"edges": []}'])
def test_an_unreadable_sidecar_is_undetectable_not_empty(tmp_path, body):
    (tmp_path / ".reevaluation-edges.json").write_text(body)
    assert sel.cited_by(tmp_path) is None


def test_malformed_sidecar_rows_are_skipped(tmp_path):
    (tmp_path / ".reevaluation-edges.json").write_text(json.dumps({"edges": {
        "sources/a": [{"page": "assessments/x", "via": ["sources"]}, {"no_page": 1}, "junk"],
        "sources/b": "not-a-list"}}))
    assert sel.cited_by(tmp_path) == {"assessments/x": {"sources/a": ["sources"]}}


def test_the_contract_requires_refutation_criteria():
    """CHE profile: require_refutation_or_resolution_criteria."""
    required = set(_contract()["required_fields"])
    assert {"would_increase_confidence", "would_decrease_confidence"} <= required


# ── exact boundaries and orderings (mutation-driven: each pins a decision a mutant flipped) ──

def _set_created(wiki, index, created):
    page = wiki / f"sources/2026/09/s{index}.md"
    text = page.read_text()
    start = text.index("created: '") + len("created: '")
    page.write_text(text[:start] + created + text[text.index("'", start):])


def test_a_derived_key_is_exactly_forty_characters_at_most():
    assert lib.question_key("a" * 60) == "a" * 40


def test_one_bad_question_does_not_hide_the_ones_after_it():
    """continue-vs-break: every rejection path must keep reading the list."""
    questions, errors = lib.normalize_questions(
        [42, {"key": "k"}, {"question": "q", "key": "BAD"}, "Dup?", "dup", "Survives?"])
    assert len(errors) == 4
    assert [q.text for q in questions] == ["Dup?", "Survives?"]


def test_exactly_the_evidence_floor_is_enough_for_a_first_assessment(tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch, sources=sel.MIN_SOURCES)
    assert _run(capsys)[1] == {"wakeAgent": True}


def test_evidence_compiled_on_the_assessment_date_is_not_new(tmp_path, monkeypatch, capsys):
    """The assessment was written that day; it had the chance to see that evidence."""
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-12")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    assert _run(capsys)[1] == {"wakeAgent": False}


def test_exactly_min_new_sources_trigger_a_reassessment(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-10")
    _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    for i in range(sel.MIN_NEW):
        _set_created(wiki, i, "2026-09-20")
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True}
    assert f"  - {sel.MIN_NEW} new source(s) compiled" in out


def test_a_source_rewritten_at_the_same_instant_is_not_a_change(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch)
    _touch(wiki, "sources/2026/09/s1", "2026-09-12T00:00:00Z")
    fm = {"last_updated": "2026-09-12T00:00:00Z"}
    assert sel.moved_evidence(wiki, fm, {"sources/2026/09/s1": ["sources"]}) == ([], [])


def test_skipped_citations_do_not_hide_later_ones(tmp_path, monkeypatch):
    """The inquiry page and a never-resolved link sort BEFORE the changed source."""
    _, wiki = _vault(tmp_path, monkeypatch)
    _touch(wiki, "sources/2026/09/s1", "2026-09-15T00:00:00Z")
    cites = {"concepts/never-existed": ["body"], "inquiry/does-it-happen": ["body"],
             "assessments/a/other": ["body"], "sources/2026/09/s1": ["sources"]}
    _touch(wiki, "assessments/a/other", "2026-09-15T00:00:00Z")
    changed, gone = sel.moved_evidence(wiki, {"last_updated": "2026-09-12T00:00:00Z"}, cites)
    assert changed == ["assessments/a/other", "sources/2026/09/s1"] and gone == []


def test_a_real_assessment_is_found_after_skipped_index_pages(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch)
    root = wiki / "assessments"
    root.mkdir()
    (root / "INDEX.md").write_text("---\ntype: assessment\n---\n")
    (root / "analysis-page.md").write_text("---\ntype: analysis\n---\n")
    (root / "real.md").write_text("---\ntype: assessment\n---\n")
    assert list(sel.existing_assessments(wiki)) == ["real"]


def test_a_closed_inquiry_does_not_hide_an_open_one_after_it(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch)
    (wiki / "inquiry/a-closed-one.md").write_text(
        PAGE.format(extra="").replace("status: open", "status: closed"), encoding="utf-8")
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True} and "`does-it-happen-main`" in out


def test_a_question_below_the_floor_does_not_hide_a_reassessment_after_it(tmp_path, monkeypatch):
    _, wiki = _vault(tmp_path, monkeypatch, sources=2,
                     extra="questions: [{key: first, question: 'A?'}, {key: second, question: 'B?'}]\n")
    _assessment(wiki, "does-it-happen-second", last_updated="2026-09-12")
    _touch(wiki, "sources/2026/09/s1", "2026-09-15T00:00:00Z")
    deps = {"assessments/d/does-it-happen-second": {"sources/2026/09/s1": ["sources"]}}
    picked = sel.candidates(wiki, lib.load_inquiries(wiki)[0], lib.load_ledger(), deps)
    assert [c.slug for c in picked] == ["does-it-happen-second"]


def test_more_moved_evidence_outranks_more_new_evidence(tmp_path, monkeypatch):
    """Rank arithmetic: two moved citations beat one moved plus many new ones."""
    base = dict(inquiry=lib.Inquiry(slug="s", path=Path("x"), question="q", status="open",
                                    terms=[], opened="2026-09-01"),
                question=lib.Question(key="k", text="q"), sources=[])
    two_moved = sel.Candidate(slug="a", existing=(Path("a"), {}), changed=["x"], gone=["y"],
                              new=[], **base)
    one_moved = sel.Candidate(slug="b", existing=(Path("b"), {}), changed=["x"], gone=[],
                              new=["n1", "n2", "n3", "n4", "n5"], **base)
    three_new = sel.Candidate(slug="c", existing=(Path("c"), {}), new=["n1", "n2", "n3"], **base)
    five_new = sel.Candidate(slug="d", existing=(Path("d"), {}), new=["n"] * 5, **base)
    # three_new is listed BEFORE five_new so a tie (a mutant collapsing the new-count) keeps
    # the wrong order instead of accidentally passing.
    ranked = sorted([three_new, five_new, one_moved, two_moved], key=lambda c: c.rank)
    assert [c.slug for c in ranked] == ["a", "b", "d", "c"]


def test_older_inquiries_rank_first_and_undated_ones_last(tmp_path, monkeypatch):
    q = lib.Question(key="k", text="q")

    def cand(slug, opened):
        inq = lib.Inquiry(slug=slug, path=Path("x"), question="q", status="open", terms=[],
                          opened=opened)
        return sel.Candidate(inquiry=inq, question=q, slug=slug, existing=None, sources=[])
    ranked = sorted([cand("undated", ""), cand("newer", "2026-09-10"),
                     cand("older", "2026-09-01")], key=lambda c: c.rank)
    assert [c.slug for c in ranked] == ["older", "newer", "undated"]


def test_evidence_is_listed_newest_path_first(tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch)
    out, _ = _run(capsys)
    assert out.index("[[sources/2026/09/s5]]") < out.index("[[sources/2026/09/s0]]")


@pytest.mark.parametrize("length,ellipsis", [(10, False), (400, False), (401, True)])
def test_excerpts_are_cut_at_exactly_400_characters(length, ellipsis):
    excerpt = sel._excerpt("x" * length)
    assert len(excerpt.rstrip("…")) == min(length, 400)
    assert excerpt.endswith("…") is ellipsis


def test_the_digest_numbers_items_and_names_the_question_when_untitled(
        tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch)
    out, _ = _run(capsys)
    assert "## 1. `does-it-happen-main`" in out
    assert "— Does the thing happen?" in out
    assert "open inquiries: 1 ·" in out
    assert "(Pub · 2026-09-0" in out


def test_an_update_shows_the_exact_date_and_a_truncated_claim(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch, created="2026-09-20")
    page = _assessment(wiki, "does-it-happen-main", last_updated="2026-09-12")
    page.write_text(page.read_text().replace("claim: It happens.", "claim: " + "c" * 400))
    out, _ = _run(capsys)
    assert "(last written 2026-09-12). Reassess because:" in out
    assert "current claim: " + "c" * 300 + "\n" in out


def test_an_undated_update_says_so(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch)
    page = wiki / "assessments/d/does-it-happen-main.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: assessment\n---\n", encoding="utf-8")
    out, _ = _run(capsys)
    assert "(last written undated)" in out


def test_a_source_without_metadata_gets_no_empty_parentheses(tmp_path, monkeypatch, capsys):
    _, wiki = _vault(tmp_path, monkeypatch)
    for page in (wiki / "sources").rglob("*.md"):
        keep = [l for l in page.read_text().splitlines()
                if not l.startswith(("publisher:", "published:", "reliability:", "credibility:"))]
        page.write_text("\n".join(keep) + "\n")
    out, _ = _run(capsys)
    assert " ()" not in out


@pytest.mark.parametrize("batch", [0, -1])
def test_a_zero_or_negative_batch_selects_nothing(tmp_path, monkeypatch, capsys, batch):
    _vault(tmp_path, monkeypatch,
           extra="questions: [{key: first, question: 'A?'}, {key: second, question: 'B?'}]\n")
    monkeypatch.setattr(sel, "BATCH", batch)
    assert _run(capsys)[1] == {"wakeAgent": False}


def test_the_receipt_template_declares_api_one(tmp_path, monkeypatch, capsys):
    _vault(tmp_path, monkeypatch)
    out, _ = _run(capsys)
    receipt = json.loads(out.split("```okengine-receipt", 1)[1].split("```", 1)[0])
    assert receipt["api"] == 1


# ── the selector as the scheduler runs it: a fresh process, in both layouts ──
#
# In-process tests import the selector once, into an interpreter whose sys.path other test
# modules already populated, so they cannot see whether the selector finds its OWN helpers. The
# deployed layout differs from the repo: the scheduler stages it at
# /opt/data/scripts/okengine.inquiry/ with selection_manifest.py one level up, and there is no
# scripts/cron beside it. A wrong parents[] index would pass every in-process test and fail on
# every scheduled run.

def _run_fresh(script: Path, vault: Path, tmp_path: Path):
    import os
    import subprocess
    env = {**os.environ, "WIKI_PATH": str(vault), "HERMES_HOME": str(tmp_path / "hh"),
           "PYTHONPATH": ""}
    return subprocess.run([sys.executable, "-I", str(script)],
                          capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)


def test_the_selector_runs_in_the_deployed_staged_layout(tmp_path):
    import shutil
    staged = tmp_path / "opt-data-scripts"
    (staged / "okengine.inquiry").mkdir(parents=True)
    for name in ("select_inquiry_questions.py", "inquiry_lib.py"):
        shutil.copy(EXT / name, staged / "okengine.inquiry" / name)
    for name in ("selection_manifest.py", "source_content.py"):
        shutil.copy(ROOT / "scripts/cron" / name, staged / name)
    (tmp_path / "vault/wiki").mkdir(parents=True)
    proc = _run_fresh(staged / "okengine.inquiry/select_inquiry_questions.py",
                      tmp_path / "vault", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == '{"wakeAgent": false}'


def test_the_selector_runs_from_the_repository_layout(tmp_path):
    (tmp_path / "vault/wiki").mkdir(parents=True)
    proc = _run_fresh(EXT / "select_inquiry_questions.py", tmp_path / "vault", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == '{"wakeAgent": false}'


# ── inquiry and assessments are enabled independently (okengine#854 step 4) ──

def test_without_the_assessment_type_the_lane_stays_asleep_and_says_why(
        tmp_path, monkeypatch, capsys):
    """NEGATIVE: without this the lane wakes daily in every pack that has an inquiry but no
    okengine.assessments, and spends a model run on pages the write path must reject."""
    vault, _ = _vault(tmp_path, monkeypatch)
    (vault / ".okengine/composed-schema.yaml").write_text("types:\n  inquiry: {}\n")
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": False}
    assert "no `assessment` type" in out and "okengine.assessments" in out
    assert not (vault / ".hermes-data/cron-plus/selections").exists(), \
        "no selection manifest may be written for a lane that cannot run"


def test_an_unreadable_schema_is_unverified_not_a_pass(tmp_path, monkeypatch, capsys):
    """UNKNOWN is reported and the lane proceeds: the write-path contract remains the backstop."""
    _vault(tmp_path, monkeypatch)
    import schema_lib

    def boom(*a, **k):
        raise OSError("disk gone")
    monkeypatch.setattr(schema_lib, "merged_schema", boom)
    out, gate = _run(capsys)
    assert "assessment type UNVERIFIED" in out and "OSError: disk gone" in out
    assert gate == {"wakeAgent": True}


def test_the_guard_reads_the_governing_schema(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    assert sel.assessments_available() == (True, "")


def test_the_step_budget_covers_the_evidence_the_digest_can_list():
    """The first live run (2026-09-24) read ~35 pages under a 20-step budget and ran out before it
    could write — receipt `failed`, no page. The budget must cover every source the digest may
    list PLUS the inquiry, the current page, and the write/verify calls, with room to spare."""
    manifest = yaml.safe_load((EXT / "extension.yaml").read_text())
    budget = manifest["operations"]["inquiry-analyze"]["max_iterations"]
    listed = int(manifest["config"]["analyze_max_evidence"]["default"])
    assert budget >= listed + 10, (budget, listed)


def test_the_prompt_tells_the_model_to_write_before_the_budget_runs_out():
    prompt = (EXT / "prompts/analyze.md").read_text()
    assert "bounded number of tool calls" in prompt
    assert "Write the page as soon as the judgment is formed" in prompt


def test_the_analyze_prompt_asks_for_every_absence_qualification_field():
    """okengine#865: the prompt asked for two of the eleven fields the adversarial-evidence review
    requires, so every absence this lane recorded went to human review for missing qualification --
    masking the specific reason a page was held. The prompt and its consumer must not drift again."""
    ae = _load("okengine_adversarial_evidence",
               ROOT / "extensions/okengine.assessments/adversarial_evidence.py")
    prompt = (EXT / "prompts/analyze.md").read_text(encoding="utf-8")
    missing = [f for f in ae.ABSENCE_REQUIRED_FIELDS if f"`{f}`" not in prompt]
    assert not missing, f"analyze.md never asks for: {missing}"
    for value in ("searched-not-found", "substantial", "near-complete", "collection-gap"):
        assert value in prompt, f"analyze.md never names the qualifying value {value!r}"


def test_discovery_hits_do_not_wake_analysis_even_with_forged_state(tmp_path, monkeypatch, capsys):
    vault, wiki = _vault(tmp_path, monkeypatch)
    for page in (wiki / "sources").rglob("*.md"):
        fm, body = sel.lib.split_frontmatter(page)
        raw = fm["raw"][0]
        capture = vault / raw
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text(json.dumps({"payload": {"matched_snippet": body}}))
        fm["raw"] = raw
        fm["content_state"] = "captured"
        page.write_text("---\n" + yaml.safe_dump(fm) + "---\n" + body * 100)
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": False}
    assert "discovery snippets" in out and "collection/hydration gaps" in out


def test_mixed_evidence_reports_collection_gap_without_listing_snippet_as_evidence(tmp_path, monkeypatch, capsys):
    vault, wiki = _vault(tmp_path, monkeypatch, sources=sel.MIN_SOURCES + 1)
    page = wiki / "sources/2026/09/s0.md"
    fm, body = sel.lib.split_frontmatter(page)
    fm["raw"] = fm["raw"][0]
    capture = vault / fm["raw"]
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text('{"payload": {"matched_snippet": "discovery"}}')
    page.write_text("---\n" + yaml.safe_dump(fm) + "---\n" + body)
    out, gate = _run(capsys)
    assert gate == {"wakeAgent": True}
    assert '"discovery": 1' in out
    assert "[[sources/2026/09/s0]]" not in out
    assert "captured content only" in out
    assert "Captured does not mean appraised" in out


def test_failed_captures_below_the_floor_never_wake_analysis(tmp_path, monkeypatch, capsys):
    vault, wiki = _vault(tmp_path, monkeypatch, sources=sel.MIN_SOURCES)
    capture = vault / "raw/captured/r0.md"
    capture.write_text("---\ncapture_dead_letter: dead-letter/403.json\n---\nCapture failed.\n")
    assert _run(capsys)[1] == {"wakeAgent": False}
    picked = sel.candidates(wiki, lib.load_inquiries(wiki)[0], lib.load_ledger())
    assert picked == [], "a failed capture cannot count as the final required article"
