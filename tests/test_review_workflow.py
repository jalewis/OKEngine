"""First-class review state machine and version-locked governed write contract (#256)."""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "okengine-mcp" / "write_server.py"


def _load():
    spec = importlib.util.spec_from_file_location("review_write_server", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def review_vault(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities" / "a").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\ntypes:\n  actor: {required: [type, title]}\n"
        "strict_types: false\npermissions:\n  default: {create: true, update: true, delete: false}\n",
        encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-17")
    monkeypatch.setenv("OKENGINE_MCP_WRITE_NOW", "2026-07-17T10:00:00Z")
    m = _load()
    page = tmp_path / "wiki" / "entities" / "a" / "apt-x.md"
    page.write_text(
        "---\ntype: actor\nid: apt-x\ntitle: APT-X\nversion: 4\nlast_updated: 2026-07-16T00:00:00Z\n"
        "needs_review: true\nsources: [sources/one, sources/two]\n---\n# APT-X\n\nClaim.\n",
        encoding="utf-8")
    return m, tmp_path, page


def _fm(m, page):
    return m._read_page(page)[0]


def test_approval_is_version_locked_and_audited(review_vault):
    m, root, page = review_vault
    _, _, _, version, digest = m._review_page_state(page)
    result = m._resolve_review("entities/a/apt-x", "approve", "Jane", "", version, digest)
    assert result["ok"] and result["state"] == "approved"
    fm = _fm(m, page)
    assert fm["needs_review"] is False and fm["reviewed_by"] == "Jane"
    assert fm["reviewed_version"] == 4 and fm["version"] == 5
    records = list((root / "wiki" / "operational" / "reviews").glob("*.yaml"))
    assert len(records) == 1
    rec = yaml.safe_load(records[0].read_text())
    assert rec["state"] == "approved" and rec["history"][-1]["decision_by"] == "Jane"
    assert "review approve entities/a/apt-x v4 by Jane" in (root / "wiki" / "log.md").read_text()


def test_stale_version_fails_without_writing(review_vault):
    m, root, page = review_vault
    before = page.read_text()
    result = m._resolve_review("entities/a/apt-x", "approve", "Jane", "", 3, "0" * 64)
    assert result["status"] == 409 and "changed" in result["error"]
    assert page.read_text() == before
    assert not (root / "wiki" / "log.md").exists()


@pytest.mark.parametrize("decision", ["request-changes", "reject", "dismiss", "defer"])
def test_nonapproval_dispositions_require_a_note(review_vault, decision):
    m, _, page = review_vault
    _, _, _, version, digest = m._review_page_state(page)
    result = m._resolve_review("entities/a/apt-x", decision, "Jane", "", version, digest)
    assert result["status"] == 400 and "requires a decision note" in result["error"]


def test_request_changes_retains_quarantine_and_note(review_vault):
    m, _, page = review_vault
    _, _, _, version, digest = m._review_page_state(page)
    result = m._resolve_review("entities/a/apt-x", "request-changes", "Jane",
                               "Alias is not supported.", version, digest)
    assert result["state"] == "changes-requested"
    fm = _fm(m, page)
    assert fm["needs_review"] is True and fm["review_state"] == "changes-requested"
    assert "reviewed_by" not in fm


def test_flagged_disposition_can_be_reassigned_at_projected_page_version(review_vault):
    m, _, page = review_vault
    _, _, _, version, digest = m._review_page_state(page)
    assert m._resolve_review("entities/a/apt-x", "defer", "Jane", "Revisit tomorrow",
                             version, digest)["ok"]
    _, _, _, projected_version, projected_digest = m._review_page_state(page)
    reassigned = m._assign_review("entities/a/apt-x", "John", projected_version, projected_digest)
    assert reassigned["ok"] and reassigned["assigned_to"] == "John"


def test_assignment_is_version_locked_and_does_not_mutate_page(review_vault):
    m, root, page = review_vault
    before = page.read_text()
    _, _, _, version, digest = m._review_page_state(page)
    result = m._assign_review("entities/a/apt-x", "Jane", version, digest)
    assert result["ok"] and result["state"] == "in-review" and result["assigned_to"] == "Jane"
    assert page.read_text() == before
    rec = yaml.safe_load(next((root / "wiki" / "operational" / "reviews").glob("*.yaml")).read_text())
    assert rec["assigned_to"] == "Jane" and rec["history"][-1]["action"] == "assign"


def test_machine_check_never_clears_human_review(review_vault):
    m, root, page = review_vault
    result = m._record_machine_review("entities/a/apt-x", "review-drain/v2", "supported", "local refs agree")
    assert result["ok"] and result["machine_check"]["outcome"] == "supported"
    assert _fm(m, page)["needs_review"] is True
    rec = yaml.safe_load(next((root / "wiki" / "operational" / "reviews").glob("*.yaml")).read_text())
    assert rec["state"] == "open" and not rec.get("decision_by")


def test_second_replace_failure_rolls_back_page_and_record(review_vault, monkeypatch):
    m, root, page = review_vault
    rec = m._ensure_review_request(page)
    rp = m._review_record_path(rec["review_id"])
    before_page, before_rec = page.read_text(), rp.read_text()
    _, _, _, version, digest = m._review_page_state(page)
    real_replace, calls = m.os.replace, 0

    def fail_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected record failure")
        return real_replace(src, dst)

    monkeypatch.setattr(m.os, "replace", fail_second)
    result = m._resolve_review("entities/a/apt-x", "approve", "Jane", "", version, digest)
    assert result["status"] == 500
    assert page.read_text() == before_page and rp.read_text() == before_rec


def test_write_path_flag_creates_structured_request(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "entities" / "a").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "okf: {required: [type]}\ntypes:\n  actor: {required: [type, title]}\nstrict_types: false\n"
        "permissions:\n  default: {create: true, update: true, delete: false}\n"
        "review:\n  confidence_field: confidence\n  confidence_review_values: [confirmed]\n",
        encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-07-17")
    m = _load()
    result = m._create("entities/a/apt-y", {"type": "actor", "id": "apt-y", "title": "APT-Y",
                                             "confidence": "confirmed"}, "Body.")
    assert "flagged for review" in result
    rec = yaml.safe_load(next((tmp_path / "wiki" / "operational" / "reviews").glob("*.yaml")).read_text())
    assert rec["reasons"][0]["code"] == "categorical-confidence"


def test_ordinary_update_cannot_forge_or_clear_review(review_vault):
    m, _, page = review_vault
    result = m._update("entities/a/apt-x", {
        "needs_review": False, "reviewed_by": "Forged", "reviewed_on": "2026-07-17",
        "review_state": "approved", "review_id": "forged",
    })
    assert result.startswith("updated")
    fm = _fm(m, page)
    assert fm["needs_review"] is True
    assert "reviewed_by" not in fm and "review_id" not in fm and "review_state" not in fm


def test_edit_after_approval_invalidates_projection_and_reopens(review_vault):
    m, root, page = review_vault
    _, _, _, version, digest = m._review_page_state(page)
    assert m._resolve_review("entities/a/apt-x", "approve", "Jane", "", version, digest)["ok"]
    assert m._update("entities/a/apt-x", body="# APT-X\n\nRevised claim.\n").startswith("updated")
    fm = _fm(m, page)
    assert fm["needs_review"] is True
    assert "reviewed_by" not in fm and "reviewed_version" not in fm
    records = [yaml.safe_load(p.read_text())
               for p in (root / "wiki" / "operational" / "reviews").glob("*.yaml")]
    assert len(records) == 2
    assert any(r["state"] == "open" and
               r["reasons"][0]["code"] == "changed-after-approval" for r in records)


def test_review_http_surface_requires_bearer_and_exposes_no_generic_write(review_vault):
    m, _, page = review_vault
    client = TestClient(m._ScopedWriteAuth(m._review_http_app(), "secret"))
    assert client.get("/healthz").status_code == 200
    assert client.post("/review/resolve", json={}).status_code == 401
    assert client.post("/entity/update", headers={"Authorization": "Bearer secret"}, json={}).status_code == 404
    _, _, _, version, digest = m._review_page_state(page)
    response = client.post("/review/resolve", headers={"Authorization": "Bearer secret"}, json={
        "path": "entities/a/apt-x", "decision": "approve", "reviewer": "Jane",
        "expected_version": version, "expected_hash": digest,
    })
    assert response.status_code == 200 and response.json()["state"] == "approved"
