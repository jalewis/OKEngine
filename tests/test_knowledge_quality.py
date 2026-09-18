import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "knowledge_quality", ROOT / "scripts/cron/knowledge_quality.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_disposition_vocabulary_is_stable_and_legacy_values_map():
    assert MODULE.DISPOSITIONS == (
        "accepted", "merged", "updated", "rejected-out-of-scope",
        "insufficient-evidence", "duplicate", "deferred-for-review", "failed",
    )
    assert MODULE.canonical_disposition("skipped") == "rejected-out-of-scope"


def test_receipt_rollup_distinguishes_unknown_from_measured_zero(tmp_path):
    root = tmp_path / "receipts/lane"
    root.mkdir(parents=True)
    (root / "one.json").write_text(json.dumps({
        "counts": {"selected": 2, "accepted": 2, "undisposed": 0}}))
    report = MODULE.rollup(tmp_path / "receipts")
    assert report["metrics"]["disposition_completeness"]["value"] == 1
    duplicate = report["metrics"]["canonical_duplicate_rate"]
    assert duplicate == {"state": "measured", "value": 0.0,
                         "numerator": 0, "denominator": 2}
    assert report["metrics"]["unsupported_claim_rate"]["state"] == "unknown"
    assert report["metrics"]["unsupported_claim_rate"]["value"] is None


def test_rollup_does_not_double_count_legacy_alias_beside_canonical_counter(tmp_path):
    root = tmp_path / "receipts/lane"
    root.mkdir(parents=True)
    (root / "one.json").write_text(json.dumps({
        "counts": {"selected": 1, "rejected-out-of-scope": 1, "skipped": 1}}))

    report = MODULE.rollup(tmp_path / "receipts")
    assert report["dispositions"]["rejected-out-of-scope"] == 1
    assert report["disposed"] == 1
    assert report["metrics"]["disposition_completeness"]["value"] == 1


def test_adjudicated_snapshot_supplies_human_truth_metrics(tmp_path):
    snapshot = tmp_path / "adjudication.json"
    snapshot.write_text(json.dumps({"metrics": {
        "unsupported_claim_rate": {"state": "measured", "value": 0.02,
                                   "sample_size": 100, "adjudicator": "blind-dual"},
        "review_precision": {"state": "measured", "value": 0.8},
    }}))
    metrics = MODULE.rollup(tmp_path / "missing", snapshot)["metrics"]
    assert metrics["unsupported_claim_rate"]["value"] == 0.02
    assert metrics["retrieval_usefulness"]["state"] == "unknown"


def test_markdown_makes_unknown_explicit(tmp_path):
    text = "\n".join(MODULE.markdown(MODULE.rollup(tmp_path / "missing")))
    assert "Knowledge quality" in text
    assert "Unknown is not zero" in text


def test_metric_units_and_malformed_evidence_paths(tmp_path):
    assert MODULE.measured(2.5, unit="hours") == {
        "state": "measured", "value": 2.5, "unit": "hours"}
    assert MODULE.canonical_disposition("not-a-disposition") is None
    root = tmp_path / "receipts/lane"
    root.mkdir(parents=True)
    (root / "broken.json").write_text("{")
    (root / "scalar.json").write_text("[]")
    assert MODULE.rollup(tmp_path / "receipts")["receipts"] == 0

    snapshot = tmp_path / "adjudication.json"
    snapshot.write_text("{")
    assert MODULE.rollup(tmp_path / "missing", snapshot)["metrics"][
        "review_precision"]["state"] == "unknown"
    snapshot.write_text("[]")
    assert MODULE.rollup(tmp_path / "missing", snapshot)["metrics"][
        "review_precision"]["state"] == "unknown"
    snapshot.write_text(json.dumps({"metrics": {
        "review_precision": {"state": "invalid", "value": 1},
    }}))
    assert MODULE.rollup(tmp_path / "missing", snapshot)["metrics"][
        "review_precision"]["state"] == "unknown"
