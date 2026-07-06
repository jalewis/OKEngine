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
