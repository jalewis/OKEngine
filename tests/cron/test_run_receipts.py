import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

MOD = Path(__file__).parents[2] / "patches" / "cron-plus" / "run_receipts.py"
spec = importlib.util.spec_from_file_location("run_receipts", MOD)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def selection(keys):
    return {"selected": keys, "input_digest": r.digest_items(keys),
            "lane_id": "lane-1", "contract_digest": "sha256:contract"}


def job():
    return {"id": "lane-1", "output_contract_digest": "sha256:contract"}


def accepted(key, wiki):
    path = wiki / f"sources/{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"content for {key}")
    return {"key": key, "disposition": "accepted", "writes": [{
        "path": f"sources/{key}.md",
        "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    }]}


def receipt(items, selected):
    return {"api": 1, "run_id": "run-1", "lane_id": "lane-1",
            "contract_digest": "sha256:contract", "input_digest": r.digest_items(selected),
            "items": items}


def test_thirty_selected_three_writes_cannot_succeed(tmp_path):
    keys = [f"item-{n}" for n in range(30)]
    value = r.validate(receipt([accepted(k, tmp_path) for k in keys[:3]], keys),
                       selection(keys), job(), tmp_path)
    assert not value["valid"] and value["state"] == "failed"
    assert value["counts"]["accepted"] == 3 and value["counts"]["undisposed"] == 27


def test_exactly_one_terminal_disposition_and_readback(tmp_path):
    keys = ["a", "b", "c"]
    items = [accepted("a", tmp_path),
             {"key": "b", "disposition": "duplicate", "reason": "canonical:sources/a"},
             {"key": "c", "disposition": "deferred", "reason": "budget"}]
    value = r.validate(receipt(items, keys), selection(keys), job(), tmp_path)
    assert value["valid"] and value["state"] == "degraded" and value["retry"] == ["c"]
    items.append({"key": "a", "disposition": "skipped", "reason": "duplicate accounting"})
    assert not r.validate(receipt(items, keys), selection(keys), job(), tmp_path)["valid"]


def test_duplicate_requires_reason_and_accepted_hash_must_match(tmp_path):
    keys = ["a", "b"]
    good = accepted("a", tmp_path)
    good["writes"][0]["sha256"] = "sha256:wrong"
    value = r.validate(receipt([good, {"key": "b", "disposition": "duplicate"}], keys),
                       selection(keys), job(), tmp_path)
    assert any("hash mismatch" in e for e in value["errors"])
    assert any("requires a machine-verifiable reason" in e for e in value["errors"])


def test_accepted_review_flag_verifies_queue_effect_without_target_write(tmp_path):
    key = "cves/windows-legacyhive-zero-day.md|quarantine-for-review"
    queue = tmp_path / "_review-queue.md"
    queue.write_text("- 2026-07-21 **cves/windows-legacyhive-zero-day.md** — review\n")
    flag_job = {**job(), "output_contract": {"operations": ["flag"]}}
    value = r.validate(receipt([{
        "key": key, "disposition": "accepted", "writes": [],
    }], [key]), selection([key]), flag_job, tmp_path)
    assert value["valid"] and value["state"] == "succeeded"


def test_accepted_review_flag_fails_without_queue_effect(tmp_path):
    key = "cves/missing.md|quarantine-for-review"
    flag_job = {**job(), "output_contract": {"operations": ["flag"]}}
    value = r.validate(receipt([{
        "key": key, "disposition": "accepted", "writes": [],
    }], [key]), selection([key]), flag_job, tmp_path)
    assert not value["valid"]
    assert any("no review queue" in error for error in value["errors"])


@pytest.mark.parametrize("response,message", [
    ("ordinary prose", "missing"),
    ("```okengine-receipt\n{bad}\n```", "invalid receipt JSON"),
])
def test_missing_and_invalid_model_receipts_fail(response, message):
    with pytest.raises(r.ReceiptError, match=message):
        r.parse_response(response)


def expected(keys):
    return {"lane_id": "lane-1", "contract_digest": "sha256:contract",
            "input_digest": r.digest_items(keys), "selected": keys}


def test_recovers_single_identity_matching_json_fence_with_prose():
    keys = ["a", "b"]
    value = receipt([
        {"key": "a", "disposition": "deferred", "writes": [], "reason": "later"},
        {"key": "b", "disposition": "skipped", "writes": [], "reason": "duplicate"},
    ], keys)
    response = "Completed the batch.\n```json\n" + json.dumps(value) + "\n```\nDone."
    parsed, source = r.parse_response_details(response, expected(keys))
    assert parsed == value
    assert source == "recovered-json"


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(lane_id="wrong"),
    lambda value: value.update(contract_digest="sha256:wrong"),
    lambda value: value.update(input_digest="sha256:wrong"),
    lambda value: value["items"].pop(),
    lambda value: value["items"].append({"key": "extra", "disposition": "skipped"}),
])
def test_recovery_rejects_stale_or_incomplete_identity(mutate):
    keys = ["a", "b"]
    value = receipt([{"key": key, "disposition": "deferred", "reason": "later"}
                     for key in keys], keys)
    mutate(value)
    with pytest.raises(r.ReceiptError, match="missing"):
        r.parse_response("```json\n" + json.dumps(value) + "\n```", expected(keys))


def test_recovery_rejects_multiple_matching_candidates():
    keys = ["a"]
    value = receipt([{"key": "a", "disposition": "deferred", "reason": "later"}], keys)
    block = "```json\n" + json.dumps(value) + "\n```"
    with pytest.raises(r.ReceiptError, match="multiple identity-matching"):
        r.parse_response(block + "\n" + block, expected(keys))


def test_canonical_receipt_remains_preferred_over_recovery_candidate():
    keys = ["a"]
    value = receipt([{"key": "a", "disposition": "deferred", "reason": "later"}], keys)
    response = ("```okengine-receipt\n" + json.dumps(value) + "\n```\n"
                "```json\n" + json.dumps(value) + "\n```")
    parsed, source = r.parse_response_details(response, expected(keys))
    assert parsed == value and source == "canonical"


def test_verify_response_records_recovery_source(tmp_path):
    keys = ["a"]
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection(keys)))
    lane = {**job(), "selection_manifest": str(manifest)}
    value = receipt([{"key": "a", "disposition": "deferred", "reason": "later"}], keys)
    parsed, result = r.verify_response(
        lane, "prose\n```json\n" + json.dumps(value) + "\n```", tmp_path)
    assert parsed == value
    assert result["valid"] and result["receipt_source"] == "recovered-json"


def test_recovers_complete_receipt_with_missing_closing_fence():
    keys = ["a"]
    value = receipt([{"key": "a", "disposition": "deferred", "reason": "no raw"}], keys)
    parsed, source = r.parse_response_details(
        "prose\n```okengine-receipt\n" + json.dumps(value), expected(keys))
    assert parsed == value
    assert source == "recovered-unterminated-fence"


@pytest.mark.parametrize("suffix", [" trailing prose", "\n```json\n{}\n```"])
def test_unterminated_receipt_rejects_trailing_payload(suffix):
    keys = ["a"]
    value = receipt([{"key": "a", "disposition": "deferred", "reason": "no raw"}], keys)
    with pytest.raises(r.ReceiptError, match="invalid"):
        r.parse_response("```okengine-receipt\n" + json.dumps(value) + suffix, expected(keys))


def test_unterminated_receipt_rejects_truncated_json():
    keys = ["a"]
    with pytest.raises(r.ReceiptError, match="invalid unterminated"):
        r.parse_response('```okengine-receipt\n{"api": 1, "lane_id": "lane-1"',
                         expected(keys))


def test_runner_crash_receipt_shape_is_failed_not_success(tmp_path):
    # A persisted invalid/missing receipt is represented conservatively by the validator.
    keys = ["a"]
    value = r.validate(receipt([], keys), selection(keys), job(), tmp_path)
    assert value["state"] == "failed" and value["counts"]["undisposed"] == 1
