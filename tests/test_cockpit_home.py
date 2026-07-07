"""Cockpit analyst-home tab (/api/home) — the flow: latest briefs → what moved →
open predictions → knowledge gaps → curated dashboards.

Built for the okcti gap where the cockpit was a set of disconnected tabs and a flat
all-page-links dashboard: an analyst needs one surface that leads through the day in
triage order. Contract: sections use the watchlist render shape ({group,title,html});
a surface that is EMPTY or unconfigured is OMITTED — the tab must never render a wall
of "none" placeholders on a young vault."""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
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


def _mk(root: Path, rel: str, fm: str, body: str = "b") -> None:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n{body}\n", encoding="utf-8")


def test_home_composes_live_surfaces_and_omits_empty(tmp_path, monkeypatch):
    # a vault with: one dated brief, a watchlist config + tiered entity, one open
    # prediction, one lacuna page, one curated dashboard — every section should appear
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n"
        "  streams:\n"
        "    - {key: briefs, label: Briefs, dir: briefings}\n"
        "  watchlist:\n"
        "    entity_types: [actor]\n"
        "    tier_field: activity_tier\n"
        "    moved_field: last_updated\n"
        "  dashboards: [top-actors-by-activity]\n"
        "  tabs: [home, briefings]\n", encoding="utf-8")
    _mk(tmp_path, "briefings/daily-2026-07-06.md", "type: briefing\ntitle: D\n")
    _mk(tmp_path, "entities/a/apt-x.md",
        "type: actor\nname: APT X\nactivity_tier: hot\nlast_updated: '2026-07-06T00:00:00Z'\n")
    _mk(tmp_path, "predictions/p1.md",
        "type: prediction\nstatus: open\nsubject: APT X pivots\nresolves_by: 2026-08-01\n")
    _mk(tmp_path, "lacuna/gap-1.md", "type: lacuna\nname: coverage gap\ncreated: '2026-07-05'\n")
    m = _load(tmp_path, monkeypatch)
    out = m.api_home()
    groups = [s["group"] for s in out["sections"]]
    assert "Start here" in groups, groups              # the brief stream has a dated issue
    assert "What moved" in groups, groups              # tiered actor with fresh last_updated
    assert "Predictions" in groups, groups
    assert "Knowledge gaps" in groups, groups
    assert "Jump off" in groups, groups
    # shape contract shared with the watchlist tab renderer
    assert all({"group", "title", "html"} <= set(s) for s in out["sections"])


def test_home_omits_everything_on_an_empty_vault(tmp_path, monkeypatch):
    """A fresh vault (no briefs, no watchlist config, no predictions/lacuna/dashboards)
    must return ZERO sections — not placeholder 'none' walls."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    assert m.api_home() == {"sections": []}


def test_home_dashboard_chips_handle_the_grouped_config_shape(tmp_path, monkeypatch):
    """Regression (cyber-market): its `dashboards:` config uses the GROUPED shape
    ([{group, items: [{path, title?}]}]) — the flat-slug chips code rendered raw dict
    reprs as chip labels/targets. Grouped items chip per item with namespace-qualified
    paths; flat slugs keep the dashboards/ prefix."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n"
        "  dashboards:\n"
        "  - group: Strategic\n"
        "    items:\n"
        "    - {path: lacuna/INDEX, title: Lacuna gaps}\n"
        "    - {path: dashboards/frontier-map}\n"
        "  tabs: [home]\n", encoding="utf-8")
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    out = m.api_home()
    html = next(s["html"] for s in out["sections"] if s["group"] == "Jump off")
    assert 'data-page="lacuna/INDEX"' in html and "Lacuna gaps" in html
    assert 'data-page="dashboards/frontier-map"' in html and "frontier map" in html
    assert "{" not in html, html   # no dict reprs ever
