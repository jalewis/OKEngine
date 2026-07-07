"""Cockpit declarative dataset tabs (/api/tab/<key>) — the data-first redesign.

The pack config defines a tab as DATASET BOXES (dataset + view); the engine renders.
Views: table (sort/require/limit, tones, defang, list-max), bars (group_by with value
LABELS for opaque codes, or label/value fields), chips, bignums (count/top, per-item
dataset override), cards (direction glyph + status + series mini-bars), coverage (join:
list_field vs a versus-dataset key, grouped), doc (latest dated file rendered inline).
Empty dataset + `empty:` note → the note renders (pipeline state is information);
empty without a note → the box is omitted."""
import importlib.util
import re
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


def _mk(root, rel, fm):
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\nbody\n", encoding="utf-8")


CFG = """\
cockpit:
  tabs: [threats]
  tab_defs:
    threats:
      label: Threats
      boxes:
        - title: Pulse
          span: 12
          view: bignums
          dataset: {dir: entities, type: actor}
          items:
            - {label: actors, stat: count}
            - {label: hot, stat: count, where: {tier: hot}, tone: crit}
            - {label: top origin, stat: top, group_by: origin}
        - title: Most active
          span: 7
          view: table
          dataset: {dir: entities, type: actor}
          sort: {field: recent, desc: true, require: true}
          limit: 2
          columns:
            - {field: title, label: Actor, link: true}
            - {field: recent, label: Recent, tone: crit}
        - title: Sectors
          span: 5
          view: bars
          dataset: {dir: incidents}
          group_by: industry
          labels: {"622110": Hospitals}
        - title: Fresh IOCs
          span: 6
          view: table
          dataset: {dir: iocs}
          columns:
            - {field: value, label: Indicator, defang: true}
        - title: Coverage
          span: 6
          view: coverage
          dataset: {dir: rules}
          list_field: covers
          versus: {dir: techniques, key: tid, group_by: tactic}
        - title: Themes
          span: 12
          view: cards
          dataset: {dir: trends, has: [report_theme]}
        - title: Forecasts
          span: 6
          view: table
          dataset: {dir: predictions, type: prediction}
          columns: [{field: title, label: P}]
          empty: "0 open — lanes armed; ledger appears when candidates land."
        - title: Ghost
          span: 6
          view: table
          dataset: {dir: nothing-here}
          columns: [{field: x, label: X}]
"""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(CFG, encoding="utf-8")
    _mk(tmp_path, "entities/a/apt-a.md", "type: actor\ntitle: APT A\ntier: hot\nrecent: 9\norigin: CN\n")
    _mk(tmp_path, "entities/b/apt-b.md", "type: actor\ntitle: APT B\ntier: cold\norigin: CN\n")
    _mk(tmp_path, "incidents/i1.md", "type: incident\nindustry: '622110'\n")
    _mk(tmp_path, "incidents/i2.md", "type: incident\nindustry: '622110'\n")
    _mk(tmp_path, "iocs/u1.md", "type: indicator\nvalue: http://evil.example/x\n")
    _mk(tmp_path, "rules/r1.md", "type: detection\ncovers: [T1000]\n")
    _mk(tmp_path, "techniques/t1000.md", "type: technique\ntid: T1000\ntactic: stealth\n")
    _mk(tmp_path, "techniques/t2000.md", "type: technique\ntid: T2000\ntactic: stealth\n")
    _mk(tmp_path, "trends/theme-x.md",
        "type: trend\ntitle: X\nreport_theme: x\ndirection: rising\ntrend_status: active\n"
        "count_by_year: {'2025': 1, '2026': 3}\n")
    _mk(tmp_path, "trends/shift-2026-07-06.md", "type: trend\ntitle: Shifts\n")  # no report_theme
    return _load(tmp_path, monkeypatch)


def test_tab_renders_every_view_from_real_frontmatter(vault):
    out = vault.api_tab("threats")
    assert out["label"] == "Threats"
    by = {b["title"]: b for b in out["boxes"]}

    # bignums: count, filtered count, top-of-group
    assert ">2<" in by["Pulse"]["html"] and ">1<" in by["Pulse"]["html"] and "CN" in by["Pulse"]["html"]
    # table: require drops the actor with no `recent`; link + tone cells render
    assert "APT A" in by["Most active"]["html"] and "APT B" not in by["Most active"]["html"]
    assert 't-crit' in by["Most active"]["html"]
    # bars: NAICS-style label map applied — the analyst never sees the raw code
    _sect = re.sub(r'data-dval="[^"]*"', '', by["Sectors"]["html"])   # drop the drill filter value
    assert "Hospitals" in _sect and "622110" not in _sect            # raw code never a VISIBLE label
    # defang: never render a live IOC
    assert "hxxp://evil[.]example/x" in by["Fresh IOCs"]["html"]
    assert "http://evil.example" not in by["Fresh IOCs"]["html"]
    # coverage: 1 of 2 stealth techniques covered
    assert "1/2" in by["Coverage"]["html"] and "stealth" in by["Coverage"]["html"]
    # cards: has-filter keeps theme pages, excludes the shift doc; direction glyph renders
    assert "▲" in by["Themes"]["html"] and "Shifts" not in by["Themes"]["html"]
    # honest-empty: the note renders in place of the empty forecast ledger
    assert "lanes armed" in by["Forecasts"]["html"]
    assert by["Forecasts"]["meta"] == "awaiting first data"
    # empty WITHOUT a note is omitted entirely — no placeholder walls
    assert "Ghost" not in by


def test_unknown_tab_404s(vault):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        vault.api_tab("nope")


def test_drilldown_targets_and_filtered_pages(vault):
    """okengine#189: aggregate widgets carry drill targets, and /api/drill re-derives the dataset +
    filter from config to return the underlying pages. Bars group_by (box 2, Sectors), bignums
    count/filtered-count/top-of-group (box 0, Pulse)."""
    by = {b["title"]: b for b in vault.api_tab("threats")["boxes"]}
    # the widgets advertise drill targets the frontend binds to
    assert 'data-drill data-dtab="threats" data-dbox="2"' in by["Sectors"]["html"]
    assert 'data-dval="622110"' in by["Sectors"]["html"]
    assert 'data-drill data-dtab="threats" data-dbox="0"' in by["Pulse"]["html"]
    assert 'data-ditem="0"' in by["Pulse"]["html"]
    # table/coverage/cards are NOT drillable — no stray targets
    assert "data-drill" not in by["Most active"]["html"]

    # bars group_by bucket -> the incidents with that industry (both i1, i2)
    d = vault.api_drill("threats", 2, value="622110")
    assert d["count"] == 2 and {p["path"] for p in d["pages"]} == {"incidents/i1", "incidents/i2"}
    assert "Hospitals" in d["title"]                       # heading uses the label map

    # bignums: item 0 = all actors; item 1 = tier:hot only; item 2 = top origin bucket
    assert {p["path"] for p in vault.api_drill("threats", 0, item=0)["pages"]} == \
        {"entities/a/apt-a", "entities/b/apt-b"}
    hot = vault.api_drill("threats", 0, item=1)
    assert {p["path"] for p in hot["pages"]} == {"entities/a/apt-a"}      # only the hot actor
    top = vault.api_drill("threats", 0, item=2)                          # top origin == CN -> both
    assert top["count"] == 2 and "CN" in top["title"]

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        vault.api_drill("threats", 1, value="x")           # box 1 is a table -> not drillable


def test_value_field_bar_opens_its_page(tmp_path, monkeypatch):
    """okengine#189 follow-up: a value_field bar is ONE page, so it opens that page directly
    (data-dpage → the page overlay), not a filtered list — no /api/drill round-trip, no data-dval."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Shared tooling\n          view: bars\n          dataset: {dir: entities, type: tool}\n"
        "          value_field: used_by_count\n          label_field: title\n", encoding="utf-8")
    _mk(tmp_path, "entities/m/mimikatz.md", "type: tool\ntitle: Mimikatz\nused_by_count: 52\n")
    _mk(tmp_path, "entities/p/psexec.md", "type: tool\ntitle: PsExec\nused_by_count: 41\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    # the top bar opens its sharded page directly; no group_by filter value, no /api/drill
    assert 'data-dpage="entities/m/mimikatz"' in html and "Mimikatz" in html
    assert "data-dval" not in html and "data-dbox" not in html
    # and the endpoint refuses a value_field box (not a list drill)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        m.api_drill("t", 0, value="x")


def test_config_exposes_tab_labels(vault):
    cfg = vault.api_config()
    assert cfg["tab_labels"] == {"threats": "Threats"}
    assert "threats" in cfg["tabs"]


def test_doc_view_links_the_original_article(tmp_path, monkeypatch):
    """A brief's `Source: [[sources/...]]` citation must reach the ORIGINAL reporting in one
    click: the slug text is swapped for the source page's real title, and that TITLE itself is
    the external link to the page's `url:` frontmatter — not an internal wikilink with the url
    demoted to a glyph. A source with no url falls back to the internal wikilink."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - {title: Brief, view: doc, dir: briefings, glob: 'daily-*', span: 7}\n",
        encoding="utf-8")
    _mk(tmp_path, "sources/2026/07/ms-device-code.md",
        "type: source\ntitle: Device code phishing via Microsoft OAuth\n"
        "url: https://securelist.example/device-code\npublisher: Kaspersky\n")
    _mk(tmp_path, "sources/2026/07/no-url.md", "type: source\ntitle: Local-only note\n")
    _mk(tmp_path, "briefings/daily-2026-07-06.md",
        "type: briefing\ntitle: D\n")
    (tmp_path / "wiki" / "briefings" / "daily-2026-07-06.md").write_text(
        "---\ntype: briefing\ntitle: D\n---\n"
        "Item one.\nSource: [[sources/2026/07/ms-device-code]]\n\n"
        "Item two.\nSource: [[sources/2026/07/no-url]]\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    # slug replaced by the page title, and the title IS the external link to the original
    assert 'href="https://securelist.example/device-code"' in html
    assert 'class="ext"' in html and "Device code phishing via Microsoft OAuth</a>" in html
    assert "&#8599;" not in html and "↗" not in html   # no demoted glyph
    assert "ms-device-code" not in html                # url'd source: no internal wikilink at all
    # no url -> title swap still happens, falls back to the internal wikilink
    assert "Local-only note" in html
    assert 'data-page="sources/2026/07/no-url"' in html
    assert html.count("https://securelist.example") == 1


def test_bars_flags_unmapped_group_values(tmp_path, monkeypatch):
    """okengine#188: a group_by bars box with a PARTIAL `labels:` map must not silently print a
    raw code as if it were a curated label. The mapped code renders its label and is not flagged;
    the unmapped code still shows (nothing hidden) but is marked degraded (`um-flag`) AND surfaced
    in the box's `unmapped` list so the UI can warn. A box with NO labels map declares no
    expectation, so it flags nothing — existing fully/zero-labeled boxes are unchanged."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: By sector\n          view: bars\n          dataset: {dir: incidents}\n"
        "          group_by: industry\n          labels: {'622110': Hospitals}\n"
        "        - title: Raw\n          view: bars\n          dataset: {dir: incidents}\n"
        "          group_by: industry\n", encoding="utf-8")
    _mk(tmp_path, "incidents/i1.md", "type: incident\nindustry: '622110'\n")
    _mk(tmp_path, "incidents/i2.md", "type: incident\nindustry: '622110'\n")
    _mk(tmp_path, "incidents/i3.md", "type: incident\nindustry: '923120'\n")  # unlabeled NAICS
    m = _load(tmp_path, monkeypatch)

    # contract: _ds_pairs yields (label, value, unmapped, key) tuples; only the uncovered code flags
    box = {"group_by": "industry", "labels": {"622110": "Hospitals"}}
    rows = m._ds_rows({"dir": "incidents"})
    pairs = dict((l, um) for l, _v, um, _k in m._ds_pairs(box, rows))
    assert pairs == {"Hospitals": False, "923120": True}

    by = {b["title"]: b for b in m.api_tab("t")["boxes"]}
    sect = by["By sector"]
    # mapped code -> label; the raw code is never shown as-if-a-label
    _vis = re.sub(r'data-dval="[^"]*"', '', sect["html"])            # drop the drill filter value
    assert "Hospitals" in _vis and "622110" not in _vis             # raw code never a VISIBLE label
    # unmapped code still shows (nothing hidden) BUT is marked degraded + surfaced to the UI
    assert "923120" in sect["html"] and "um-flag" in sect["html"]
    assert sect.get("unmapped") == ["923120"]

    # a box with NO labels map declares no expectation -> nothing flagged, no `unmapped` key
    raw = by["Raw"]
    assert "um-flag" not in raw["html"]
    assert "unmapped" not in raw


def test_page_links_carry_the_sharded_path(tmp_path, monkeypatch):
    """Regression: _page_link emitted `entities/<stem>` for a page at entities/<letter>/<stem>.md —
    basename resolution papered over it until two shards collide on a stem (and any consumer
    needing the true path, e.g. the write server, got a wrong one)."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: A\n          view: table\n          dataset: {dir: entities, type: actor}\n"
        "          columns: [{field: title, label: Actor, link: true}]\n", encoding="utf-8")
    _mk(tmp_path, "entities/s/scattered-spider.md", "type: actor\ntitle: Scattered Spider\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert 'data-page="entities/s/scattered-spider"' in html, html
