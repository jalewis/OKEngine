import importlib.util
import json
from pathlib import Path


MOD = Path(__file__).parents[2] / "scripts" / "cron" / "model_write_audit.py"
spec = importlib.util.spec_from_file_location("model_write_audit", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def contract():
    return {"api": 1, "allowed_namespaces": ["sources"], "allowed_types": ["source"],
            "operations": ["create", "update"], "required_fields": ["type", "raw", "publisher"],
            "required_relationships": [], "body": {"required": True, "min_non_whitespace": 80},
            "unknown_fields": "reject", "unresolved_links": "reject",
            "placeholder_links": "reject", "completion": "per-selected-item"}


def fixture(tmp_path):
    page = tmp_path / "wiki" / "sources" / "bad.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: source\nproducer_lane: compile\nraw: raw/bad.md\nversion: 2\n---\n\n[More](#) [[entities/missing]]\n")
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [
        {"name": "compile", "enabled_toolsets": ["okengine-write"], "output_contract": contract(),
         "audit_markers": ["raw"]},
        {"name": "unsafe", "enabled_toolsets": ["okengine-write"]},
    ]}))
    return page, jobs


def test_audit_is_read_only_and_attributes_lane_type_reason(tmp_path):
    page, jobs = fixture(tmp_path)
    before = page.read_bytes()
    report = m.audit(tmp_path, jobs, now="2026-07-21T00:00:00Z")
    assert page.read_bytes() == before
    assert report["last_successful_audit"] == "2026-07-21T00:00:00Z"
    reasons = {f["reason"] for f in report["findings"]}
    assert {"contract_missing", "required_field_missing", "body_too_short",
            "placeholder_link", "unresolved_link"} <= reasons
    page_findings = [f for f in report["findings"] if f.get("scope") == "page"]
    assert all(f["lane"] == "compile" and f["type"] == "source" for f in page_findings)
    assert report["strict_readiness"]["compile"] is False


def test_repair_plan_is_hash_locked_dry_run_and_never_invents_evidence(tmp_path):
    page, jobs = fixture(tmp_path)
    plan = m.repair_plan(m.audit(tmp_path, jobs))
    assert plan["dry_run"] is True and plan["actions"]
    assert all(a["expected_sha256"].startswith("sha256:") for a in plan["actions"])
    assert all(a["expected_version"] == 2 and a["fabricate_evidence"] is False
               for a in plan["actions"])


def test_readiness_alerts_on_stale_audit_and_acceptance_regression():
    from datetime import datetime, timezone
    alerts = m.readiness_alerts(
        {"last_successful_audit": "2026-07-19T00:00:00Z"},
        {"selected": 30, "accepted": 3, "undisposed": 27},
        now=datetime(2026, 7, 21, tzinfo=timezone.utc))
    assert {a["reason"] for a in alerts} == {
        "audit_stale", "acceptance_regression", "undisposed_inputs"}


def test_mixed_producer_source_is_not_guessed_as_raw_backfill(tmp_path):
    source = tmp_path / "wiki" / "sources" / "feed.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source\npublisher: Feed\npublished: 2026-07-20\n---\n\n"
                      + "Legitimate deterministic feed content. " * 5)
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"name": "raw-backfill",
        "enabled_toolsets": ["okengine-write"], "output_contract": contract(),
        "audit_markers": ["raw"]}]}))
    report = m.audit(tmp_path, jobs)
    assert not any(f.get("path") == "sources/feed.md" for f in report["findings"])
    assert m.repair_plan(report)["actions"] == []


def test_canonical_link_resolves_to_physically_sharded_page(tmp_path):
    source = tmp_path / "wiki" / "sources" / "compiled.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\ntype: source\nraw: raw/compiled.md\npublisher: Example\n"
                      "producer_lane: raw-backfill\n---\n\n" + "Grounded content. " * 6
                      + " See [[entities/qilin]].\n")
    entity = tmp_path / "wiki" / "entities" / "q" / "qilin.md"
    entity.parent.mkdir(parents=True)
    entity.write_text("---\ntype: entity\n---\n\n# Qilin\n")
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{"name": "raw-backfill",
        "enabled_toolsets": ["okengine-write"], "output_contract": contract(),
        "audit_markers": ["raw"]}]}))
    report = m.audit(tmp_path, jobs)
    assert not any(f.get("path") == "sources/compiled.md" and f["reason"] == "unresolved_link"
                   for f in report["findings"])
