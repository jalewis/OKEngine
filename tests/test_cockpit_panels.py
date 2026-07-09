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
