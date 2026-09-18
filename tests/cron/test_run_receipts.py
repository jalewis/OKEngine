import hashlib
import importlib.util
import json
import time
from datetime import datetime, timezone
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


def test_run_completion_fails_without_an_executed_write(tmp_path):
    value = {"output_contract": {"completion": "run"},
             "_okengine_executed_writes": []}
    with pytest.raises(r.ReceiptError, match="executed writes=0"):
        r.verify_run_completion(value, datetime(2026, 9, 2, tzinfo=timezone.utc), tmp_path)


def test_run_completion_reads_back_required_dated_write(tmp_path):
    target = tmp_path / "briefings/daily-2026-09-02.md"
    target.parent.mkdir(parents=True)
    target.write_text("brief")
    value = {
        "output_contract": {"completion": "run",
                            "required_write_path": "briefings/daily-{date}.md"},
        "_okengine_executed_writes": [
            {"operation": "created", "path": "briefings/daily-2026-09-02.md", "version": 1}],
    }
    verified = r.verify_run_completion(
        value, datetime(2026, 9, 2, 12, tzinfo=timezone.utc), tmp_path)
    assert verified[0]["path"] == "briefings/daily-2026-09-02.md"
    assert verified[0]["sha256"].startswith("sha256:")


def test_required_date_uses_deployment_timezone_at_utc_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        target = tmp_path / "briefings/daily-2026-09-02.md"
        target.parent.mkdir(parents=True)
        target.write_text("brief")
        value = {
            "output_contract": {"completion": "run",
                                "required_write_path": "briefings/daily-{date}.md"},
            "_okengine_executed_writes": [
                {"operation": "created", "path": target.relative_to(tmp_path).as_posix(),
                 "version": 1}],
        }
        verified = r.verify_run_completion(
            value, datetime(2026, 9, 3, 3, tzinfo=timezone.utc), tmp_path)
        assert verified[0]["path"] == "briefings/daily-2026-09-02.md"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_run_completion_rejects_stale_prior_day_artifact(tmp_path):
    target = tmp_path / "briefings/daily-2026-08-31.md"
    target.parent.mkdir(parents=True)
    target.write_text("stale")
    value = {
        "output_contract": {"completion": "run",
                            "required_write_path": "briefings/daily-{date}.md"},
        "_okengine_executed_writes": [
            {"operation": "created", "path": "briefings/daily-2026-08-31.md", "version": 1}],
    }
    with pytest.raises(r.ReceiptError, match="required write was not executed"):
        r.verify_run_completion(value, datetime(2026, 9, 2, tzinfo=timezone.utc), tmp_path)


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


def test_prompt_cost_is_reported_per_accepted_disposition(tmp_path):
    keys = ["a", "b"]
    value = r.validate(receipt([accepted(key, tmp_path) for key in keys], keys),
                       selection(keys), {**job(), "prompt_metrics": {
                           "bytes": 120, "estimated_tokens": 30}}, tmp_path)
    assert value["prompt_metrics"] == {
        "bytes": 120, "estimated_tokens": 30,
        "bytes_per_successful_disposition": 60,
    }


def test_duplicate_requires_reason_and_accepted_hash_must_match(tmp_path):
    keys = ["a", "b"]
    good = accepted("a", tmp_path)
    good["writes"][0]["sha256"] = "sha256:wrong"
    value = r.validate(receipt([good, {"key": "b", "disposition": "duplicate"}], keys),
                       selection(keys), job(), tmp_path)
    assert any("hash mismatch" in e for e in value["errors"])
    assert any("requires a machine-verifiable reason" in e for e in value["errors"])
    assert value["reconciliation"] == [{
        "key": "a", "path": "sources/a.md", "claimed_sha256": "sha256:wrong",
        "observed_sha256": "sha256:" + hashlib.sha256(
            (tmp_path / "sources/a.md").read_bytes()).hexdigest(),
        "classification": "receipt-hash-mismatch",
    }]
    assert value["verified"] == []


def test_mixed_receipt_identifies_verified_and_reconcilable_items(tmp_path):
    keys = [f"item-{n}" for n in range(10)]
    items = [accepted(key, tmp_path) for key in keys]
    for item in items[:4]:
        item["writes"][0]["sha256"] = "sha256:model-copied-the-old-hash"
    value = r.validate(receipt(items, keys), selection(keys), job(), tmp_path)
    assert not value["valid"]
    assert [entry["key"] for entry in value["reconciliation"]] == keys[:4]
    assert value["verified"] == keys[4:]


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


def test_opt_in_readback_mode_normalizes_stale_model_hash(tmp_path):
    keys = ["sources/a.md|recompile-from-declared-raw"]
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection(keys)))
    page = tmp_path / "sources/a.md"
    page.parent.mkdir()
    page.write_text("authoritative post-write content")
    value = receipt([{
        "key": keys[0],
        "disposition": "accepted",
        "writes": [{"path": "wiki/sources/a.md", "sha256": "sha256:model-stale"}],
    }], keys)
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        tmp_path,
    )
    observed = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    assert result["valid"] and result["counts"]["undisposed"] == 0
    assert parsed["items"][0]["writes"][0]["sha256"] == observed
    assert result["normalized_write_hashes"] == [{
        "key": keys[0],
        "path": "wiki/sources/a.md",
        "claimed_sha256": "sha256:model-stale",
        "observed_sha256": observed,
    }]


def test_opt_in_readback_mode_normalizes_path_only_write(tmp_path):
    keys = ["raw/a.md"]
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection(keys)))
    page = tmp_path / "sources/a.md"
    page.parent.mkdir()
    page.write_text("authoritative post-write content")
    value = receipt([{
        "key": keys[0],
        "disposition": "accepted",
        "writes": ["sources/a.md"],
    }], keys)
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        tmp_path,
    )
    assert result["valid"]
    assert parsed["items"][0]["writes"][0]["path"] == "sources/a.md"
    assert parsed["items"][0]["writes"][0]["sha256"].startswith("sha256:")


def test_readback_normalizes_sharded_path_and_hash_from_telemetry(tmp_path):
    keys = ["sources/input.md|sha256:revision"]
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection(keys)))
    page = tmp_path / "entities/t/h/threat-actor-ai-agents.md"
    page.parent.mkdir(parents=True)
    page.write_text("authoritative post-write content")
    value = receipt([{
        "key": keys[0],
        "disposition": "accepted",
        "writes": [{"path": "wiki/entities/t/threat-actor-ai-agents.md", "sha256": None}],
    }], keys)
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
            "_okengine_executed_writes": [{
                "path": "entities/t/h/threat-actor-ai-agents.md",
                "operation": "created",
                "version": 1,
            }],
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        tmp_path,
    )
    write = parsed["items"][0]["writes"][0]
    assert result["valid"], result
    assert write["path"] == "wiki/entities/t/h/threat-actor-ai-agents.md"
    assert write["sha256"] == "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    assert result["normalized_write_hashes"][0]["claimed_path"] == (
        "wiki/entities/t/threat-actor-ai-agents.md")


def test_readback_normalizes_extensionless_sharded_entity_path(tmp_path):
    keys = ["sources/input.md|sha256:revision"]
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection(keys)))
    page = tmp_path / "entities/a/i/ai-attack.md"
    page.parent.mkdir(parents=True)
    page.write_text("authoritative post-write content")
    value = receipt([{
        "key": keys[0],
        "disposition": "accepted",
        "writes": [{"path": "entities/a/ai-attack", "sha256": None}],
    }], keys)
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        tmp_path,
    )
    write = parsed["items"][0]["writes"][0]
    assert result["valid"], result
    assert write["path"] == "wiki/entities/a/i/ai-attack.md"
    assert write["sha256"] == "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()


def test_single_telemetry_write_owns_collision_safe_writer_path(tmp_path):
    keys = ["raw/qualification/input.md"]
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection(keys)))
    page = tmp_path / "sources/2026/07/input-a1b2c3d4.md"
    page.parent.mkdir(parents=True)
    page.write_text("authoritative post-write content")
    value = receipt([{
        "key": keys[0],
        "disposition": "accepted",
        "writes": [{"path": "sources/2026/07/input.md", "sha256": None}],
    }], keys)
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
            "_okengine_executed_writes": [{
                "path": "sources/2026/07/input-a1b2c3d4.md",
                "operation": "created",
                "version": 1,
            }],
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        tmp_path,
    )
    assert result["valid"], result
    assert parsed["items"][0]["writes"][0]["path"] == (
        "wiki/sources/2026/07/input-a1b2c3d4.md")


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


def _sharded_accept(key, wiki, claimed_rel, actual_rel):
    """An accepted item whose page the write path SHARDED away from the claimed path."""
    actual = wiki / actual_rel
    actual.parent.mkdir(parents=True, exist_ok=True)
    actual.write_text(f"content for {key}")
    return {"key": key, "disposition": "accepted", "writes": [{
        "path": claimed_rel,
        "sha256": "sha256:" + hashlib.sha256(actual.read_bytes()).hexdigest(),
    }]}


def test_sharded_write_verifies_against_its_canonical_shard(tmp_path):
    """okengine#478: the model reports the FLAT path it asked for; the write path shards it.

    Live failure this reproduces: 14 correct entity writes were rejected as
    "accepted write does not exist" because the receipt named
    `entities/tuxbot-v3-botnet-framework.md` while the page landed at
    `entities/t/u/tuxbot-v3-botnet-framework.md`. The run failed and the selection
    was left unconsumed, so the work was redone.
    """
    wiki = tmp_path / "wiki"
    item = _sharded_accept("k1", wiki,
                           "wiki/entities/tuxbot-v3-botnet-framework.md",
                           "entities/t/u/tuxbot-v3-botnet-framework.md")
    errors, recon = r._readback(item, wiki)

    assert errors == []
    assert recon and recon[0]["classification"] == "receipt-path-sharded"
    assert recon[0]["observed_path"] == "entities/t/u/tuxbot-v3-botnet-framework.md"


def test_duplicate_slug_across_shards_is_ambiguous_not_silently_accepted(tmp_path):
    """A slug present in two shards (the okengine#54 class) must NOT pick one at random."""
    wiki = tmp_path / "wiki"
    item = _sharded_accept("k1", wiki, "wiki/entities/mirai.md", "entities/m/i/mirai.md")
    (wiki / "entities" / "m").mkdir(parents=True, exist_ok=True)
    (wiki / "entities" / "m" / "mirai.md").write_text("a rival copy")

    errors, _ = r._readback(item, wiki)
    assert any("ambiguous after sharding" in e for e in errors), errors


def test_genuinely_missing_write_still_fails(tmp_path):
    """The fallback must not turn a fabricated write into a pass."""
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    item = {"key": "k1", "disposition": "accepted",
            "writes": [{"path": "wiki/entities/never-written.md", "sha256": "sha256:00"}]}
    errors, _ = r._readback(item, wiki)
    assert any("does not exist" in e for e in errors)


def test_shard_resolution_stays_inside_the_claimed_namespace(tmp_path):
    """A same-named page in ANOTHER namespace must not satisfy the claim."""
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    decoy = wiki / "concepts" / "c" / "acme.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("wrong namespace")
    item = {"key": "k1", "disposition": "accepted",
            "writes": [{"path": "wiki/entities/acme.md", "sha256": "sha256:00"}]}
    errors, _ = r._readback(item, wiki)
    assert any("does not exist" in e for e in errors), errors


def test_sharded_write_still_checks_the_hash(tmp_path):
    """Resolving the shard must not skip hash verification."""
    wiki = tmp_path / "wiki"
    item = _sharded_accept("k1", wiki, "wiki/entities/acme.md", "entities/a/c/acme.md")
    item["writes"][0]["sha256"] = "sha256:" + "0" * 64      # stale/fabricated
    errors, _ = r._readback(item, wiki)
    assert any("hash mismatch" in e for e in errors), errors


def _tele_job(paths):
    """A job dict carrying patch-14 write telemetry."""
    return {"id": "lane-1", "output_contract_digest": "sha256:contract",
            "_okengine_executed_writes": [
                {"operation": "created", "path": p, "version": 1,
                 "tool": "mcp__okengine_write_x__create_entity"} for p in paths]}


def test_telemetry_resolves_the_sharded_path_without_scanning(tmp_path):
    """okengine#469: the write tool's own result carries the canonical post-shard path."""
    wiki = tmp_path / "wiki"
    item = _sharded_accept("k1", wiki, "wiki/entities/acme.md", "entities/a/c/acme.md")
    tele = r._telemetry_paths(_tele_job(["entities/a/c/acme.md"]))
    errors, _ = r._readback(item, wiki, tele)
    assert errors == [], errors


def test_accepted_write_absent_from_telemetry_is_refused(tmp_path):
    """A receipt claiming credit for a page THIS run did not write must fail.

    This is the anti-fabrication signal the receipt was always meant to carry — now
    taken from telemetry rather than the model's word.
    """
    wiki = tmp_path / "wiki"
    pre = wiki / "entities" / "a" / "c" / "acme.md"
    pre.parent.mkdir(parents=True)
    pre.write_text("a page that already existed")
    item = {"key": "k1", "disposition": "accepted", "writes": [{
        "path": "wiki/entities/a/c/acme.md",
        "sha256": "sha256:" + hashlib.sha256(pre.read_bytes()).hexdigest()}]}
    tele = r._telemetry_paths(_tele_job(["entities/other/thing.md"]))
    errors, _ = r._readback(item, wiki, tele)
    assert any("not performed by this run" in e for e in errors), errors


def test_absent_telemetry_stays_silent_for_older_images(tmp_path):
    """No `_okengine_executed_writes` key = pre-patch-14 gateway. Must not fail the run."""
    assert r._telemetry_paths({"id": "lane-1"}) is None
    wiki = tmp_path / "wiki"
    item = _sharded_accept("k1", wiki, "wiki/entities/acme.md", "entities/a/c/acme.md")
    errors, _ = r._readback(item, wiki, None)      # falls back to the shard scan
    assert errors == [], errors


def test_executed_writes_ignores_malformed_telemetry_entries():
    job = {"_okengine_executed_writes": [{"path": "entities/a.md"}, "junk", {"nope": 1}]}
    assert r.executed_writes(job) == [{"path": "entities/a.md"}]


def test_empty_telemetry_never_refuses_a_write(tmp_path):
    """Regression: an EMPTY telemetry list is not proof the run wrote nothing.

    It is equally the signature of a telemetry PARSE failure. Treating the two alike
    turned 28 real entity writes into "accepted write not performed by this run" on the
    canary — caught before the fleet roll. Only entry-bearing telemetry may refuse.
    """
    wiki = tmp_path / "wiki"
    page = wiki / "entities" / "a" / "c" / "acme.md"
    page.parent.mkdir(parents=True)
    page.write_text("real content written by this run")
    item = {"key": "k1", "disposition": "accepted", "writes": [{
        "path": "wiki/entities/a/c/acme.md",
        "sha256": "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()}]}

    errors, _ = r._readback(item, wiki, set())      # telemetry parsed to nothing
    assert errors == [], errors


def _sel(keys):
    return {"selected": keys, "input_digest": r.digest_items(keys),
            "lane_id": "lane-1", "contract_digest": "sha256:contract"}


def _wrote(wiki, rels):
    out = []
    for rel in rels:
        p = wiki / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {rel}")
        out.append({"operation": "created", "path": rel, "version": 1,
                    "tool": "mcp__okengine_write_x__create_entity"})
    return out


def test_synthesizes_a_receipt_when_the_model_omitted_one(tmp_path):
    """okengine#469: the largest failure class is the model doing the work and not describing it."""
    wiki = tmp_path / "wiki"
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": _wrote(wiki, ["entities/t/u/tuxbot.md",
                                                      "entities/k/e/keksec.md"])}
    got = r.synthesize_receipt(job, _sel(["src-1"]), wiki)

    assert got is not None
    assert got["items"][0]["key"] == "src-1"
    assert got["items"][0]["disposition"] == "accepted"
    assert [w["path"] for w in got["items"][0]["writes"]] == [
        "wiki/entities/k/e/keksec.md", "wiki/entities/t/u/tuxbot.md"]
    # hashes are computed from disk, never transcribed
    for w in got["items"][0]["writes"]:
        assert w["sha256"].startswith("sha256:") and len(w["sha256"]) == 71


def _wrote_citing(wiki, rel, cites):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\ntype: malware\nsources:\n" + "".join(f"- {c}\n" for c in cites) + "---\n# page\n"
    p.write_text(body)
    return {"operation": "created", "path": rel, "version": 1,
            "tool": "mcp__okengine_write_x__create_entity"}


def test_multi_item_attribution_uses_the_page_citation(tmp_path):
    """okengine#469: with N inputs, attribution is DERIVABLE, not a guess.

    A selection key is `<path>|<revision>`; the selector advertises the input as
    `sources/<path-without-suffix>` and a grounded page carries exactly that in its
    `sources:` list. So the key -> citation mapping is deterministic.
    """
    wiki = tmp_path / "wiki"
    k1, k2 = "2026/07/20/rst.md|sha256:aa", "2026/06/30/other.md|sha256:bb"
    tele = [_wrote_citing(wiki, "entities/t/u/tuxbot.md", ["sources/2026/07/20/rst"]),
            _wrote_citing(wiki, "entities/o/t/othr.md", ["sources/2026/06/30/other"])]
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}

    got = r.synthesize_receipt(job, _sel([k1, k2]), wiki)
    bykey = {i["key"]: i for i in got["items"]}
    assert bykey[k1]["disposition"] == "accepted"
    assert bykey[k1]["writes"][0]["path"] == "wiki/entities/t/u/tuxbot.md"
    assert bykey[k2]["writes"][0]["path"] == "wiki/entities/o/t/othr.md"


def test_unwritten_input_is_DEFERRED_not_skipped(tmp_path):
    """We know no write happened for it; we do NOT know it was considered and declined.

    `deferred` is retryable, so the input is re-offered rather than consumed on an
    assumption — the line between recovering accounting and inventing it.
    """
    wiki = tmp_path / "wiki"
    k1, k2 = "2026/07/20/rst.md|sha256:aa", "2026/06/30/other.md|sha256:bb"
    tele = [_wrote_citing(wiki, "entities/t/u/tuxbot.md", ["sources/2026/07/20/rst"])]
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}

    got = r.synthesize_receipt(job, _sel([k1, k2]), wiki)
    bykey = {i["key"]: i for i in got["items"]}
    assert bykey[k2]["disposition"] == "deferred-for-review"
    assert "no write recorded" in bykey[k2]["reason"]
    assert bykey[k2]["disposition"] in r.RETRYABLE


def test_a_page_citing_two_inputs_counts_for_both(tmp_path):
    """Evidence for two inputs is not a partition problem — claim it for both."""
    wiki = tmp_path / "wiki"
    k1, k2 = "a/one.md|sha256:aa", "b/two.md|sha256:bb"
    tele = [_wrote_citing(wiki, "entities/x/y/z.md", ["sources/a/one", "sources/b/two"])]
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}
    got = r.synthesize_receipt(job, _sel([k1, k2]), wiki)
    assert all(i["disposition"] == "accepted" for i in got["items"])


def test_unattributable_multi_input_writes_are_conservatively_deferred(tmp_path):
    """Bounded runs get terminal accounting without claiming ambiguous writes."""
    wiki = tmp_path / "wiki"
    tele = [_wrote_citing(wiki, "entities/a/c/acme.md", ["sources/unrelated/page"])]
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}
    got = r.synthesize_receipt(
        job, _sel(["a/one.md|sha256:aa", "b/two.md|sha256:bb"]), wiki)
    assert got is not None
    assert {item["disposition"] for item in got["items"]} == {"deferred-for-review"}
    assert all("could not be attributed" in item["reason"] for item in got["items"])


def test_no_synthesis_without_telemetry(tmp_path):
    """A pre-patch-14 gateway must fail rather than assume the run wrote nothing."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    assert r.synthesize_receipt({"id": "lane-1"}, _sel(["src-1"]), wiki) is None


def test_empty_write_telemetry_defers_every_selected_item(tmp_path):
    """An explicit empty ledger proves no governed write succeeded.

    Deferral is retryable and makes max-iteration exhaustion account for every
    selected input without claiming that the model considered or completed it.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    job = {
        "id": "lane-1",
        "output_contract_digest": "sha256:contract",
        "_okengine_executed_writes": [],
    }

    got = r.synthesize_receipt(job, _sel(["src-1", "src-2"]), wiki)

    assert [item["key"] for item in got["items"]] == ["src-1", "src-2"]
    assert {item["disposition"] for item in got["items"]} == {"deferred-for-review"}
    assert all(item["reason"] == "no successful governed write recorded by this run"
               for item in got["items"])


def test_missing_model_receipt_with_empty_write_telemetry_validates_as_deferred(tmp_path):
    """A bounded run may spend its final turn without emitting the requested receipt."""
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(_sel(["raw/example.md|sha256:before"])))
    job = {
        "id": "lane-1",
        "output_contract_digest": "sha256:contract",
        "selection_manifest": str(manifest),
        "_okengine_executed_writes": [],
    }

    parsed, result = r.verify_response(job, "Maximum iterations reached.", tmp_path)

    assert result["valid"], result
    assert result["receipt_source"] == "telemetry-synthesized"
    assert result["counts"]["deferred-for-review"] == 1
    assert parsed["items"][0]["key"] == "raw/example.md|sha256:before"


def test_synthesizes_changed_update_in_place_without_mcp_telemetry(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "entities" / "acme.md"
    page.parent.mkdir(parents=True)
    page.write_text("before")
    before = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    key = f"wiki/entities/acme.md|{before}"
    page.write_text("after")
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": []}

    got = r.synthesize_receipt(job, _sel([key]), wiki)

    assert got["items"] == [{
        "key": key,
        "disposition": "accepted",
        "writes": [{
            "path": "wiki/entities/acme.md",
            "sha256": "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest(),
        }],
    }]


def test_unchanged_update_in_place_is_deferred(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "entities" / "acme.md"
    page.parent.mkdir(parents=True)
    page.write_text("same")
    before = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    key = f"wiki/entities/acme.md|{before}"
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": []}

    got = r.synthesize_receipt(job, _sel([key]), wiki)

    assert got["items"] == [{
        "key": key,
        "disposition": "deferred-for-review",
        "reason": "no successful governed write recorded by this run",
    }]


def test_changed_selected_page_wins_over_unrelated_mcp_telemetry(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "predictions" / "one.md"
    unrelated = wiki / "entities" / "other.md"
    page.parent.mkdir(parents=True)
    unrelated.parent.mkdir(parents=True)
    page.write_text("before")
    before = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    key = f"wiki/predictions/one.md|{before}"
    page.write_text("after")
    unrelated.write_text("other")
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": [{"path": "entities/other.md"}]}

    got = r.synthesize_receipt(job, _sel([key]), wiki)

    assert got["items"][0]["writes"][0]["path"] == "wiki/predictions/one.md"


def test_invalid_model_receipt_is_repaired_from_changed_selected_page(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "sources" / "one.md"
    page.parent.mkdir(parents=True)
    page.write_text("before")
    before = "sha256:" + hashlib.sha256(page.read_bytes()).hexdigest()
    key = f"wiki/sources/one.md|{before}"
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(_sel([key])))
    page.write_text("after")
    value = receipt([{
        "key": key, "disposition": "accepted",
        "writes": [{"path": "wiki/entities/wrong.md", "sha256": None}],
    }], [key])
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
            "_okengine_executed_writes": [{"path": "entities/unrelated.md"}],
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        wiki,
    )
    assert result["valid"], result
    assert result["receipt_source"] == "evidence-repaired-invalid-receipt"
    assert parsed["items"][0]["writes"][0]["path"] == "wiki/sources/one.md"


def test_no_synthesis_when_a_telemetried_page_is_missing(tmp_path):
    """Telemetry claiming a page that is not on disk must not become a passing receipt."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": [{"operation": "created", "path": "entities/a/gone.md",
                                          "version": 1, "tool": "mcp__okengine_write_x__create_entity"}]}
    assert r.synthesize_receipt(job, _sel(["src-1"]), wiki) is None


def test_repeated_writes_to_one_page_collapse_to_one_record(tmp_path):
    """created-then-updated in one run is ONE durable effect, not two."""
    wiki = tmp_path / "wiki"
    tele = _wrote(wiki, ["entities/a/c/acme.md"])
    tele.append({"operation": "updated", "path": "entities/a/c/acme.md", "version": 2,
                 "tool": "mcp__okengine_write_x__update_entity"})
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}
    got = r.synthesize_receipt(job, _sel(["src-1"]), wiki)
    assert len(got["items"][0]["writes"]) == 1


def test_a_synthesized_receipt_still_passes_full_validation(tmp_path):
    """Synthesis recovers the ACCOUNTING; it must not bypass the checks."""
    wiki = tmp_path / "wiki"
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": _wrote(wiki, ["entities/a/c/acme.md"])}
    selection = _sel(["src-1"])
    receipt = r.synthesize_receipt(job, selection, wiki)
    result = r.validate(receipt, selection, job, wiki)
    assert result["valid"] is True, result["errors"]


def test_update_in_place_lane_attributes_by_PATH_IDENTITY(tmp_path):
    """okengine#485: source-quality-backfill writes the very page it selected.

    Its keys are `wiki/sources/<path>` and the write lands on that same path, so the write is
    attributable without any citation — and without even reading the file.
    """
    wiki = tmp_path / "wiki"
    k = "wiki/sources/2026/07/20/rst.md|sha256:aa"
    tele = _wrote(wiki, ["sources/2026/07/20/rst.md"])
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}

    got = r.synthesize_receipt(job, _sel([k, "wiki/sources/other.md|sha256:bb"]), wiki)
    bykey = {i["key"]: i for i in got["items"]}
    assert bykey[k]["disposition"] == "accepted"
    assert bykey["wiki/sources/other.md|sha256:bb"]["disposition"] == "deferred-for-review"


def test_raw_lane_attributes_via_the_raw_citation(tmp_path):
    """okengine#485: raw-backfill keys are raw paths, and the written source page lists them.

    The page carries the key path VERBATIM in its `raw:` list, so the same citation mechanism
    that works for entity lanes works here with no new machinery.
    """
    wiki = tmp_path / "wiki"
    k1 = "raw/indicators/2026-07-06-agentic-soc-f071e.md|sha256:aa"
    k2 = "raw/threat-actors/2026-07-09-guardrails-e9ee.md|sha256:bb"
    tele = [_wrote_citing(wiki, "sources/2026/07/06/agentic-soc.md",
                          ["raw/indicators/2026-07-06-agentic-soc-f071e.md"])]
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": tele}

    got = r.synthesize_receipt(job, _sel([k1, k2]), wiki)
    bykey = {i["key"]: i for i in got["items"]}
    assert bykey[k1]["disposition"] == "accepted"
    assert bykey[k2]["disposition"] == "deferred-for-review"  # not written this run


def test_single_input_still_claims_uncited_writes(tmp_path):
    """With ONE selected input there is nothing else the writes could belong to.

    This is the only case where claiming without an edge is a fact rather than a guess, and it
    must survive the attribution generalisation.
    """
    wiki = tmp_path / "wiki"
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": _wrote(wiki, ["entities/a/c/acme.md"])}
    got = r.synthesize_receipt(job, _sel(["src-1"]), wiki)
    assert got["items"][0]["disposition"] == "accepted"


@pytest.mark.parametrize("bad_disposition", [None, "pending"])
def test_nonterminal_dispositions_are_repaired_from_executed_write_telemetry(
    tmp_path, bad_disposition
):
    key = "wiki/sources/a.md"
    manifest = tmp_path / "selection.json"
    manifest.write_text(json.dumps(selection([key])))
    page = tmp_path / "sources" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("source body")
    value = receipt([{
        "key": key,
        "disposition": bad_disposition,
        "writes": [{"path": key, "sha256": None}],
        "reason": None,
    }], [key])
    parsed, result = r.verify_response(
        {
            **job(),
            "selection_manifest": str(manifest),
            "receipt_hash_mode": "readback",
            "_okengine_executed_writes": [{
                "path": "sources/a.md",
                "operation": "updated",
                "version": 2,
            }],
        },
        "```okengine-receipt\n" + json.dumps(value) + "\n```",
        tmp_path,
    )
    assert result["valid"], result
    assert result["receipt_source"] == "telemetry-repaired-nonterminal-dispositions"
    assert parsed["items"][0]["disposition"] == "accepted"


def test_multi_input_with_no_edge_never_claims_acceptance(tmp_path):
    """The generalisation provides accounting but remains no licence to claim writes."""
    wiki = tmp_path / "wiki"
    job = {"id": "lane-1", "output_contract_digest": "sha256:contract",
           "_okengine_executed_writes": _wrote(wiki, ["entities/a/c/acme.md"])}
    got = r.synthesize_receipt(
        job, _sel(["a/one.md|sha256:aa", "b/two.md|sha256:bb"]), wiki)
    assert got is not None
    assert [item["disposition"] for item in got["items"]] == [
        "deferred-for-review", "deferred-for-review"]
    assert all("writes" not in item for item in got["items"])
