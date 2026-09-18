"""Cockpit declarative dataset tabs (/api/tab/<key>) — the data-first redesign.

The pack config defines a tab as DATASET BOXES (dataset + view); the engine renders.
Views: table (sort/require/limit, tones, defang, list-max), bars (group_by with value
LABELS for opaque codes, or label/value fields), chips, bignums (count/top, per-item
dataset override), cards (direction glyph + status + series mini-bars), coverage (join:
list_field vs a versus-dataset key, grouped), doc (latest dated file rendered inline),
doc-summary (named section or bounded fallback excerpt, linked to the full document).
Empty dataset + `empty:` note → the note renders (pipeline state is information);
empty without a note → the box is omitted."""
import importlib.util
import json
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
          where: {status: active}
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
    _mk(tmp_path, "incidents/i1.md", "type: incident\nstatus: active\nindustry: '622110'\n")
    _mk(tmp_path, "incidents/i2.md", "type: incident\nstatus: superseded\nindustry: '622110'\n")
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
    assert '<span class="bnum">1</span>' in _sect                      # superseded row filtered
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


def test_trend_cards_honor_each_records_analytical_clock(vault):
    partial = {"title": "Partial", "direction": "down", "trend_status": "active",
               "comparison": "partial-period", "count_by_year": {"2025": 8, "2026": 2}}
    ytd = {"title": "YTD", "direction": "up", "trend_status": "active",
           "comparison": "ytd", "comparison_as_of": "07-15",
           "count_by_year": {"2025": 8, "2026": 9},
           "count_ytd_by_year": {"2025": 3, "2026": 5}}
    full = {"title": "Full", "direction": "up", "trend_status": "active",
            "count_by_year": {"2024": 2, "2025": 4}}

    html = vault._v_cards({}, [partial, ytd, full])

    assert 'title="comparison: partial-period">◒ partial period' in html
    assert "partial period · direction suppressed" in html
    assert 'title="comparison: ytd">▲ up' in html and "YTD through 07-15" in html
    assert "2025: 3" in html and "2026: 5" in html
    assert 'title="comparison: full-period">▲ up' in html and "full-period comparison" in html


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
    # tables/cards carry no PER-ROW drill target (there is no bucket to filter on); coverage does.
    # Their whole-list drill hangs off the meta line instead — see meta_drill tests below.
    assert "data-drill" not in by["Most active"]["html"]
    assert 'data-drill data-dtab="threats" data-dbox="4"' in by["Coverage"]["html"]
    assert 'data-dval="stealth"' in by["Coverage"]["html"]

    # bars group_by bucket -> only the box-level `where`-eligible incident
    d = vault.api_drill("threats", 2, value="622110")
    assert d["count"] == 1 and {p["path"] for p in d["pages"]} == {"incidents/i1"}
    assert "Hospitals" in d["title"]                       # heading uses the label map

    # bignums: item 0 = all actors; item 1 = tier:hot only; item 2 = top origin bucket
    assert {p["path"] for p in vault.api_drill("threats", 0, item=0)["pages"]} == \
        {"entities/a/apt-a", "entities/b/apt-b"}
    hot = vault.api_drill("threats", 0, item=1)
    assert {p["path"] for p in hot["pages"]} == {"entities/a/apt-a"}      # only the hot actor
    top = vault.api_drill("threats", 0, item=2)                          # top origin == CN -> both
    assert top["count"] == 2 and "CN" in top["title"]

    coverage = vault.api_drill("threats", 4, value="stealth")
    assert coverage["count"] == 2
    assert {p["path"] for p in coverage["pages"]} == {
        "techniques/t1000", "techniques/t2000"}

    # okengine#564: a table IS drillable now — its full row set in the box's own sort order. It
    # takes no bucket, so `value` is ignored rather than rejected.
    t = vault.api_drill("threats", 1, value="ignored")
    assert t["count"] == 1 and {p["path"] for p in t["pages"]} == {"entities/a/apt-a"}
    assert t["truncated"] is False
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        vault.api_drill("threats", 99, value="x")          # no such box


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


def test_dataset_exclude_values_filters_scalar_and_list_fields(vault):
    rows = [
        {"id": "net", "tags": ["utility", "windows"]},
        {"id": "mimikatz", "tags": ["credential-access"]},
        {"id": "ping", "tags": "utility"},
    ]
    assert vault._refine_rows(rows, {"exclude_values": {"id": ["Net", "PING"]}}) == [rows[1]]
    assert vault._refine_rows(rows, {"exclude_values": {"tags": "utility"}}) == [rows[1]]


def test_source_display_hygiene_repairs_slugs_and_missing_publishers(vault):
    row = {"_sub": "sources", "_name": "report", "_rel": "2026/07/report",
           "type": "source", "title": "microsoft-device-code-phishing-advisory-md",
           "url": "https://www.microsoft.com/security/blog/report"}
    title = vault._ds_cell(row, {"field": "title", "link": True, "source_title": True})
    publisher = vault._ds_cell(row, {"field": "publisher", "source_publisher": True})
    assert "Microsoft Device Code Phishing Advisory" in title and "-md" not in title
    assert "microsoft.com" in publisher
    assert "Unknown publisher ⚠" in vault._source_publisher({})
    assert vault._source_publisher({"publisher": "unknown (derived from entity)"}) == "Unknown publisher ⚠"
    assert vault._source_publisher({
        "publisher": "CTO at NCSC newsletter Ollie blueteamsec Substack cybersecurity threat "
                     "intelligence briefing aggregating vendor reporting from multiple sources",
        "url": "https://ctoatncsc.substack.com/p/weekly",
    }) == "ctoatncsc.substack.com"
    assert vault._source_publisher({
        "publisher": "Smashing Security Podcast with Graham Cluley Quentyn Taylor episode",
        "url": "https://www.smashingsecurity.com/474",
    }) == "smashingsecurity.com"
    assert vault._source_publisher({
        "publisher": "CISA KEV / The Hacker News (derived)",
        "url": "https://thehackernews.com/example",
    }) == "CISA KEV / The Hacker News (derived)"


def test_config_exposes_tab_labels(vault):
    cfg = vault.api_config()
    assert cfg["tab_labels"] == {"threats": "Threats"}
    assert "threats" in cfg["tabs"]


def test_table_labels_dates_and_metadata_template(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Actors\n          view: table\n          dataset: {dir: entities, type: actor}\n"
        "          sort: {field: seen, desc: true, require: true, date: true}\n          limit: 1\n"
        "          meta_template: 'showing {shown} of {total}'\n          columns:\n"
        "            - {field: origin, label: Origin, labels: {IR: Iran}}\n"
        "            - {field: seen, label: Seen, date: true}\n",
        encoding="utf-8")
    _mk(tmp_path, "entities/a.md", "type: actor\norigin: IR\nseen: 2026-07-16\n")
    _mk(tmp_path, "entities/b.md", "type: actor\norigin: IR\nseen: queued-for-review\n")
    m = _load(tmp_path, monkeypatch)
    box = m.api_tab("t")["boxes"][0]
    assert box["meta"] == "showing 1 of 1"
    assert "Iran" in box["html"]
    # Render the malformed row directly: dates are never silently sliced into plausible nonsense.
    assert "invalid date" in m._v_table(
        {"columns": [{"field": "seen", "label": "Seen", "date": True}]},
        [{"seen": "2026-0713T"}])
    assert "Unknown" in m._v_table(
        {"columns": [{"field": "origin", "label": "Origin", "empty": "Unknown"}]}, [{}])
    assert m._disp({"_name": "missing-curated-title"}) == "Missing Curated Title"


def test_assessed_value_column_joins_ledger_and_documents_epistemic_state(tmp_path, monkeypatch):
    """An analytical origin must never render as an unqualified actor fact."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Actor movement\n          view: table\n"
        "          dataset: {dir: entities, type: actor}\n          columns:\n"
        "            - {field: title, label: Actor, link: true}\n"
        "            - label: Assessed origin\n              assessment:\n"
        "                kind: actor-country-linkage\n                value_field: assessed_value\n"
        "                labels: {IR: Iran}\n",
        encoding="utf-8")
    _mk(tmp_path, "entities/a/apt-a.md",
        "type: actor\ntitle: APT A\norigin_country: RU\n")
    _mk(tmp_path, "entities/b/apt-b.md",
        "type: actor\ntitle: APT B\norigin_country: CN\n")
    _mk(tmp_path, "assessments/a/apt-a-iran.md",
        "type: assessment\ntitle: APT A — Iran reported association\n"
        "assessment_kind: actor-country-linkage\nsubject: entities/a/apt-a\n"
        "status: active\nepistemic_status: assessed\nassessed_value: IR\n"
        "confidence: 0.85\nconfidence_band: high\nneeds_review: true\n"
        "as_of: 2026-07-17T12:00:00Z\nlast_updated: 2026-07-17T12:00:00Z\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]

    assert "Assessed origin" in html
    assert "Iran" in html and "85%" in html and "◇" in html
    assert 'data-page="assessments/a/apt-a-iran"' in html
    assert 'aria-label="Assessed analytical judgment; high confidence; human review pending"' in html
    assert "Review not run" in html                     # APT B is outside the latest review
    assert "Russia" not in html and "China" not in html  # never fall back to actor fact fields
    assert "Assessed judgment" in html
    assert 'data-page="assessments/_about"' in html


def test_assessed_value_states_and_missing_metadata_fail_visible(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Actors\n          view: table\n"
        "          dataset: {dir: entities, type: actor}\n          columns:\n"
        "            - label: Assessed origin\n              assessment:\n"
        "                kind: actor-country-linkage\n                value_field: assessed_value\n",
        encoding="utf-8")
    for slug in ("confirmed", "disputed", "inconclusive", "broken"):
        _mk(tmp_path, f"entities/{slug[0]}/{slug}.md", f"type: actor\ntitle: {slug}\n")
    _mk(tmp_path, "assessments/d/disputed.md",
        "type: assessment\nassessment_kind: actor-country-linkage\nsubject: entities/d/disputed\n"
        "status: disputed\nepistemic_status: disputed\nassessed_value: IR\nconfidence: 0.55\n"
        "as_of: 2026-07-17T12:00:00Z\n")
    _mk(tmp_path, "assessments/i/inconclusive.md",
        "type: assessment\nassessment_kind: actor-country-linkage\nsubject: entities/i/inconclusive\n"
        "status: active\nepistemic_status: inconclusive\nconfidence: 0.50\n"
        "as_of: 2026-07-17T12:00:01Z\n")
    _mk(tmp_path, "assessments/b/broken.md",
        "type: assessment\nassessment_kind: actor-country-linkage\nsubject: entities/b/broken\n"
        "status: active\nepistemic_status: assessed\nconfidence: 0.80\n"
        "as_of: 2026-07-17T12:00:02Z\n")
    _mk(tmp_path, "assessments/c/confirmed.md",
        "type: assessment\nassessment_kind: actor-country-linkage\nsubject: entities/c/confirmed\n"
        "status: active\nepistemic_status: confirmed\nassessed_value: RU\nconfidence: 0.98\n"
        "as_of: 2026-07-17T12:00:03Z\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Disputed" in html
    assert "Inconclusive" in html
    assert "assessment metadata unavailable" in html
    assert "◆" in html and "98%" in html


def test_assessment_backed_rollup_separates_epistemic_states_and_drills_both_records(
        tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Top assessed origins\n          view: bars\n"
        "          dataset: {dir: entities, type: actor}\n          limit: 6\n"
        "          assessment:\n            kind: actor-country-linkage\n"
        "            value_field: assessed_value\n"
        "            labels: {IR: Iran, CN: China, RU: Russia}\n",
        encoding="utf-8")
    for slug, canonical in (("alpha", "RU"), ("beta", "CN"), ("gamma", "IR"), ("delta", "RU")):
        _mk(tmp_path, f"entities/{slug[0]}/{slug}.md",
            f"type: actor\ntitle: {slug.title()}\norigin_country: {canonical}\n")
    _mk(tmp_path, "entities/r/retired.md",
        "type: actor\ntitle: Retired duplicate\nstatus: tombstoned\nredirect_to: entities/a/alpha\n")
    _mk(tmp_path, "entities/r/redirect.md",
        "type: actor\ntitle: Redirect alias\nredirect_to: entities/a/alpha\n")
    _mk(tmp_path, "assessments/a/alpha-old.md",
        "type: assessment\ntitle: Alpha old\nassessment_kind: actor-country-linkage\n"
        "subject: entities/a/alpha\nstatus: active\nepistemic_status: assessed\n"
        "assessed_value: RU\nconfidence: 0.20\nlast_updated: 2026-07-16T12:00:00Z\n")
    _mk(tmp_path, "assessments/a/alpha-current.md",
        "type: assessment\ntitle: Alpha current\nassessment_kind: actor-country-linkage\n"
        "subject: entities/a/alpha\nstatus: active\nepistemic_status: reported\n"
        "assessed_value: IR\nconfidence: 0.80\nlast_updated: 2026-07-17T12:00:00Z\n")
    _mk(tmp_path, "assessments/b/beta.md",
        "type: assessment\ntitle: Beta current\nassessment_kind: actor-country-linkage\n"
        "subject: entities/b/beta\nstatus: active\nepistemic_status: assessed\n"
        "assessed_value: CN\nconfidence: 0.60\nneeds_review: true\n"
        "last_updated: 2026-07-17T12:00:00Z\n")
    _mk(tmp_path, "assessments/g/gamma.md",
        "type: assessment\ntitle: Gamma disputed\nassessment_kind: actor-country-linkage\n"
        "subject: entities/g/gamma\nstatus: disputed\nepistemic_status: disputed\n"
        "assessed_value: IR\nconfidence: 0.50\nlast_updated: 2026-07-17T12:00:00Z\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Iran ◇ 80% avg" in html
    assert "China ◇ 60% avg ⚠1" in html
    assert "Disputed" in html and "Review not run" in html
    assert "Retired duplicate" not in html
    assert "Redirect alias" not in html
    assert "Russia ◇" not in html  # no canonical fallback and newest record wins

    assert "Assessment-backed rollup" in html
    assert 'data-page="assessments/_about"' in html
    assert 'data-dval="IR"' in html and 'data-dpage="IR"' not in html

    iran = m.api_drill("t", 0, value="IR")
    assert iran["count"] == 1
    assert iran["count_label"] == "1 actor"
    assert [(section["title"], section["count"]) for section in iran["sections"]] == [
        ("Actors", 1), ("Supporting assessments", 1)]
    assert [page["path"] for page in iran["sections"][0]["pages"]] == ["entities/a/alpha"]
    assert [page["path"] for page in iran["sections"][1]["pages"]] == [
        "assessments/a/alpha-current"]
    assert {page["path"] for page in iran["pages"]} == {
        "entities/a/alpha", "assessments/a/alpha-current"}
    unassessed = m.api_drill("t", 0, value="__review_not_run__")
    assert unassessed["count"] == 1
    assert unassessed["count_label"] == "1 actor" and "sections" not in unassessed
    assert [page["path"] for page in unassessed["pages"]] == ["entities/d/delta"]


def test_table_dataset_excludes_tombstoned_actor(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Actors\n          view: table\n"
        "          dataset: {dir: entities, type: actor}\n"
        "          columns: [{field: title, label: Actor}]\n",
        encoding="utf-8",
    )
    _mk(tmp_path, "entities/l/live.md", "type: actor\ntitle: Live Actor\n")
    _mk(tmp_path, "entities/u/unsafe.md",
        "type: actor\ntitle: Unsafe\nstatus: tombstoned\n")

    html = _load(tmp_path, monkeypatch).api_tab("t")["boxes"][0]["html"]

    assert "Live Actor" in html
    assert "Unsafe" not in html


def test_assessed_origin_uses_bounded_terminal_states_when_no_record_exists(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Actors\n          view: table\n"
        "          dataset: {dir: entities, type: actor}\n          columns:\n"
        "            - {field: title, label: Actor}\n"
        "            - label: Assessed origin\n              assessment:\n"
        "                kind: actor-country-linkage\n                value_field: assessed_value\n",
        encoding="utf-8")
    _mk(tmp_path, "entities/a/alpha.md", "type: actor\ntitle: Alpha\n")
    _mk(tmp_path, "entities/b/bravo.md", "type: actor\ntitle: Bravo\n")
    _mk(tmp_path, "entities/c/charlie.md", "type: actor\ntitle: Charlie\n")
    state = tmp_path / ".okengine/actor-country-review-coverage.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"subjects": {
        "entities/a/alpha": {"state": "no-association-established",
                             "reason": "bounded local search found no country claim"},
        "entities/b/bravo": {"state": "collection-required",
                             "reason": "declared source page is missing"},
        "entities/c/charlie": {"state": "assessed",
                                "reason": "assessment subject path is stale"}}}), encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "No association established" in html
    assert "Collection required" in html
    assert "Assessment reference stale" in html
    assert "declared source page is missing" in html
    assert "Not assessed" not in html and "Review not run" not in html
    assert 'data-page="dashboards/actor-review-status"' in html


def _terminal_vault(tmp_path, monkeypatch, subjects, pages=("entities/q/qilin.md",)):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Actors\n          view: table\n"
        "          dataset: {dir: entities, type: actor}\n          columns:\n"
        "            - {field: title, label: Actor}\n"
        "            - label: Assessed origin\n              assessment:\n"
        "                kind: actor-country-linkage\n                value_field: assessed_value\n",
        encoding="utf-8")
    for page in pages:
        _mk(tmp_path, page, "type: actor\ntitle: Qilin\n")
    state = tmp_path / ".okengine/actor-country-review-coverage.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps({"subjects": subjects}), encoding="utf-8")
    return _load(tmp_path, monkeypatch)


def test_a_reshard_between_the_lane_and_the_view_does_not_orphan_the_terminal_state(
        tmp_path, monkeypatch):
    """The projection stores the subject path AS IT WAS when the lane ran.

    A reshard moves the page afterwards -- `entities/q/i/qilin` collapsed to `entities/q/qilin` on
    the live vault -- and a raw-path lookup then misses, so a measured bounded no-finding silently
    reads as "Review not run": a verdict that WAS computed, presented as never computed. This is the
    same orphaning `_subject_key` already fixes for assessment records; the sibling lookup was left
    on the raw path.
    """
    m = _terminal_vault(tmp_path, monkeypatch, {
        "entities/q/i/qilin": {"state": "collection-required",
                               "reason": "declared source page is missing"}})
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Collection required" in html, "the state survives the page moving shard"
    assert "Review not run" not in html


def test_an_exact_path_match_still_wins(tmp_path, monkeypatch):
    """The fallback must not change behaviour when the projection is current."""
    m = _terminal_vault(tmp_path, monkeypatch, {
        "entities/q/qilin": {"state": "collection-required", "reason": "exact"},
        "entities/q/x/qilin": {"state": "no-association-established", "reason": "stale shard"}})
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Collection required" in html and "exact" in html
    assert "No association established" not in html


def test_two_shards_disagreeing_about_one_slug_refuse_to_pick_a_winner(tmp_path, monkeypatch):
    """A slug at two paths in one namespace is a partition duplicate `check_partition_dups()` owns
    as a FAIL. Resolving it by picking one would publish one of two contradictory verdicts as fact,
    and the reader could not tell it had been guessed. Falling through to "Review not run" is the
    honest answer -- the duplicate is a corpus fault, not a display decision."""
    m = _terminal_vault(tmp_path, monkeypatch, {
        "entities/q/i/qilin": {"state": "collection-required", "reason": "one"},
        "entities/q/x/qilin": {"state": "no-association-established", "reason": "two"}})
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Review not run" in html
    assert "Collection required" not in html and "No association established" not in html


def test_two_shards_that_agree_are_not_ambiguous(tmp_path, monkeypatch):
    record = {"state": "collection-required", "reason": "same verdict twice"}
    m = _terminal_vault(tmp_path, monkeypatch, {
        "entities/q/i/qilin": dict(record), "entities/q/x/qilin": dict(record)})
    assert "Collection required" in m.api_tab("t")["boxes"][0]["html"]


def test_a_malformed_projection_entry_is_skipped_not_crashed(tmp_path, monkeypatch):
    m = _terminal_vault(tmp_path, monkeypatch, {
        "entities/q/i/qilin": ["not", "a", "record"],
        "entities/z/z/other": {"state": "collection-required", "reason": "unrelated"}})
    assert "Review not run" in m.api_tab("t")["boxes"][0]["html"]


def test_plain_columns_do_not_receive_assessment_legend(vault):
    assert "Assessed judgment" not in vault.api_tab("threats")["boxes"][1]["html"]


def test_doc_summary_named_section_default_excerpt_and_full_link(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - {title: Daily, view: doc-summary, dir: briefings, glob: 'daily-*', max_sections: 1}\n"
        "        - {title: Weekly, view: doc-summary, dir: briefings, glob: 'weekly-*', section: Themes}\n",
        encoding="utf-8")
    _mk(tmp_path, "briefings/daily-2026-07-16.md", "type: briefing\n")
    (tmp_path / "wiki/briefings/daily-2026-07-16.md").write_text(
        "---\ntype: briefing\n---\n## First\nAlpha\n\n## Second\nBeta\n")
    (tmp_path / "wiki/briefings/weekly-2026-07-16.md").write_text(
        "---\ntype: briefing\n---\n## Themes\nOne\n\n## Detail\nTwo\n")
    m = _load(tmp_path, monkeypatch)
    by = {b["title"]: b for b in m.api_tab("t")["boxes"]}
    assert "Alpha" in by["Daily"]["html"] and "Beta" not in by["Daily"]["html"]
    assert "One" in by["Weekly"]["html"] and "Two" not in by["Weekly"]["html"]
    assert 'data-page="briefings/daily-2026-07-16"' in by["Daily"]["html"]
    # `section` selects markdown content; it is not a layout group. Leaking it into the
    # response used to insert a full-width heading between two span-6 brief cards.
    assert "section" not in by["Weekly"]


def test_dataset_box_layout_section_is_explicit(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Grouped\n"
        "          view: table\n"
        "          dataset: {dir: entities}\n"
        "          layout_section: Attention\n"
        "          columns: [{field: title, label: Actor}]\n",
        encoding="utf-8")
    _mk(tmp_path, "entities/alpha.md", "type: actor\n")
    m = _load(tmp_path, monkeypatch)
    box = m.api_tab("t")["boxes"][0]
    assert box["layout_section"] == "Attention"
    assert "section" not in box


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


def test_doc_summary_extracts_named_section_and_links_full_page(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - {title: Weekly, view: doc-summary, section: 'Themes at a glance', "
        "dir: briefings, glob: 'weekly-*'}\n", encoding="utf-8")
    p = tmp_path / "wiki" / "briefings" / "weekly-2026-07-14.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: briefing\ntitle: Weekly Brief\n---\n# Weekly Brief\n\n"
                 "## Themes at a glance\n\n- Rising: AI.\n- Flat: ransomware.\n\n"
                 "## News Pulse\n\n" + ("Long detail. " * 200), encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Rising: AI" in html and "Flat: ransomware" in html
    assert "Long detail" not in html
    assert 'data-page="briefings/weekly-2026-07-14"' in html
    assert "Read full brief: Weekly Brief" in html


def test_doc_summary_uses_first_available_ordered_section(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - {title: Weekly, view: doc-summary, "
        "section: ['Themes at a glance', 'Executive summary'], "
        "dir: briefings, glob: 'weekly-*'}\n", encoding="utf-8")
    p = tmp_path / "wiki" / "briefings" / "weekly-2026-08-03.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: briefing\ntitle: Weekly Brief\n---\n"
                 "## Executive summary\n\nCurrent-format summary.\n\n"
                 "## Cross-signal findings\n\nLong detail.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Current-format summary" in html
    assert "Long detail" not in html
    assert "Summary section unavailable" not in html


def test_doc_summary_section_stops_at_thematic_break(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    body = "## Summary\n\nOne paragraph.\n\n---\n\n### Detail\nShould not render."
    assert m._markdown_section(body, "Summary") == "One paragraph."


def test_doc_summary_fallback_is_bounded_and_explicit(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - {title: Daily, view: doc-summary, section: Summary, max_chars: 240, "
        "dir: briefings, glob: 'daily-*'}\n", encoding="utf-8")
    p = tmp_path / "wiki" / "briefings" / "daily-2026-07-15.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: briefing\ntitle: Daily\n---\n# Daily\n\n" + ("Detail sentence. " * 100),
                 encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "Summary section unavailable" in html and "Read full brief" in html
    assert len(html) < 1200


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


def test_unmapped_values_are_demoted_below_mapped(tmp_path, monkeypatch):
    """okengine#259: with a `labels:` vocabulary configured, sanctioned (mapped) values rank ABOVE
    unmapped drift, so a HIGH-count unmapped value is demoted out of the ranked top-N instead of
    occupying a slot (the 'China ⚠ duplicate pollutes Top origins' class). Nothing is hidden — it
    just loses the ranking contest to real categories."""
    m = _load(tmp_path, monkeypatch)
    box = {"group_by": "origin", "limit": 2,
           "labels": {"cn": "China", "ru": "Russia"}}
    # the unmapped 'cn-dup' has the HIGHEST count; the mapped cn/ru have lower counts.
    rows = ([{"origin": "cn-dup"}] * 50) + ([{"origin": "cn"}] * 10) + ([{"origin": "ru"}] * 5)
    labels_shown = [l for l, _v, _um, _k in m._ds_pairs(box, rows)]
    # top-2 are the mapped China/Russia despite cn-dup's higher count; cn-dup is demoted past the cap
    assert labels_shown == ["China", "Russia"], labels_shown
    # with no labels map, plain most-common ordering is unchanged (cn-dup wins on count)
    top = m._ds_pairs({"group_by": "origin", "limit": 1}, rows)[0]
    assert top[0] == "cn-dup", top


def test_group_aliases_merge_duplicate_display_buckets_and_drill_both(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Origins\n          view: bars\n          dataset: {dir: entities}\n"
        "          group_by: origin\n          aliases: {China: CN}\n          labels: {CN: China}\n",
        encoding="utf-8")
    _mk(tmp_path, "entities/a.md", "type: actor\norigin: CN\n")
    _mk(tmp_path, "entities/b.md", "type: actor\norigin: China\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert html.count('class="bl"') == 1 and "China" in html and ">2<" in html
    assert m.api_drill("t", 0, value="CN")["count"] == 2


def test_numeric_table_column_marks_legacy_non_numeric_value(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: P\n          view: table\n          dataset: {dir: predictions}\n"
        "          columns: [{field: confidence, label: Confidence, numeric: true}]\n",
        encoding="utf-8")
    _mk(tmp_path, "predictions/a.md", "type: prediction\nconfidence: 0.7\n")
    _mk(tmp_path, "predictions/b.md", "type: prediction\nconfidence: medium-high\n")
    m = _load(tmp_path, monkeypatch)
    html = m.api_tab("t")["boxes"][0]["html"]
    assert "0.7" in html and "⚠ medium-high" in html and "invalid-value" in html


def test_table_column_value_labels_and_semantic_meta(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Recent\n          view: table\n          limit: 1\n"
        "          dataset: {dir: events}\n"
        "          sort: {field: state, desc: false}\n"
        "          columns: [{field: state, label: State, labels: {new: Newly observed}}]\n"
        "        - title: Origins\n          view: bars\n          limit: 1\n"
        "          dataset: {dir: events}\n          group_by: origin\n"
        "          meta_template: 'Top {groups} origins across {total} observations'\n",
        encoding="utf-8")
    _mk(tmp_path, "events/a.md", "state: new\norigin: CN\n")
    _mk(tmp_path, "events/b.md", "state: old\norigin: US\n")
    m = _load(tmp_path, monkeypatch)
    by = {b["title"]: b for b in m.api_tab("t")["boxes"]}
    assert "Newly observed" in by["Recent"]["html"]
    assert by["Recent"]["meta"] == "showing 1 of 2 records"
    assert by["Origins"]["meta"] == "Top 1 origins across 2 observations"


def test_partial_period_cards_suppress_misleading_direction(vault):
    rows = vault._ds_rows({"dir": "trends", "has": ["report_theme"]})
    html = vault._v_cards({"comparison": "partial-period"}, rows)
    assert "partial period" in html and "▲ rising" not in html
    assert 'title="comparison: partial-period"' in html


def test_ytd_and_full_period_cards_expose_their_analytical_clock(vault):
    rows = vault._ds_rows({"dir": "trends", "has": ["report_theme"]})
    rows[0].update({"comparison": "ytd", "comparison_as_of": "07-15",
                    "count_ytd_by_year": {"2025": 2, "2026": 3}})
    html = vault._v_cards({}, rows)
    assert "YTD through 07-15" in html and 'title="comparison: ytd"' in html
    assert "2025: 2" in html and "2026: 3" in html
    rows[0].pop("comparison")
    html = vault._v_cards({"comparison": "full-period"}, rows)
    assert "full-period comparison" in html and "▲ rising" in html


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


def test_link_page_label_field_shows_linked_name(tmp_path, monkeypatch):
    """okengine#259: a group_by bars box with link_page.label_field displays the LINKED page's
    field (an ATT&CK technique id -> its title "Ingress Tool Transfer") instead of the opaque id;
    the drill still resolves the technique page."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Top techniques\n          view: bars\n          dataset: {dir: entities, type: actor}\n"
        "          group_by: techniques\n"
        "          link_page: {dir: techniques, by: id, label_field: title}\n", encoding="utf-8")
    _mk(tmp_path, "entities/a/a1.md", "type: actor\ntechniques: [T1105]\n")
    _mk(tmp_path, "entities/a/a2.md", "type: actor\ntechniques: [T1105]\n")
    _mk(tmp_path, "techniques/t/T1105.md", "type: technique\nid: T1105\ntitle: Ingress Tool Transfer\n")
    m = _load(tmp_path, monkeypatch)
    box = {"group_by": "techniques", "link_page": {"dir": "techniques", "by": "id", "label_field": "title"}}
    assert m._link_page_map(box)["T1105"]["label"] == "Ingress Tool Transfer"
    html = {b["title"]: b for b in m.api_tab("t")["boxes"]}["Top techniques"]["html"]
    assert "Ingress Tool Transfer" in html          # human name is shown
    assert '<span class="bl">T1105' not in html      # raw id is NOT the visible label
    assert "T1105" in html                            # drill still resolves the technique page


def test_link_page_without_label_field_keeps_raw_value(tmp_path, monkeypatch):
    """Back-compat: link_page WITHOUT label_field still labels bars with the raw group value."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Top techniques\n          view: bars\n          dataset: {dir: entities, type: actor}\n"
        "          group_by: techniques\n          link_page: {dir: techniques, by: id}\n", encoding="utf-8")
    _mk(tmp_path, "entities/a/a1.md", "type: actor\ntechniques: [T1105]\n")
    _mk(tmp_path, "techniques/t/T1105.md", "type: technique\nid: T1105\ntitle: Ingress Tool Transfer\n")
    m = _load(tmp_path, monkeypatch)
    html = {b["title"]: b for b in m.api_tab("t")["boxes"]}["Top techniques"]["html"]
    assert '<span class="bl">T1105' in html and "Ingress Tool Transfer" not in html


def test_bucket_unmapped_collapses_drift_and_drills_offenders(tmp_path, monkeypatch):
    """okengine#259 Rec 4/10: `bucket_unmapped` collapses every value outside the labels vocabulary
    into ONE 'unmapped (N)' row after the sanctioned top-N — so raw NAICS codes / a 'China ⚠'
    near-duplicate / free-text stop each occupying a real-category slot. The bucket drills (via the
    sentinel key) to the offender pages; the degraded-card `unmapped` list is not double-populated."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: By sector\n          view: bars\n          dataset: {dir: incidents}\n"
        "          group_by: industry\n          bucket_unmapped: true\n"
        "          labels: {'622110': Hospitals, '541512': IT}\n", encoding="utf-8")
    _mk(tmp_path, "incidents/i1.md", "type: incident\nindustry: '622110'\n")
    _mk(tmp_path, "incidents/i2.md", "type: incident\nindustry: '541512'\n")
    _mk(tmp_path, "incidents/i3.md", "type: incident\nindustry: '923120'\n")   # unmapped
    _mk(tmp_path, "incidents/i4.md", "type: incident\nindustry: '92'\n")       # unmapped
    _mk(tmp_path, "incidents/i5.md", "type: incident\nindustry: '923120'\n")   # unmapped (dup value)
    m = _load(tmp_path, monkeypatch)

    box = {"group_by": "industry", "bucket_unmapped": True,
           "labels": {"622110": "Hospitals", "541512": "IT"}}
    pairs = m._ds_pairs(box, m._ds_rows({"dir": "incidents"}))
    labels = [l for l, _v, _um, _k in pairs]
    assert "Hospitals" in labels and "IT" in labels          # sanctioned categories kept
    assert "923120" not in labels and "92" not in labels     # individual drift NOT shown
    bucket = [(l, v, um, k) for l, v, um, k in pairs if k == m._UNMAPPED_KEY]
    assert len(bucket) == 1
    l, v, um, _k = bucket[0]
    assert um is True and v == 3 and l == "unmapped (2)"      # 3 pages across 2 distinct values

    # the bucket drills to the offender pages (group value outside the vocabulary)
    d = m.api_drill("t", 0, value=m._UNMAPPED_KEY)
    assert d["count"] == 3
    assert {p["path"] for p in d["pages"]} == {"incidents/i3", "incidents/i4", "incidents/i5"}
    assert "unmapped" in d["title"]

    # the rendered card shows the collapsed bar, not the raw codes, and no redundant `unmapped` list
    sect = {b["title"]: b for b in m.api_tab("t")["boxes"]}["By sector"]
    vis = re.sub(r'data-dval="[^"]*"', '', sect["html"])
    assert "unmapped (2)" in vis and "923120" not in vis and "'92'" not in vis
    assert "unmapped" not in sect                            # not double-surfaced on the card


def test_a_supporting_assessment_always_states_at_least_its_status(tmp_path, monkeypatch):
    """A supporting record is shown to explain WHY the subject sits in that bucket, so it has to say
    something. This is also the invariant that keeps the empty-facts case out of reach: the record
    is selected by its `status`, so a selected record always has one to state."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [t]\n  tab_defs:\n    t:\n      label: T\n      boxes:\n"
        "        - title: Top assessed origins\n          view: bars\n"
        "          dataset: {dir: entities, type: actor}\n          limit: 6\n"
        "          assessment:\n            kind: actor-country-linkage\n"
        "            value_field: assessed_value\n"
        "            labels: {CN: China}\n",
        encoding="utf-8")
    _mk(tmp_path, "entities/b/beta.md", "type: actor\ntitle: Beta\n")
    # everything optional omitted: no epistemic_status, no confidence_band, no as_of
    _mk(tmp_path, "assessments/b/bare.md",
        "type: assessment\ntitle: Bare\nassessment_kind: actor-country-linkage\n"
        "subject: entities/b/beta\nstatus: active\nassessed_value: CN\n"
        "last_updated: 2026-07-17T12:00:00Z\n")
    m = _load(tmp_path, monkeypatch)
    drill = m.api_drill("t", 0, value="CN")
    supporting = next(p for p in drill["pages"] if p["path"] == "assessments/b/bare")
    assert supporting["type"] == "assessment"
    assert supporting["facts"] == [{"label": "Status", "value": "active"},
                                   {"label": "Evidence state", "value": "assessed"}], supporting
