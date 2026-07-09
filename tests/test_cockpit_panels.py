"""Cockpit UI extension panels (okengine#160, ported from the reader): /api/page must
carry a render-ready `panel` — a GENERATED page's self-declared `panel:` frontmatter
(e.g. viz's two-axis wardley map, nodes included) passes through verbatim; a type bound
in VAULT/.okengine/reader-panels.json yields a `fields` panel; plain pages get None.
Regression for the wardley-maps-render-in-the-reader-but-not-the-cockpit gap."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")
pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "okengine-cockpit" / "app.py"


def _load(vault, monkeypatch):
    monkeypatch.setenv("VAULT_DIR", str(vault))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def test_self_declared_panel_passes_through_api_page(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki" / "dashboards"
    wiki.mkdir(parents=True)
    (wiki / "wardley.md").write_text(
        "---\n"
        "type: dashboard\n"
        "title: Wardley map\n"
        "panel:\n"
        "  kind: two-axis\n"
        "  x_label: Evolution\n"
        "  y_label: Visibility\n"
        "  nodes:\n"
        "    - {label: Alpha, slug: alpha, x: 0.2, y: 0.9}\n"
        "    - {label: Beta, slug: beta, x: 0.8, y: 0.3}\n"
        "---\n# Map\n",
        encoding="utf-8",
    )
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="dashboards/wardley")
    assert d["panel"]["kind"] == "two-axis"
    assert [n["label"] for n in d["panel"]["nodes"]] == ["Alpha", "Beta"]
    assert d["panel"]["x_label"] == "Evolution"


def test_type_bound_fields_panel_from_staged_bindings(tmp_path, monkeypatch):
    (tmp_path / ".okengine").mkdir(parents=True)
    (tmp_path / ".okengine" / "reader-panels.json").write_text(
        json.dumps({"vendor": {"kind": "fields", "title": "Vendor",
                               "fields": ["tier", "hq"]}}),
        encoding="utf-8",
    )
    m = _load(tmp_path, monkeypatch)
    panel = m._panel_for({"type": "vendor", "tier": "leader", "hq": "Austin"})
    assert panel == {"kind": "fields", "title": "Vendor",
                     "items": [{"label": "tier", "value": "leader"},
                               {"label": "hq", "value": "Austin"}]}


def test_plain_page_gets_no_panel(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki" / "concepts"
    wiki.mkdir(parents=True)
    (wiki / "plain.md").write_text("---\ntype: concept\ntitle: Plain\n---\nbody\n",
                                   encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="concepts/plain")
    assert d["panel"] is None


def test_html_table_dates_and_numbers_get_num_class(tmp_path, monkeypatch):
    """Operator report: a bare date/number cell in a `_html_table` (e.g. a prediction's 'Resolves
    by') is tagged `.num` so it never wraps/breaks; a text or HTML cell stays a normal wrapping
    cell. Guards the ledger-date-wrap fix."""
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    html = m._html_table(
        ["Prediction", "Conf", "Resolves by"],
        [['<a class="wl">Prediction: X</a>', "moderate-high", "2026-09-30"]])
    assert '<td class="num">2026-09-30</td>' in html            # date -> nowrap
    assert '<td>moderate-high</td>' in html                      # status text -> normal wrap
    assert '<td><a class="wl">Prediction: X</a></td>' in html    # HTML cell -> normal
    # numbers too
    h2 = m._html_table(["a", "b"], [["x", "1,234"], ["y", "—"]])
    assert '<td class="num">1,234</td>' in h2 and '<td class="num">—</td>' in h2


def test_prediction_files_recurse_into_dated_partition(tmp_path, monkeypatch):
    """Operator report: candidate-watch writes predictions into a resolution-quarter partition
    (predictions/YYYY/qN/predict-*.md); a flat `predictions/*.md` glob found ZERO and the
    Open-predictions view went empty. The scan must recurse into the dated subdirs."""
    pred = tmp_path / "wiki" / "predictions" / "2026" / "q3"
    pred.mkdir(parents=True)
    (pred / "predict-x.md").write_text(
        "---\ntype: prediction\nsubject: X pattern expands\nstatus: open\n"
        "resolves_by: '2026-12-15'\n---\nbody\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert any("predict-x.md" in f for f in m._prediction_files()), "dated-partition prediction not found"
    rows = m._load_predictions()
    assert any(r.get("subject") == "X pattern expands" for r in rows), f"not loaded: {rows}"


def test_prediction_detail_resolves_nested_partition(tmp_path, monkeypatch):
    """UI feedback #1: prediction rows are discovered recursively (predictions/YYYY/qN/…), but the
    detail endpoint looked up a FLAT predictions/<id>.md — so a partitioned prediction appeared in
    the ledger then 404'd on click. api_prediction must resolve the nested page by id."""
    p = tmp_path / "wiki" / "predictions" / "2026" / "q3"
    p.mkdir(parents=True)
    (p / "predict-widget-adoption.md").write_text(
        "---\ntype: prediction\nstatus: open\nconfidence: 0.6\nsubject: Widgets\n"
        "resolves_by: 2026-12-31\n---\nWidgets will ship.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_prediction(id="predict-widget-adoption")           # nested id, no slash
    assert d["id"] == "predict-widget-adoption"
    assert "Widgets" in (d.get("claim") or "") or d["fm"].get("subject") == "Widgets"


def test_dashboard_autolist_includes_nested_namespaces(tmp_path, monkeypatch):
    """UI feedback #2: dashboard auto-discovery used a flat dashboards/*.md glob, so nested
    extension dashboards (dashboards/<ns>/*.md) were invisible unless a pack curated them. The
    auto-list must recurse and keep the sub-namespace in the path."""
    base = tmp_path / "wiki" / "dashboards"
    (base / "competitive").mkdir(parents=True)
    (base / "top.md").write_text("---\ntype: dashboard\ntitle: Top\n---\nx\n", encoding="utf-8")
    (base / "competitive" / "quadrants.md").write_text(
        "---\ntype: dashboard\ntitle: Quadrants\n---\nx\n", encoding="utf-8")
    (base / "_scaffold.md").write_text("---\ntype: dashboard\n---\nx\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    groups = m.api_dashboards()["groups"]
    paths = {it["path"] for g in groups for it in g["items"]}
    assert "dashboards/competitive/quadrants" in paths   # nested now visible
    assert "dashboards/top" in paths
    assert "dashboards/_scaffold" not in paths            # scaffold still skipped


def test_provenance_strip_surfaces_trust_fields(tmp_path, monkeypatch):
    """Provenance/trust strip (ported from the reader): api_page carries a `provenance` dict that
    answers 'can I trust this?' from fields the trust lanes + write path stamp — source coverage,
    grounding tally, review sign-off, tlp/sensitivity, reliability/credibility, composition
    provenance. A plain page with no trust signals gets {} (no empty strip)."""
    d = tmp_path / "wiki" / "entities" / "a"
    d.mkdir(parents=True)
    (d / "apt-x.md").write_text(
        "---\ntype: entity\nname: APT-X\n"
        "sources: [sources/2026/s1, 'https://ex.com/report']\n"
        "needs_review: true\ntlp: AMBER\nreviewed_by: analyst\nreviewed_on: 2026-07-01\n"
        "reliability: A\ncredibility: '2'\nmaintained_by: [okpack-cti]\ndiscovered_by: okpack-cti\n"
        "---\nBody.\n\n## Grounding check\n\n- claim 1 - **supported**\n- claim 2 - **unsupported**\n",
        encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    p = m.api_page(path="entities/a/apt-x")["provenance"]
    assert p["sources"] == 2 and p["source_pages"] == 1          # one source PAGE, one external URL
    assert p["needs_review"] is True and p["tlp"] == "AMBER"
    assert p["reviewed_by"] == "analyst" and p["reviewed_on"] == "2026-07-01"
    assert p["reliability"] == "A" and p["credibility"] == "2"
    assert p["maintained_by"] == "okpack-cti" and p["discovered_by"] == "okpack-cti"
    assert p["grounding"] == {"supported": 1, "unsupported": 1}
    (d / "plain.md").write_text("---\ntype: entity\nname: Plain\n---\njust text\n", encoding="utf-8")
    assert m.api_page(path="entities/a/plain")["provenance"] == {}   # no signals -> no strip


def test_api_page_route_serves_provenance_over_http(tmp_path, monkeypatch):
    """Routing regression: /api/page must resolve to api_page (not a helper). A decorator that
    slipped onto _provenance served the wrong signature -> HTTP 422, while the direct-call unit
    test still passed. Exercise the real route with a client so it can't regress silently."""
    from starlette.testclient import TestClient
    d = tmp_path / "wiki" / "entities" / "a"
    d.mkdir(parents=True)
    (d / "apt-x.md").write_text(
        "---\ntype: entity\nname: APT-X\ntlp: AMBER\nneeds_review: true\n---\nBody.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    r = TestClient(m.app).get("/api/page", params={"path": "entities/a/apt-x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "APT-X"
    assert body["provenance"]["tlp"] == "AMBER" and body["provenance"]["needs_review"] is True


def test_evidence_entries_parse_string_and_dict_shapes(tmp_path, monkeypatch):
    """UI feature #7: evidence arrives as `[date tag] note` strings (regrade lanes) OR dicts.
    _evidence_entries normalizes both, buckets the direction, keeps confidence moves + source,
    and sorts oldest→newest."""
    m = _load(tmp_path, monkeypatch)
    ent = m._evidence_entries({"evidence": [
        {"date": "2026-07-01", "direction": "contradicts", "note": "Countersignal",
         "confidence_before": 0.6, "confidence_after": 0.5, "source": "https://ex.com/r"},
        "[2026-06-01 regrade] Vendor report reinforces the pattern.",
        "plain note with no bracket",
    ]})
    assert [e["date"] for e in ent] == [None, "2026-06-01", "2026-07-01"]   # sorted; None first
    reg = next(e for e in ent if e["date"] == "2026-06-01")
    assert reg["tag"] == "regrade" and reg["direction"] == "neutral"
    assert reg["note"] == "Vendor report reinforces the pattern."
    con = next(e for e in ent if e["date"] == "2026-07-01")
    assert con["direction"] == "contradicts" and con["confidence_after"] == 0.5
    assert con["source"] == "https://ex.com/r"
    assert m._evidence_entries({"evidence": "not a list"}) == []


def test_prediction_evidence_drilldown_and_string_tally(tmp_path, monkeypatch):
    """UI feature #7 (+ nested-bug pairing): api_prediction exposes the evidence log, and the ledger
    tally counts STRING-shaped regrade entries — the old dict-only tally scored them zero, wrongly
    flagging an evidenced open prediction (made >60d ago) as idle."""
    p = tmp_path / "wiki" / "predictions" / "2026" / "q4"
    p.mkdir(parents=True)
    (p / "predict-alpha.md").write_text(
        "---\ntype: prediction\nstatus: open\nconfidence: 0.6\nsubject: Alpha\n"
        "made_on: 2026-01-01\nresolves_by: 2026-12-31\n"
        "evidence:\n"
        "  - '[2026-06-01 regrade] Vendor report reinforces the pattern.'\n"
        "  - {date: 2026-07-01, direction: contradicts, note: Countersignal}\n"
        "---\nAlpha will happen.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    r = next(x for x in m._load_predictions() if x["id"] == "predict-alpha")
    assert r["evidence_n"] == 2                       # string entry counted, not zero
    assert r["ev_dir"]["contradicts"] == 1
    assert r["idle"] is False                         # evidenced -> not idle despite old made_on
    d = m.api_prediction(id="predict-alpha")
    assert len(d["evidence"]) == 2
    assert d["evidence"][0]["note"].startswith("Vendor report")   # sorted oldest-first


def test_ops_tab_surfaces_operational_health_pages(tmp_path, monkeypatch):
    """UI feature #2: an engine-level Ops tab groups the health/audit pages the engine crons
    generate. Pages are grouped, only existing ones show, extras under operational/ aren't hidden,
    and /api/config auto-appends the tab when that content exists."""
    w = tmp_path / "wiki"
    (w / "dashboards").mkdir(parents=True)
    (w / "operational").mkdir(parents=True)
    (w / "dashboards" / "fleet-health.md").write_text(
        "---\ntitle: Fleet health\nsummary: uptime\n---\nx\n", encoding="utf-8")
    (w / "operational" / "schema-conformance.md").write_text(
        "---\ntitle: Conformance\n---\nx\n", encoding="utf-8")
    (w / "_review-queue.md").write_text("---\ntitle: Review queue\n---\nx\n", encoding="utf-8")
    for dt in ("2026-07-06", "2026-07-07", "2026-07-08"):                   # a daily series
        (w / "operational" / f"lint-watch-{dt}.md").write_text(
            "---\ntitle: Lint\n---\nx\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    by = {g["group"]: {it["path"] for it in g["items"]} for g in m.api_ops()["groups"]}
    assert "dashboards/fleet-health" in by["Health"]
    assert "operational/schema-conformance" in by["Conformance"]
    assert "_review-queue" in by["Review & grounding"]
    # daily series collapses to only its newest page (no page-per-day flood)
    assert "operational/lint-watch-2026-07-08" in by["Operational log"]
    assert "operational/lint-watch-2026-07-06" not in by["Operational log"]
    assert "operational/lint-watch-2026-07-07" not in by["Operational log"]
    assert "ops" in m.api_config()["tabs"]                                  # auto-appended


def test_ops_tab_absent_when_no_operational_pages(tmp_path, monkeypatch):
    """Ops is content-gated: a vault the engine health crons haven't populated shows no Ops tab
    (no empty nav entry)."""
    (tmp_path / "wiki" / "briefings").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    assert m.api_ops()["groups"] == []
    assert "ops" not in m.api_config()["tabs"]
