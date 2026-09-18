import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize(("physical_paths", "target", "unresolved"), [
    (["entities/q/qilin.md"], "entities/qilin", False),
    (["concepts/q/qilin.md"], "entities/qilin", True),
    (["entities/q/qilin.md", "entities/x/qilin.md"], "entities/qilin", True),
    (["entities/weapon/q/qilin.md", "entities/other/q/qilin.md"],
     "entities/weapon/qilin", True),
])
def test_live_enforcer_and_offline_audit_agree_on_sharded_link_scope(
        tmp_path, monkeypatch, physical_paths, target, unresolved):
    """One real vault fixture proves both gates agree, including negative space."""
    wiki = tmp_path / "wiki"
    source = wiki / "sources" / "compiled.md"
    source.parent.mkdir(parents=True)
    body = ("Grounded source content with a canonical link. " * 3
            + f"See [[{target}]].\n")
    source.write_text("---\ntype: source\nproducer_lane: compile\nraw: raw/compiled.md\n"
                      "publisher: Example\n---\n" + body, encoding="utf-8")
    for physical in physical_paths:
        page = wiki / physical
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntype: entity\n---\n# Qilin\n", encoding="utf-8")
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{
        "name": "compile", "enabled_toolsets": ["okengine-write"],
        "output_contract": contract(), "audit_markers": ["raw"],
    }]}), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
    live = m._link_module()
    live._cache.update(key=None, jobs={})
    findings = live.evaluate({"kind": "job", "actor": "cron:compile"},
                            operation="create", namespace="sources", page_type="source",
                            frontmatter={"type": "source", "raw": "raw/compiled.md",
                                         "publisher": "Example"},
                            body=body, unknown_fields=[], wiki=wiki)
    live_unresolved = any(f["code"] == "unresolved_link" for f in findings)
    report = m.audit(tmp_path, jobs)
    audit_unresolved = any(f.get("path") == "sources/compiled.md"
                           and f["reason"] == "unresolved_link" for f in report["findings"])
    assert live_unresolved == unresolved, findings
    assert audit_unresolved == live_unresolved, report["findings"]


def test_staged_audit_imports_the_baked_live_enforcer(monkeypatch):
    """In a deployed gateway the source-tree path is absent; use its wheel."""
    live = m._link_module()
    monkeypatch.setattr(m, "__file__", "/opt/data/scripts/model_write_audit.py")
    monkeypatch.setitem(sys.modules, "output_contract_enforce", live)
    assert m._link_module() is live


def test_page_parser_errors_and_class_level_unattributed_findings(tmp_path, monkeypatch):
    plain = tmp_path / "plain.md"
    plain.write_text("plain")
    assert m._page(plain) is None
    plain.write_text("---\n[\n---\n")
    assert m._page(plain) is None
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m._page(plain) is None

    monkeypatch.undo()
    wiki = tmp_path / "wiki" / "sources"
    wiki.mkdir(parents=True)
    empty = wiki / "empty.md"
    empty.write_text("---\ntype: source\n---\n")
    placeholder = wiki / "placeholder.md"
    placeholder.write_text("---\ntype: source\n---\n[More](#)\n")
    jobs = tmp_path / "jobs.json"
    wildcard = contract()
    wildcard["allowed_namespaces"] = ["*"]
    wildcard["allowed_types"] = ["*"]
    jobs.write_text(json.dumps([{
        "name": "one", "output_contract": wildcard, "audit_markers": ["raw"],
    }, {
        "name": "two", "output_contract": wildcard, "audit_markers": ["publisher"],
    }]))
    report = m.audit(tmp_path, jobs)
    reasons = {x["reason"] for x in report["findings"]}
    assert {"class_empty_body", "class_placeholder_link"} <= reasons
    assert all(x.get("lane") == "unattributed" for x in report["findings"])


def test_candidate_attribution_body_required_relationships_and_ambiguous_links(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "sources" / "candidate.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: source\nraw: raw/x\npublisher: P\n---\n"
        "[[entities/dup]] [[entities/dup]]\n"
    )
    for shard in ("a", "b"):
        target = wiki / "entities" / shard / "dup.md"
        target.parent.mkdir(parents=True)
        target.write_text("---\ntype: entity\n---\nbody")
    jobs = tmp_path / "jobs.json"
    c = contract()
    c["body"] = {"required": True, "min_non_whitespace": 0}
    c["required_relationships"] = ["related"]
    jobs.write_text(json.dumps({"jobs": [{
        "name": "candidate", "output_contract": c, "audit_markers": ["raw"],
    }]}))
    report = m.audit(tmp_path, jobs)
    assert {x["reason"] for x in report["findings"]} == {
        "unresolved_link", "required_relationship_missing"
    }
    unresolved = next(x for x in report["findings"] if x["reason"] == "unresolved_link")
    assert unresolved["detail"] == "entities/dup"

    page.write_text("---\ntype: source\nraw: raw/x\npublisher: P\nrelated: one\n---\n")
    report = m.audit(tmp_path, jobs)
    assert any(x["reason"] == "body_required" for x in report["findings"])


@pytest.mark.parametrize("field", ["required_relationships", "optional_relationships"])
def test_live_enforcer_and_offline_audit_reject_dangling_relationship(tmp_path, monkeypatch, field):
    wiki = tmp_path / "wiki"
    page = wiki / "sources" / "relationship.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: source\nproducer_lane: compile\nraw: raw/x\npublisher: P\n"
        "related: '[[entities/missing|Missing]]'\n---\n" + "Grounded body. " * 8,
        encoding="utf-8",
    )
    c = contract()
    c["required_relationships"] = []
    c["optional_relationships"] = []
    c[field] = ["related"]
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [{
        "name": "compile", "enabled_toolsets": ["okengine-write"],
        "output_contract": c, "audit_markers": ["raw"],
    }]}), encoding="utf-8")
    monkeypatch.setenv("OKENGINE_CRON_JOBS", str(jobs))
    live = m._link_module()
    live._cache.update(key=None, jobs={})

    live_findings = live.evaluate(
        {"kind": "job", "actor": "cron:compile"}, operation="create",
        namespace="sources", page_type="source",
        frontmatter={"type": "source", "raw": "raw/x", "publisher": "P",
                     "related": "[[entities/missing|Missing]]"},
        body="Grounded body. " * 8, unknown_fields=[], wiki=wiki,
    )
    report = m.audit(tmp_path, jobs)

    assert any(item["code"] == "relationship_unresolved" for item in live_findings)
    assert any(item.get("path") == "sources/relationship.md"
               and item["reason"] == "relationship_unresolved"
               for item in report["findings"])


def test_readiness_missing_fresh_and_zero_selected():
    from datetime import datetime, timezone
    assert m.readiness_alerts({}, {}, now=datetime(2026, 1, 1, tzinfo=timezone.utc)) == [
        {"reason": "audit_missing"}
    ]
    assert m.readiness_alerts(
        {"last_successful_audit": "2026-01-01T00:00:00+00:00"},
        {"selected": 0, "accepted": 0, "undisposed": 0},
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ) == []


def test_repair_plan_skips_lane_and_quarantines_other_page_reasons():
    report = {"findings": [
        {"scope": "lane", "reason": "contract_missing"},
        {"scope": "page", "path": "x", "sha256": "sha256:x", "version": None,
         "reason": "placeholder_link"},
    ]}
    assert m.repair_plan(report)["actions"][0]["action"] == "quarantine-for-review"


def test_main_stdout_files_plan_and_entrypoint(tmp_path, monkeypatch, capsys):
    jobs = tmp_path / "jobs.json"
    jobs.write_text("[]")
    monkeypatch.setattr(sys, "argv", ["model_write_audit", str(tmp_path), str(jobs)])
    assert m.main() == 0
    assert json.loads(capsys.readouterr().out)["api"] == 1

    output, plan = tmp_path / "out.json", tmp_path / "plan.json"
    monkeypatch.setattr(sys, "argv", [
        "model_write_audit", str(tmp_path), str(jobs),
        "--output", str(output), "--plan", str(plan),
    ])
    assert m.main() == 0 and output.is_file() and plan.is_file()

    monkeypatch.setattr(sys, "argv", [str(MOD), str(tmp_path), str(jobs)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(MOD), run_name="__main__")
    assert exc.value.code == 0


def test_audit_invalid_contract_index_page_and_digest_read_races(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki/sources"; wiki.mkdir(parents=True)
    plain = wiki / "plain.md"; plain.write_text("not frontmatter")
    empty = wiki / "empty.md"; empty.write_text("---\ntype: source\n---\n")
    placeholder = wiki / "placeholder.md"
    placeholder.write_text("---\ntype: source\n---\n[More](#)\n")
    attributed = wiki / "attributed.md"
    attributed.write_text(
        "---\ntype: source\nproducer_lane: lane\n---\nshort body\n")
    jobs = tmp_path / "jobs.json"
    relaxed = contract(); relaxed["unresolved_links"] = "allow"
    invalid = contract(); invalid["bad"] = True
    jobs.write_text(json.dumps({"jobs": [
        {"name": "invalid", "output_contract": invalid},
        {"name": "lane", "output_contract": relaxed},
    ]}))
    monkeypatch.setattr(m, "_contract_module", lambda: SimpleNamespace(
        validate=lambda value, _ctx: ["invalid contract"] if value.get("bad") else []))
    original_relative = Path.relative_to
    original_bytes = Path.read_bytes
    relative_calls = {plain: 0}

    def relative(path, other, *args):
        if path == plain and relative_calls[path] == 0:
            relative_calls[path] += 1
            raise ValueError("index race")
        return original_relative(path, other, *args)

    def unreadable(path):
        if path in {empty, placeholder, attributed}:
            raise OSError("vanished")
        return original_bytes(path)

    monkeypatch.setattr(Path, "relative_to", relative)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    report = m.audit(tmp_path, jobs)
    assert any(f["reason"] == "contract_invalid" for f in report["findings"])
    assert not any(f.get("path") in {"sources/empty.md", "sources/placeholder.md",
                                     "sources/attributed.md"}
                   for f in report["findings"])
