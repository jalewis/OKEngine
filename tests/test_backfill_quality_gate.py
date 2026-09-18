import importlib.util
import json
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backfill_quality_gate", REPO / "scripts" / "backfill_quality_gate.py")
G = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = G
SPEC.loader.exec_module(G)


def test_reviewed_corpus_covers_every_family_and_fixture_class():
    corpus = G.load_corpus(REPO / "quality" / "backfill-gold" / "corpus.yaml")
    assert len(corpus["cases"]) == 28
    assert len({case["family"] for case in corpus["cases"]}) == 7


def test_threshold_gate_passes_only_complete_safe_results():
    corpus = G.load_corpus(REPO / "quality" / "backfill-gold" / "corpus.yaml")
    results = [{
        "case_id": case["id"],
        "action": case["expected"]["action"],
        "expected_action_match": True,
        "schema_valid": True,
        "destructive_violation": False,
        "fabrication_violation": False,
    } for case in corpus["cases"]]
    assert G.score(corpus, results)["verdict"] == "pass"
    results[0]["fabrication_violation"] = True
    report = G.score(corpus, results)
    assert report["verdict"] == "fail"
    assert any("fabrication_violations" in failure for failure in report["failures"])


def test_missing_case_fails_coverage_and_completeness():
    corpus = G.load_corpus(REPO / "quality" / "backfill-gold" / "corpus.yaml")
    report = G.score(corpus, [])
    assert report["verdict"] == "fail" and report["missing"]


def test_load_corpus_rejects_review_ids_and_incomplete_families(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("api: 2\nreview: {status: draft}\ncases: []\n")
    with pytest.raises(ValueError, match="reviewer-approved"):
        G.load_corpus(bad)
    bad.write_text(
        "api: 1\nreview: {status: approved}\ncases:\n"
        "- {id: x, family: f, class: positive}\n"
        "- {id: x, family: f, class: negative}\n"
    )
    with pytest.raises(ValueError, match="present and unique"):
        G.load_corpus(bad)
    bad.write_text(
        "api: 1\nreview: {status: approved}\ncases:\n"
        "- {id: x, family: f, class: positive}\n"
    )
    with pytest.raises(ValueError, match="missing required fixture"):
        G.load_corpus(bad)


def test_score_empty_expected_extra_and_all_threshold_failures():
    corpus = {
        "cases": [],
        "thresholds": {
            "precision": 1, "coverage": 1, "abstention_correctness": 1,
            "schema_validity": 1, "destructive_write_violations": 0,
            "fabrication_violations": 0,
        },
    }
    report = G.score(corpus, [{"case_id": "extra", "action": "write"}])
    assert report["extra"] == ["extra"] and report["verdict"] == "fail"

    corpus["cases"] = [
        {"id": "n", "class": "negative"},
        {"id": "p", "class": "positive"},
    ]
    results = [
        {"case_id": "n", "action": "write", "expected_action_match": False,
         "schema_valid": False, "destructive_violation": True,
         "fabrication_violation": True},
        {"case_id": "p", "action": "abstain", "expected_action_match": False,
         "schema_valid": False},
    ]
    failures = G.score(corpus, results)["failures"]
    assert any("precision" in x for x in failures)
    assert any("abstention_correctness" in x for x in failures)
    assert any("schema_validity" in x for x in failures)
    assert any("destructive" in x for x in failures)


def test_main_pass_fail_and_entrypoint(monkeypatch, tmp_path, capsys):
    corpus = tmp_path / "corpus.yaml"
    corpus.write_text("api: 1\nreview: {status: approved}\ncases:\n"
                      "- {id: x, family: f, class: positive}\n"
                      "- {id: n, family: f, class: negative}\n"
                      "- {id: a, family: f, class: ambiguous}\n"
                      "- {id: z, family: f, class: adversarial}\n"
                      "thresholds: {precision: 0, coverage: 0, abstention_correctness: 0, "
                      "schema_validity: 0, destructive_write_violations: 0, fabrication_violations: 0}\n")
    results = tmp_path / "results.jsonl"
    results.write_text("".join(json.dumps({
        "case_id": case_id, "action": "abstain", "expected_action_match": True,
        "schema_valid": True, "destructive_violation": False,
        "fabrication_violation": False,
    }) + "\n" for case_id in ("x", "n", "a", "z")))
    assert G.main(["--corpus", str(corpus), "--results", str(results)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    monkeypatch.setattr(G, "score", lambda *_a: {"verdict": "fail"})
    assert G.main(["--corpus", str(corpus), "--results", str(results)]) == 1

    monkeypatch.setattr(sys, "argv", [
        str(REPO / "scripts" / "backfill_quality_gate.py"),
        "--corpus", str(corpus), "--results", str(results),
    ])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(REPO / "scripts" / "backfill_quality_gate.py"),
                       run_name="__main__")
    assert exc.value.code == 0
