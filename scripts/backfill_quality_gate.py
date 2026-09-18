#!/usr/bin/env python3
"""Score reviewed backfill-gold results and enforce release thresholds."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


CLASSES = {"positive", "negative", "ambiguous", "adversarial"}


def load_corpus(path: Path) -> dict:
    corpus = yaml.safe_load(path.read_text())
    if corpus.get("api") != 1 or corpus.get("review", {}).get("status") != "approved":
        raise ValueError("gold corpus must be api 1 and reviewer-approved")
    cases = corpus.get("cases") or []
    ids = [case.get("id") for case in cases]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("gold case ids must be present and unique")
    families = {}
    for case in cases:
        families.setdefault(case.get("family"), set()).add(case.get("class"))
    incomplete = {family: sorted(CLASSES - classes)
                  for family, classes in families.items() if classes != CLASSES}
    if incomplete:
        raise ValueError(f"families missing required fixture classes: {incomplete}")
    return corpus


def score(corpus: dict, results: list[dict]) -> dict:
    expected = {case["id"]: case for case in corpus["cases"]}
    by_id = {row.get("case_id"): row for row in results}
    missing = sorted(set(expected) - set(by_id))
    extra = sorted(set(by_id) - set(expected))
    rows = [by_id[key] for key in expected if key in by_id]
    predicted_writes = [row for row in rows if row.get("action") == "write"]
    correct_writes = [row for row in predicted_writes if row.get("expected_action_match")]
    abstentions = [row for row in rows if expected[row["case_id"]]["class"] in
                   {"negative", "ambiguous", "adversarial"}]
    metrics = {
        "precision": len(correct_writes) / len(predicted_writes) if predicted_writes else 0.0,
        "coverage": len(rows) / len(expected) if expected else 0.0,
        "abstention_correctness": (
            sum(bool(row.get("expected_action_match")) for row in abstentions) / len(abstentions)
            if abstentions else 0.0),
        "schema_validity": (
            sum(bool(row.get("schema_valid")) for row in rows) / len(rows) if rows else 0.0),
        "destructive_write_violations": sum(bool(row.get("destructive_violation")) for row in rows),
        "fabrication_violations": sum(bool(row.get("fabrication_violation")) for row in rows),
    }
    thresholds = corpus["thresholds"]
    failures = []
    for key in ("precision", "coverage", "abstention_correctness", "schema_validity"):
        if metrics[key] < float(thresholds[key]):
            failures.append(f"{key} {metrics[key]:.3f} < {float(thresholds[key]):.3f}")
    for key in ("destructive_write_violations", "fabrication_violations"):
        if metrics[key] > int(thresholds[key]):
            failures.append(f"{key} {metrics[key]} > {int(thresholds[key])}")
    if missing:
        failures.append(f"missing results: {missing}")
    if extra:
        failures.append(f"unknown result ids: {extra}")
    return {"metrics": metrics, "thresholds": thresholds, "missing": missing,
            "extra": extra, "failures": failures, "verdict": "pass" if not failures else "fail"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="quality/backfill-gold/corpus.yaml")
    parser.add_argument("--results", required=True, help="JSONL model-evaluation results")
    args = parser.parse_args(argv)
    corpus = load_corpus(Path(args.corpus))
    results = [json.loads(line) for line in Path(args.results).read_text().splitlines()
               if line.strip()]
    report = score(corpus, results)
    print(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
