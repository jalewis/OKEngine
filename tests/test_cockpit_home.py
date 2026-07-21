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


def test_active_predictions_count_as_open(tmp_path, monkeypatch):
    """The prediction 'open' vocabulary is a cross-surface contract: base-schema
    `open_values: [open, active]`, mirrored by pred_lib.OPEN_VALUES and read config-driven by the cron
    lanes. The cockpit is a fourth consumer — a status:active prediction (routine for migrated/drained
    sets) must appear under 'Open predictions' and in the due-soon tally, not be silently dropped
    because the code hardcoded status=='open' (invariant-audit M11)."""
    (tmp_path / "schema.yaml").write_text("cockpit:\n  tabs: [home]\n", encoding="utf-8")
    _mk(tmp_path, "predictions/p-active.md",
        "type: prediction\nstatus: active\nsubject: APT X pivots\nresolves_by: 2026-08-01\n")
    m = _load(tmp_path, monkeypatch)
    pr = m.api_predictions()
    assert pr["total"] == 1
    m2 = _load(tmp_path, monkeypatch)
    home = m2.api_home()
    pred = next((s for s in home["sections"] if s["group"] == "Predictions"), None)
    assert pred is not None, "status:active prediction must surface under Open predictions"
    assert "APT X pivots" in pred["html"]


def test_cockpit_open_status_matches_pred_lib_contract():
    """Pin the cockpit's _OPEN_STATUS to the single source of truth (pred_lib.OPEN_VALUES) so the two
    can't drift apart (the multi-surface-contract rule) — invariant-audit M11."""
    import os as _os
    _os.environ.setdefault("VAULT_DIR", "/tmp")   # module import only needs the env present
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    pl_path = REPO / "extensions" / "okengine.predictions" / "pred_lib.py"
    pl_spec = importlib.util.spec_from_file_location("pred_lib", pl_path)
    pl = importlib.util.module_from_spec(pl_spec)
    pl_spec.loader.exec_module(pl)
    assert m._OPEN_STATUS == set(pl.OPEN_VALUES), (m._OPEN_STATUS, pl.OPEN_VALUES)


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


def test_cockpit_browse_hides_archived_and_walkup_excluded(tmp_path, monkeypatch):
    """Cockpit discovery surfaces — browse ledger (_scan_dir), rail count (api_tree), dataset scan
    (_scan_dir_meta) — must hide reserved _archive/ sub-dirs (all three) and walk-up-nested excluded
    namespaces (the browse pair), matching the reader + /api/streams (batch-2 completeness re-verify)."""
    (tmp_path / "schema.yaml").write_text("exclude: [observations]\n", encoding="utf-8")
    e = tmp_path / "wiki" / "entities" / "a"; e.mkdir(parents=True)
    (e / "live.md").write_text("---\ntype: actor\n---\nL\n", encoding="utf-8")
    arch = tmp_path / "wiki" / "entities" / "_archive"; arch.mkdir(parents=True)
    (arch / "retired.md").write_text("---\ntype: actor\n---\nR\n", encoding="utf-8")
    wu = tmp_path / "wiki" / "sub" / "observations"; wu.mkdir(parents=True)
    (wu / "o.md").write_text("---\ntype: observation\n---\nO\n", encoding="utf-8")
    (tmp_path / "wiki" / "sub" / "entities").mkdir(parents=True)
    (tmp_path / "wiki" / "sub" / "entities" / "x.md").write_text("---\ntype: actor\n---\nX\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    tree = {x["dir"]: x["count"] for x in m.api_tree()["dirs"]}
    assert tree.get("entities") == 1, "archived page must not inflate the rail count"
    assert tree.get("sub") == 1, "walk-up excluded observations must not be counted"
    assert len(m._scan_dir("entities")) == 1
    assert all("_archive" not in r["path"] for r in m._scan_dir("entities"))
    assert len(m._scan_dir_meta("entities")) == 1, "dataset scan must skip _archive/ retired pages"


def test_predictions_and_backlinks_hide_archived(tmp_path, monkeypatch):
    """Two more cockpit discovery surfaces the completeness sweep's fix must cover: the Open-predictions
    view (_prediction_files) and the cockpit's own backlinks (_skip_backlink_src) must hide reserved
    _archive/ pages, matching browse/streams/search (batch-2 completeness re-verify)."""
    _mk(tmp_path, "predictions/p1.md",
        "type: prediction\nstatus: open\nsubject: Live\nresolves_by: 2026-09-01\n")
    _mk(tmp_path, "predictions/_archive/old.md",
        "type: prediction\nstatus: open\nsubject: Retired\nresolves_by: 2020-01-01\n")
    m = _load(tmp_path, monkeypatch)
    assert m.api_predictions()["total"] == 1, "archived prediction must not appear in the view"
    assert m._skip_backlink_src("entities/_archive/old") is True
    assert m._skip_backlink_src("entities/_archive/2026/old") is True
    assert m._skip_backlink_src("entities/a/live") is False


def test_reshard_bucket_page_stays_visible(tmp_path, monkeypatch):
    """The bare-`_` reshard second-letter bucket (entities/x/_/x-force.md for a non-alnum slug) is a
    LEGITIMATE canonical location — the reserved-segment guard must NOT over-drop it (batch-2 over-drop
    re-verify: x-force / e-commerce / t-mobile all reshard into a `_` bucket)."""
    b = tmp_path / "wiki" / "entities" / "x" / "_"; b.mkdir(parents=True)
    (b / "x-force.md").write_text("---\ntype: actor\nname: X-Force\n---\nbody\n", encoding="utf-8")
    ea = tmp_path / "wiki" / "entities" / "a"; ea.mkdir(parents=True)
    (ea / "apt.md").write_text("---\ntype: actor\n---\nA\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m._is_reserved_seg("_archive") is True and m._is_reserved_seg(".git") is True
    assert m._is_reserved_seg("_") is False and m._is_reserved_seg("entities") is False
    tree = {x["dir"]: x["count"] for x in m.api_tree()["dirs"]}
    assert tree.get("entities") == 2, "the _ reshard bucket page must be counted, not over-dropped"
    assert any("x-force" in r["path"] for r in m._scan_dir("entities")), "resharded x-force missing from ledger"
    assert len(m._scan_dir_meta("entities")) == 2, "reshard bucket page missing from dataset scan"


def test_home_gaps_ranked_by_maturity_with_signal_columns(tmp_path, monkeypatch):
    """The Knowledge-gaps table surfaces each gap's maturity (density / confidence / testable) and
    ranks most-grounded first, so a well-supported gap outranks a thin one without opening the page."""
    (tmp_path / "schema.yaml").write_text("cockpit:\n  tabs: [home]\n", encoding="utf-8")
    _mk(tmp_path, "lacuna/thin.md",
        "type: lacuna\nname: thin gap\ncreated: '2026-07-06'\nsurround_density: 12 links\nconfidence: low\n")
    _mk(tmp_path, "lacuna/grounded.md",
        "type: lacuna\nname: grounded gap\ncreated: '2026-07-05'\n"
        "surround_density: 340 links · sources 300\nconfidence: high\n"
        "prediction_candidate: predictions/grounded-2028\n")
    m = _load(tmp_path, monkeypatch)
    sec = next(s for s in m.api_home()["sections"] if s["group"] == "Knowledge gaps")
    html = sec["html"]
    for col in ("Density", "Confidence", "Testable"):
        assert col in html, col
    assert "340" in html and "high" in html and "✓" in html          # the grounded gap's signals
    assert html.index("grounded gap") < html.index("thin gap")       # most-grounded first


def test_browse_groups_omit_empty_kind(tmp_path, monkeypatch):
    """okengine#259 Browse cleanup: the cockpit /api/groups drops a declared display_group with 0
    matching pages (the observed 'Report vendors 0'), matching /api/tree hiding empty namespaces."""
    (tmp_path / "schema.yaml").write_text(
        "display_groups:\n"
        "  Threat actors: [actor]\n"
        "  Report vendors: [report-vendor]\n", encoding="utf-8")
    _mk(tmp_path, "entities/a/apt42.md", "type: actor\n")
    m = _load(tmp_path, monkeypatch)
    assert [g["label"] for g in m.api_groups()["groups"]] == ["Threat actors"]


def test_box_provenance_affordance(tmp_path, monkeypatch):
    """okengine#259 Rec 11: a panel tagged `provenance:` (a corpus/coverage measure, not a threat
    measure) surfaces a structured provenance badge {label, note} on the box so the UI can mark it
    distinct — a 'coverage ▼' dip must not read as the threat declining. String shorthand + dict
    both work; an untagged panel carries no provenance key."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Theme trends\n          view: bars\n          dataset: {dir: trends, type: trend}\n"
        "          group_by: direction\n"
        "          provenance: {label: coverage, note: \"Reporting volume, not threat level.\"}\n"
        "        - title: Actor movement\n          view: bars\n          dataset: {dir: entities, type: actor}\n"
        "          group_by: origin\n", encoding="utf-8")
    _mk(tmp_path, "trends/x.md", "type: trend\ndirection: up\n")
    _mk(tmp_path, "entities/a/apt.md", "type: actor\norigin: China\n")
    m = _load(tmp_path, monkeypatch)
    by = {b["title"]: b for b in m.api_tab("t")["boxes"]}
    assert by["Theme trends"]["provenance"] == {"label": "coverage", "note": "Reporting volume, not threat level."}
    assert "provenance" not in by["Actor movement"]          # untagged panel unaffected


def test_box_provenance_string_shorthand(tmp_path, monkeypatch):
    """`provenance: coverage` shorthand expands to {label: coverage, note: ''}."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Publisher activity\n          view: bars\n          dataset: {dir: sources, type: source}\n"
        "          group_by: publisher\n          provenance: coverage\n", encoding="utf-8")
    _mk(tmp_path, "sources/s.md", "type: source\npublisher: Acme\n")
    m = _load(tmp_path, monkeypatch)
    box = {b["title"]: b for b in m.api_tab("t")["boxes"]}["Publisher activity"]
    assert box["provenance"] == {"label": "coverage", "note": ""}
