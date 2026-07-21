"""Cockpit read queue + protected review proxy contract (#256)."""
import importlib.util
import json
import sys
import warnings
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
# Starlette 1.x emits its transition warning at import time while FastAPI's supported
# test client still resolves through this module. Keep product deprecations visible; suppress
# only this upstream test-harness migration notice until FastAPI switches its client backend.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`.*")
    from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit" / "app.py"


def _load(tmp_path, monkeypatch, preserve_review=False):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    monkeypatch.delenv("OKENGINE_READER_PASSWORD", raising=False)
    if not preserve_review:
        monkeypatch.delenv("OKENGINE_REVIEW_API", raising=False)
        monkeypatch.delenv("OKENGINE_REVIEW_TOKEN", raising=False)
        monkeypatch.delenv("OKENGINE_REVIEWER_NAME", raising=False)
        monkeypatch.delenv("OKENGINE_REVIEW_TRUSTED_NETWORK", raising=False)
    sys.path.insert(0, str(APP.parent))
    spec = importlib.util.spec_from_file_location("cockpit_review_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def review_app(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "s").mkdir(parents=True)
    (wiki / "sources" / "2026").mkdir(parents=True)
    tmp_path.joinpath("schema.yaml").write_text("cockpit:\n  title: Test\n  tabs: [browse]\n", encoding="utf-8")
    refs = [f"sources/2026/source-{i}" for i in range(6)]
    for i, ref in enumerate(refs):
        (wiki / f"{ref}.md").write_text(
            f"---\ntype: source\nid: source-{i}\ntitle: Source {i}\nreliability: A\npublished: 2026-07-{i+1:02d}\n---\nBody.\n",
            encoding="utf-8")
    (wiki / "entities" / "s" / "shinyhunters.md").write_text(
        "---\ntype: actor\nid: shinyhunters\ntitle: ShinyHunters\nversion: 12\n"
        "last_updated: 2026-07-16T17:21:45Z\nneeds_review: true\n"
        "sources:\n" + "".join(f"- {r}\n" for r in refs) + "- MISP galaxy\n---\nSubstantive claim.\n",
        encoding="utf-8")
    return _load(tmp_path, monkeypatch), tmp_path


def test_review_detail_exposes_all_evidence_and_unresolved_values(review_app):
    m, _ = review_app
    d = m.api_review("entities/s/shinyhunters")
    assert d["version"] == 12 and len(d["hash"]) == 64
    assert d["evidence_total"] == 7 and d["evidence_resolved"] == 6
    assert [e["name"] for e in d["evidence"] if not e["page"]] == ["MISP galaxy"]
    assert d["reasons"][0]["code"] == "legacy-unspecified"
    assert d["decision_context"]["noun"] == "record"
    assert "does not prove the opposite" in d["decision_context"]["reject"]


def test_review_detail_exposes_assessment_specific_decision_semantics(tmp_path, monkeypatch):
    page = tmp_path / "wiki" / "assessments" / "identity" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: assessment\ntitle: Identity review\nversion: 2\nneeds_review: true\n"
        "question: Are A and B aliases?\nclaim: Reporting treats A and B as aliases.\n"
        "review_proposition: Reporting treats A and B as labels for one tracked identity.\n"
        "review_scope: Decide the reporting-backed label mapping only.\n"
        "review_approve_meaning: Evidence supports the bounded mapping.\n"
        "review_reject_meaning: Evidence does not support the mapping; distinctness is not proven.\n"
        "review_change_meaning: Correct labels or evidence.\n---\nBody.\n", encoding="utf-8")
    tmp_path.joinpath("schema.yaml").write_text("cockpit: {tabs: [browse]}\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    dc = m.api_review("assessments/identity/x")["decision_context"]
    assert dc == {
        "question": "Are A and B aliases?",
        "proposition": "Reporting treats A and B as labels for one tracked identity.",
        "scope": "Decide the reporting-backed label mapping only.",
        "approve": "Evidence supports the bounded mapping.",
        "reject": "Evidence does not support the mapping; distinctness is not proven.",
        "request_changes": "Correct labels or evidence.",
        "defer": "More evidence or analysis is required before deciding.",
        "dismiss": "This review item is duplicate, out of scope, or not applicable.",
        "noun": "assessment",
    }


def test_review_ui_explains_each_decision_before_controls():
    js = (REPO / "okengine-cockpit" / "static" / "app.js").read_text()
    assert "Decision to make" in js and "Approve ${esc(noun)}" in js
    assert js.index("decisionContext +") < js.index("Why this needs review")


def test_review_queue_is_complete_paginated_and_filterable(review_app):
    m, root = review_app
    for i in range(3):
        (root / "wiki" / "entities" / "s" / f"x-{i}.md").write_text(
            f"---\ntype: actor\nid: x-{i}\ntitle: X {i}\nneeds_review: true\n---\nBody.\n", encoding="utf-8")
    first = m.api_reviews(offset=0, limit=2, reason="", page_type="actor", state="open")
    second = m.api_reviews(offset=2, limit=2, reason="", page_type="actor", state="open")
    assert first["total"] == 4 and len(first["items"]) == 2 and len(second["items"]) == 2
    assert {r["subject"] for r in first["items"]}.isdisjoint({r["subject"] for r in second["items"]})


def test_review_queue_supports_pack_scoped_type_sets(review_app):
    m, root = review_app
    predictions = root / "wiki" / "predictions"
    predictions.mkdir()
    predictions.joinpath("p.md").write_text(
        "---\ntype: prediction\ntitle: P\nneeds_review: true\n---\nClaim.\n", encoding="utf-8")
    result = m.api_reviews(offset=0, limit=50, reason="", page_type="", state="",
                           page_types="actor,prediction")
    assert result["total"] == 2
    assert set(result["facets"]["types"]) == {"actor", "prediction"}


def test_declarative_review_queue_box_links_to_scoped_worklist(tmp_path, monkeypatch):
    page = tmp_path / "wiki" / "assessments" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: assessment\ntitle: A\nneeds_review: true\n---\nClaim.\n",
                    encoding="utf-8")
    tmp_path.joinpath("schema.yaml").write_text(
        "cockpit:\n  tabs: [che]\n  tab_defs:\n    che:\n      label: CHE\n      boxes:\n"
        "        - title: Awaiting CHE review\n          view: review-queue\n"
        "          review_types: [assessment, prediction, analytic-hypothesis]\n"
        "          review_dirs: [assessments, predictions]\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_review_queue_snapshot", lambda: (_ for _ in ()).throw(
        AssertionError("scoped review launcher must not scan the whole vault")))
    box = m.api_tab("che")["boxes"][0]
    assert "1 awaiting review" in box["html"]
    assert 'data-review-types="assessment,prediction,analytic-hypothesis"' in box["html"]


def test_public_cockpit_has_no_review_mutation_capability(review_app):
    m, _ = review_app
    assert m.api_config()["review_enabled"] is False
    client = TestClient(m.app)
    response = client.post("/api/review/decision", headers={"X-OKEngine-Review": "1"}, json={})
    assert response.status_code == 404


def test_trusted_network_review_is_explicit_and_passwordless(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("cockpit: {tabs: [browse]}\n", encoding="utf-8")
    monkeypatch.setenv("OKENGINE_REVIEW_API", "http://review-write:8731")
    monkeypatch.setenv("OKENGINE_REVIEW_TOKEN", "secret")
    monkeypatch.setenv("OKENGINE_REVIEWER_NAME", "analyst:test")
    monkeypatch.setenv("OKENGINE_REVIEW_TRUSTED_NETWORK", "1")
    m = _load(tmp_path, monkeypatch, preserve_review=True)
    assert m._READER_PASSWORD == ""
    assert m.api_config()["review_enabled"] is True
    assert m.api_config()["review_auth_mode"] == "trusted-network"


def test_trusted_network_review_requires_explicit_reviewer(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("cockpit: {tabs: [browse]}\n", encoding="utf-8")
    monkeypatch.setenv("OKENGINE_REVIEW_API", "http://review-write:8731")
    monkeypatch.setenv("OKENGINE_REVIEW_TOKEN", "secret")
    monkeypatch.delenv("OKENGINE_REVIEWER_NAME", raising=False)
    monkeypatch.setenv("OKENGINE_REVIEW_TRUSTED_NETWORK", "1")
    m = _load(tmp_path, monkeypatch, preserve_review=True)
    assert m.api_config()["review_enabled"] is False


def test_protected_proxy_injects_server_side_reviewer(review_app, monkeypatch):
    m, _ = review_app
    m._REVIEW_ENABLED = True; m._REVIEW_API = "http://review-write:8731"; m._REVIEW_TOKEN = "secret"
    m._REVIEWER = "Jane"
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return json.dumps({"ok": True, "state": "approved"}).encode()

    def fake_open(req, timeout=0):
        captured["auth"] = req.headers["Authorization"]
        captured["payload"] = json.loads(req.data)
        return Response()

    monkeypatch.setattr(m.urllib.request, "urlopen", fake_open)
    client = TestClient(m.app)
    response = client.post("/api/review/decision", headers={"X-OKEngine-Review": "1"}, json={
        "path": "entities/s/shinyhunters", "decision": "approve", "note": "",
        "expected_version": 12, "expected_hash": "a" * 64})
    assert response.status_code == 200 and response.json()["state"] == "approved"
    assert captured["auth"] == "Bearer secret"
    assert captured["payload"]["reviewer"] == "Jane" and captured["payload"]["service"] == "cockpit"


def test_review_required_type_appears_without_legacy_flag(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki" / "predictions"
    wiki.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "review_required_types: [prediction]\ncockpit: {tabs: [predictions]}\n", encoding="utf-8")
    (wiki / "p.md").write_text(
        "---\ntype: prediction\ntitle: P\nversion: 1\n---\nClaim.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    result = m.api_reviews(offset=0, limit=50, reason="", page_type="", state="")
    assert result["total"] == 1
    assert result["items"][0]["reasons"][0]["code"] == "import-unvetted"


def test_actor_page_surfaces_subject_assessments_above_quarantine(tmp_path, monkeypatch):
    actor = tmp_path / "wiki" / "entities" / "a" / "apt39.md"
    actor.parent.mkdir(parents=True)
    actor.write_text("---\ntype: actor\ntitle: APT39\nneeds_review: true\n---\nClaim.\n",
                     encoding="utf-8")
    assessment = tmp_path / "wiki" / "assessments" / "a" / "apt39-iran.md"
    assessment.parent.mkdir(parents=True)
    assessment.write_text(
        "---\ntype: assessment\ntitle: APT39 — Iran association\n"
        "subject: entities/a/apt39\nclaim: Reporting associates APT39 with Iran.\n"
        "status: active\nconfidence: 0.85\nconfidence_band: high\nas_of: 2026-07-16\n"
        "needs_review: true\n---\nAnalysis.\n", encoding="utf-8")
    superseded = tmp_path / "wiki" / "assessments" / "a" / "apt39-old.md"
    superseded.write_text(
        "---\ntype: assessment\ntitle: Obsolete APT39 judgment\n"
        "subject: entities/a/apt39\nclaim: Obsolete claim.\n"
        "status: superseded\nconfidence: 0.65\nas_of: 2026-07-15\n"
        "needs_review: true\n---\nRetained audit history.\n", encoding="utf-8")
    (tmp_path / "schema.yaml").write_text("cockpit: {tabs: [browse]}\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    page = m.api_page("entities/a/apt39")
    assert page["trust"]["state"] == "quarantined"
    assert page["assessments"] == [{
        "path": "assessments/a/apt39-iran", "title": "APT39 — Iran association",
        "claim": "Reporting associates APT39 with Iran.", "status": "active",
        "assessment_kind": "", "assessed_value": None, "assessed_label": "",
        "epistemic_status": "assessed",
        "confidence": 0.85, "confidence_band": "high", "as_of": "2026-07-16",
        "last_updated": "2026-07-16", "needs_review": True,
        "reviewed_by": "", "reviewed_on": "",
    }]
    js = (REPO / "okengine-cockpit" / "static" / "app.js").read_text()
    assert js.index("assessmentPanel(d.assessments)") < js.index("trustGatedHtml(d.trust")


def test_normal_review_required_pages_have_a_review_affordance():
    js = (REPO / "okengine-cockpit" / "static" / "app.js").read_text()
    assert "function reviewAffordance(d)" in js
    assert "d.provenance?.needs_review" in js
    assert js.index("reviewAffordance(d)") < js.index("trustGatedHtml(d.trust, profile")


# ── review-snapshot latency contract (2026-07-19 UI sweep) ───────────────────────────────────

def _mk_review_page(wiki: Path, name: str):
    (wiki / "entities" / "s").mkdir(parents=True, exist_ok=True)
    (wiki / "entities" / "s" / f"{name}.md").write_text(
        "---\ntype: actor\ntitle: T\nneeds_review: true\n---\nbody\n", encoding="utf-8")


def test_review_snapshot_serves_stale_and_refreshes_off_request_path(tmp_path, monkeypatch):
    """The full-vault worklist build takes tens of seconds on a real vault. An EXPIRED cache must
    serve the previous snapshot immediately and rebuild in the background — never block the tab
    request (the 24s Detections/CHE 'Loading…' hang). Only a process that has NEVER built one may
    block."""
    import threading as _threading
    import time as _time
    wiki = tmp_path / "wiki"
    _mk_review_page(wiki, "one")
    tmp_path.joinpath("schema.yaml").write_text("cockpit:\n  title: T\n  tabs: [browse]\n",
                                                encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    rows, _ = m._review_queue_snapshot()              # first build: synchronous by design
    assert [r["subject"] for r in rows] == ["entities/s/one"]

    # expire the cache, add a page, and make the rebuild OBSERVABLY slow
    _mk_review_page(wiki, "two")
    built = _threading.Event()
    real_build = m._build_review_snapshot

    def slow_build():
        _time.sleep(0.3)
        out = real_build()
        built.set()
        return out
    m._build_review_snapshot = slow_build
    m._review_snapshot_cache = (m.time.monotonic() - m._REVIEW_SNAPSHOT_TTL - 1,
                                rows, [])
    t0 = _time.monotonic()
    stale_rows, _ = m._review_queue_snapshot()
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.25, f"expired-cache read blocked on the rebuild ({elapsed:.2f}s)"
    assert [r["subject"] for r in stale_rows] == ["entities/s/one"], "must serve the stale snapshot"
    assert built.wait(5), "background rebuild never ran"
    for _ in range(50):                                # swap is async; poll briefly
        fresh_rows, _ = m._review_queue_snapshot()
        if len(fresh_rows) == 2:
            break
        _time.sleep(0.05)
    assert {r["subject"] for r in fresh_rows} == {"entities/s/one", "entities/s/two"}


def test_review_snapshot_invalidate_forces_synchronous_fresh_read(tmp_path, monkeypatch):
    """After an operator review action, _invalidate_review_snapshot() must make the NEXT read
    reflect the vault immediately (serve-stale would show the pre-action state)."""
    wiki = tmp_path / "wiki"
    _mk_review_page(wiki, "one")
    tmp_path.joinpath("schema.yaml").write_text("cockpit:\n  title: T\n  tabs: [browse]\n",
                                                encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert len(m._review_queue_snapshot()[0]) == 1
    _mk_review_page(wiki, "two")
    m._invalidate_review_snapshot()
    assert len(m._review_queue_snapshot()[0]) == 2


def test_doc_view_caps_giant_documents(tmp_path, monkeypatch):
    """A generated dashboard can grow without bound (okcti's 500KB adversarial review became a
    650KB HTML panel). A doc BOX inlines at most _DOC_INLINE_CAP and links to the full page."""
    wiki = tmp_path / "wiki"
    (wiki / "dashboards").mkdir(parents=True)
    big = "# Big\n\n" + ("lorem ipsum paragraph line\n" * 40000)     # ~1MB
    (wiki / "dashboards" / "giant.md").write_text("---\ntitle: G\n---\n" + big, encoding="utf-8")
    (wiki / "dashboards" / "small.md").write_text("---\ntitle: S\n---\n# S\nshort\n",
                                                  encoding="utf-8")
    tmp_path.joinpath("schema.yaml").write_text("cockpit:\n  title: T\n  tabs: [browse]\n",
                                                encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html, _stem = m._v_doc({"dir": "dashboards", "glob": "giant.md"})
    assert len(html) < 2 * m._DOC_INLINE_CAP, "giant doc rendered uncapped"
    assert "Document truncated for inline view" in html
    assert 'data-page="dashboards/giant"' in html                   # full page one click away
    small, _ = m._v_doc({"dir": "dashboards", "glob": "small.md"})
    assert "truncated" not in small                                  # small docs untouched
