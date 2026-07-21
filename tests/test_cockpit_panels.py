"""Cockpit UI extension panels (okengine#160, ported from the reader): /api/page must
carry a render-ready `panel` — a GENERATED page's self-declared `panel:` frontmatter
(e.g. viz's two-axis wardley map, nodes included) passes through verbatim; a type bound
in VAULT/.okengine/reader-panels.json yields a `fields` panel; plain pages get None.
Regression for the wardley-maps-render-in-the-reader-but-not-the-cockpit gap."""
import importlib.util
import json
import sys
import time
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


def test_load_dir_serves_stale_while_revalidating(tmp_path, monkeypatch):
    """The 'overview slow to load' fix: a request that finds the dir cache STALE must serve the
    stale rows immediately and rescan in the BACKGROUND — never block on the multi-thousand-file
    scan synchronously (which is what made the overview tab take ~8s every time the TTL expired)."""
    m = _load(tmp_path, monkeypatch)
    m._DIR_CACHE["entities"] = (time.monotonic() - m._DIR_TTL - 1, [{"_name": "old"}])
    calls = []
    monkeypatch.setattr(m, "_scan_dir_meta", lambda sub: calls.append(sub) or [{"_name": "new"}])
    got = m._load_dir("entities")
    assert got == [{"_name": "old"}], "stale request did not serve the cached rows immediately"
    for _ in range(100):                              # let the daemon refresh land
        if m._DIR_CACHE["entities"][1] == [{"_name": "new"}]:
            break
        time.sleep(0.02)
    assert m._DIR_CACHE["entities"][1] == [{"_name": "new"}], "background rescan never swapped in"
    assert calls == ["entities"], f"expected exactly one background rescan, got {calls}"


def test_load_dir_cold_miss_scans_synchronously(tmp_path, monkeypatch):
    """A dir never loaded before (no cache at all) must return real rows on the first call — the
    startup warmer covers configured datasets, but an ad-hoc dir still resolves."""
    m = _load(tmp_path, monkeypatch)
    m._DIR_CACHE.clear()
    monkeypatch.setattr(m, "_scan_dir_meta", lambda sub: [{"_name": "x"}])
    assert m._load_dir("adhoc") == [{"_name": "x"}]


def test_warm_tab_datasets_prepopulates_configured_dirs(tmp_path, monkeypatch):
    """The startup warmer must pre-scan every namespace the tabs/streams read, so the first tab
    request is already warm."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n"
        "  tab_defs:\n"
        "    overview:\n"
        "      label: Overview\n"
        "      boxes:\n"
        "        - {title: Actors, view: table, dataset: {dir: entities, type: actor}}\n"
        "  streams:\n"
        "    - {key: briefs, label: Briefs, dir: briefings}\n",
        encoding="utf-8",
    )
    m = _load(tmp_path, monkeypatch)
    m._DIR_CACHE.clear()
    scanned = []
    monkeypatch.setattr(m, "_scan_dir_meta", lambda sub: scanned.append(sub) or [])
    m._warm_tab_datasets()
    assert set(scanned) == {"entities", "briefings"}, f"warmer scanned {scanned}"


def test_initial_warmer_scans_only_landing_tab(tmp_path, monkeypatch):
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  tabs: [overview, che]\n  tab_defs:\n"
        "    overview:\n      boxes:\n"
        "        - {title: Actors, dataset: {dir: entities}}\n"
        "        - {title: Brief, view: doc, dir: dashboards}\n"
        "    che:\n      boxes:\n"
        "        - {title: Ledger, dataset: {dir: assessments}}\n",
        encoding="utf-8",
    )
    m = _load(tmp_path, monkeypatch)
    m._DIR_CACHE.clear()
    scanned = []
    monkeypatch.setattr(m, "_scan_dir_meta", lambda sub: scanned.append(sub) or [])
    m._warm_initial_tab_datasets()
    assert set(scanned) == {"entities", "dashboards"}
    assert "assessments" not in scanned


def test_ds_sorted_date_fields_honor_direction(tmp_path, monkeypatch):
    """Live incident #2 (Knowledge-gaps box): ISO dates aren't floatable, so a date-sorted box lives
    entirely in the non-numeric bucket — the first junk-last fix sorted that bucket ascending
    unconditionally and `sort: {field: created, desc: true}` showed OLDEST gaps first. Direction
    must apply WITHIN the bucket. YAML yields date objects (unquoted) and strings (quoted) — both
    must order consistently."""
    import datetime
    m = _load(tmp_path, monkeypatch)
    rows = [{"_name": "old", "created": "2026-07-07"},
            {"_name": "mid", "created": datetime.date(2026, 7, 8)},   # unquoted YAML -> date object
            {"_name": "new", "created": "2026-07-09T14:00:00Z"}]
    newest_first = m._ds_sorted(rows, {"field": "created", "desc": True})
    assert [r["_name"] for r in newest_first] == ["new", "mid", "old"]
    oldest_first = m._ds_sorted(rows, {"field": "created"})
    assert [r["_name"] for r in oldest_first] == ["old", "mid", "new"]


def test_ds_sorted_junk_ranks_last_regardless_of_direction(tmp_path, monkeypatch):
    """Live incident (Most-active table): one actor page with `recent_reports:` hand-set to a LIST
    of source paths took the #1 slot, because the desc sort's reverse=True flipped the
    (numeric, junk) buckets too. Junk must rank LAST in both directions."""
    m = _load(tmp_path, monkeypatch)
    rows = [{"_name": "a", "recent_reports": 15},
            {"_name": "junk", "recent_reports": ["sources/2026/07/x"]},
            {"_name": "b", "recent_reports": 7},
            {"_name": "junk2", "recent_reports": "sources/2026/07/y"},
            {"_name": "c", "recent_reports": 26}]
    desc = m._ds_sorted(rows, {"field": "recent_reports", "desc": True, "require": True})
    assert [r["_name"] for r in desc][:3] == ["c", "a", "b"], [r["_name"] for r in desc]
    assert {r["_name"] for r in desc[3:]} == {"junk", "junk2"}   # junk below every number; order among junk is arbitrary
    asc = m._ds_sorted(rows, {"field": "recent_reports"})
    assert [r["_name"] for r in asc][:3] == ["b", "a", "c"]
    assert {r["_name"] for r in asc[3:]} == {"junk", "junk2"}


def test_curated_other_group_includes_nested_dashboards(tmp_path, monkeypatch):  # invariant-audit M7
    """With a curated dashboards config, the 'Other' catch-all must surface a NESTED, un-curated
    dashboard (dashboards/<ns>/*.md) — a flat glob left extension-written nested dashboards invisible
    in the grid entirely (never curated, never caught)."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n"
        "  dashboards:\n"
        "    - group: Today\n"
        "      items:\n"
        "        - {path: dashboards/curated-top, title: Curated}\n",
        encoding="utf-8",
    )
    dash = tmp_path / "wiki" / "dashboards"
    (dash / "competitive").mkdir(parents=True)
    (dash / "curated-top.md").write_text("---\ntitle: Curated\n---\n# t\n", encoding="utf-8")
    (dash / "competitive" / "nested.md").write_text("---\ntitle: Nested\n---\n# n\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    groups = m.api_dashboards()["groups"]
    other = next((g for g in groups if g["group"] == "Other"), None)
    assert other is not None, groups
    paths = {it["path"] for it in other["items"]}
    assert "dashboards/competitive/nested" in paths        # nested + un-curated -> visible in Other
    assert "dashboards/curated-top" not in paths           # curated -> not duplicated into Other


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


def test_page_overlay_surfaces_facts_conflicts_and_observations(tmp_path, monkeypatch):
    """Richer page overlay (ported from the reader): api_page must carry the fact panel (`meta`
    surfaced intel + `meta_aux` collapsed record-keeping), the multi-source `conflicts` view tagged
    with Admiralty reliability from schema.yaml source_registry, and `observations` resolved by the
    page's canonical slug. Turns a clicked page from rendered markdown into a typed intel object."""
    (tmp_path / "schema.yaml").write_text(
        "source_registry:\n  Vendor A: {reliability: A}\n  Rando Blog: {reliability: E}\n",
        encoding="utf-8")
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    (ent / "apt-x.md").write_text(
        "---\ntype: actor\nname: APT-X\naliases: [Thief Libra]\norigin: Iran\n"
        "refs: ['https://attack.mitre.org/groups/G0001']\n"
        "sources: [sources/2026/s1]\nmaintained_by: [okpack-cti]\nupdated: 2026-07-01\n"
        "conflicts:\n"
        "  - field: origin\n    headline: Iran\n"
        "    values:\n"
        "      - {value: Iran, sources: [Vendor A]}\n"
        "      - {value: Russia, sources: [Rando Blog]}\n"
        "---\nBody.\n", encoding="utf-8")
    src = tmp_path / "wiki" / "sources" / "2026"
    src.mkdir(parents=True)
    (src / "s1.md").write_text("---\ntype: source\ntitle: S1\n---\n", encoding="utf-8")   # ref target must exist to linkify
    obs = tmp_path / "wiki" / "observations"
    obs.mkdir(parents=True)
    (obs / "o1.md").write_text(
        "---\ntype: observation\ncanonical: apt-x\nsource: Vendor A\n---\nseen\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="entities/a/apt-x")
    # fact panel: surfaced intel in `meta`, record-keeping in `meta_aux`
    meta = {row["label"]: row["values"] for row in d["meta"]}
    assert "Aliases" in meta and meta["Aliases"][0]["text"] == "Thief Libra"
    assert meta["Origin"][0]["text"] == "Iran"
    assert meta["Refs"][0]["url"].endswith("/groups/G0001")             # external url chip
    assert "Sources" not in meta                                        # sources -> the Evidence section
    assert d["citations"][0]["page"] == "sources/2026/s1"               # rendered there as a citation
    aux = {row["label"] for row in d["meta_aux"]}
    assert "Maintained by" in aux and "Updated" in aux and "Origin" not in aux
    # conflicts: per-field values tagged with reliability + rank, headline flagged
    c = d["conflicts"][0]
    assert c["field"] == "origin" and c["headline"] == "Iran"
    iran = next(v for v in c["values"] if v["value"] == "Iran")
    assert iran["is_headline"] and iran["sources"][0]["reliability"] == "A" and iran["rank"] == 5
    russia = next(v for v in c["values"] if v["value"] == "Russia")
    assert russia["sources"][0]["reliability"] == "E" and russia["rank"] == 1
    # observations resolved by canonical slug
    assert d["observations"] and d["observations"][0]["source"] == "Vendor A"


def test_box_engine_missing_filter_and_list_group_by(tmp_path, monkeypatch):
    """Engine box capability for work-surface tabs: a `missing:` dataset filter selects field-ABSENT
    pages (e.g. unsourced actors), and group_by EXPLODES list fields (an actor targeting
    ['gov','finance'] counts toward both buckets)."""
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    (ent / "sourced.md").write_text(
        "---\ntype: actor\nname: A\nsources: [s1]\ntarget_sector: [gov, finance]\n---\n", encoding="utf-8")
    (ent / "unsourced.md").write_text(
        "---\ntype: actor\nname: B\ntarget_sector: [finance]\n---\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    rows = m._ds_rows({"dir": "entities", "type": "actor", "missing": ["sources"]})
    assert {r.get("name") for r in rows} == {"B"}                     # only the unsourced actor
    all_actors = m._ds_rows({"dir": "entities", "type": "actor"})
    counts = {k: v for (l, v, um, k) in m._ds_pairs({"group_by": "target_sector"}, all_actors)}
    assert counts == {"gov": 1, "finance": 2}                        # list field exploded per element
    assert m._gb_values(["gov", "finance"]) == ["gov", "finance"] and m._gb_values("KP") == ["KP"]
    assert m._gb_values(None) == []


def test_evidence_section_grades_and_dates_citations(tmp_path, monkeypatch):
    """Evidence section (#4): a page's cited sources render as graded, dated citations — Admiralty
    reliability from schema.yaml source_registry, recency from a source page's date, an internal link
    when the source resolves to a page. `sources` also drops from the fact panel (Evidence covers it)."""
    (tmp_path / "schema.yaml").write_text(
        "source_registry:\n  MITRE ATT&CK: {reliability: A}\n  MISP galaxy: {reliability: B}\n",
        encoding="utf-8")
    src = tmp_path / "wiki" / "sources" / "2026"
    src.mkdir(parents=True)
    (src / "rep1.md").write_text(
        "---\ntype: source\ntitle: Rep1\npublished: 2026-07-01\nreliability: A\n---\n",
        encoding="utf-8")
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    (ent / "apt.md").write_text(
        "---\ntype: actor\nname: APT\nsources: ['MITRE ATT&CK', 'MISP galaxy', sources/2026/rep1]\n---\nb\n",
        encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="entities/a/apt")
    cites = {c["name"]: c for c in d["citations"]}
    assert cites["MITRE ATT&CK"]["reliability"] == "A"          # graded from the registry
    assert cites["MISP galaxy"]["reliability"] == "B"
    assert cites["sources/2026/rep1"]["page"] == "sources/2026/rep1"   # internal link
    assert cites["sources/2026/rep1"]["date"] == "2026-07-01"          # recency from the source page
    assert cites["sources/2026/rep1"]["reliability"] == "A"           # specific reviewed record
    assert "Sources" not in {r["label"] for r in d["meta"]}    # dropped from the fact panel


def test_type_aware_profile_orders_fact_panel(tmp_path, monkeypatch):
    """Type-aware profile order (#2): a pack declares `cockpit.profiles: {<type>: [field order]}`;
    the fact panel then renders that type's declared fields first in that order, remaining fields in
    frontmatter order. Makes an actor page read as a curated profile, not raw frontmatter."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  profiles:\n    actor: [aliases, origin_country, techniques]\n", encoding="utf-8")
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    # frontmatter deliberately out of profile order; an undeclared field (attack_id) trails
    (ent / "apt.md").write_text(
        "---\ntype: actor\nname: APT\nattack_id: G0001\ntechniques: [T1059]\n"
        "aliases: [Alt]\norigin_country: Iran\n---\nb\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="entities/a/apt")
    labels = [r["label"] for r in d["meta"]]
    assert labels == ["Aliases", "Origin country", "Techniques"]       # ONLY declared fields, in order
    aux = {r["label"] for r in d["meta_aux"]}
    assert "Attack id" in aux and "Attack id" not in labels            # undeclared -> Record details
    assert d["profiled"] is True                                       # profiled type -> fact panel leads
    # a type with no profile falls back to frontmatter order (no crash), not profile-first
    (ent / "plain.md").write_text("---\ntype: concept\nname: C\nfoo: 1\nbar: 2\n---\nb\n", encoding="utf-8")
    pd = m.api_page(path="entities/a/plain")
    assert [r["label"] for r in pd["meta"]] == ["Foo", "Bar"] and pd["profiled"] is False


def test_related_rail_groups_backlinks_by_type(tmp_path, monkeypatch):
    """Relationship rail (#3): api_backlinks groups referrers by their namespace (the OKF type
    bucket) with true per-group counts, most-connected first, items capped per group. Turns a flat
    'what links here' into 'predictions about it / findings involving it / related entities'."""
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    # stub the backlink graph: 2 predictions + 1 finding reference entities/a/apt-x
    monkeypatch.setattr(m, "_load_backlinks", lambda blocking=False: {
        "entities/a/apt-x": [
            {"key": "predictions/2026/q3/p1", "title": "P1"},
            {"key": "predictions/2026/q3/p2", "title": "P2"},
            {"key": "findings/f1", "title": "F1"},
        ]})
    d = m.api_backlinks(path="entities/a/apt-x")
    assert d["count"] == 3
    g = {x["ns"]: x for x in d["groups"]}
    assert g["predictions"]["count"] == 2 and g["findings"]["count"] == 1
    assert d["groups"][0]["ns"] == "predictions"          # most-connected first
    assert g["predictions"]["label"] == "Predictions"     # humanized


def test_fact_panel_drops_needs_review_boolean(tmp_path, monkeypatch):
    """needs_review dedup: the flag is covered by the quality badge + trust strip, so the raw
    boolean no longer clutters the fact panel (it stays in `quality`/`provenance`)."""
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    (ent / "x.md").write_text("---\ntype: actor\nname: X\naliases: [Y]\nneeds_review: true\n---\nb\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="entities/a/x")
    labels = {r["label"] for r in d["meta"]} | {r["label"] for r in d["meta_aux"]}
    assert "Needs review" not in labels                   # dropped from the fact panel
    assert d["needs_review"] is True                      # still carried for the badge/strip
    assert any(b["label"] == "needs review" for b in d["quality"])


def test_page_quality_badges_flag_health_problems(tmp_path, monkeypatch):
    """Page quality/status badges (#5): api_page carries a problem-only `quality` row computed from
    data already present — missing required fields (schema-declared), no sources / ungrounded,
    needs-review, conflicts, staleness, thin. A clean, well-sourced page yields no badges."""
    monkeypatch.setenv("OKENGINE_COCKPIT_STALE_DAYS", "90")
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type, aliases]}\n  source: {required: [type]}\n",
        encoding="utf-8")
    ent = tmp_path / "wiki" / "entities" / "a"
    ent.mkdir(parents=True)
    # thin, unsourced, needs-review, stale, missing the required `aliases`
    (ent / "stub.md").write_text(
        "---\ntype: actor\nname: Stub\nneeds_review: true\nupdated: 2020-01-01\n---\nx\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    q = {b["label"]: b["level"] for b in m.api_page(path="entities/a/stub")["quality"]}
    assert q.get("missing aliases") == "bad"           # schema-required field absent
    assert q.get("no sources") == "bad"
    assert q.get("needs review") == "warn"
    assert any(k.startswith("stale ") for k in q)       # updated 2020 >> 90d
    assert "thin" in q
    # a clean, sourced, recent, complete page -> no badges
    src = tmp_path / "wiki" / "sources" / "2026"
    src.mkdir(parents=True)
    (src / "s1.md").write_text("---\ntype: source\ntitle: S1\n---\nbody\n", encoding="utf-8")
    (ent / "good.md").write_text(
        "---\ntype: actor\nname: Good\naliases: [Alt]\nsources: [sources/2026/s1]\n"
        f"updated: {m.TODAY().isoformat()}\n---\n" + ("Substantial prose. " * 20) + "\n", encoding="utf-8")
    assert m.api_page(path="entities/a/good")["quality"] == []


def test_source_record_uses_upstream_capture_as_grounding(tmp_path, monkeypatch):
    """A source is an evidence record, not a knowledge claim that must cite another source page."""
    (tmp_path / "schema.yaml").write_text(
        "types:\n  source: {required: [type, published]}\n", encoding="utf-8")
    sources = tmp_path / "wiki" / "sources" / "2026" / "07"
    sources.mkdir(parents=True)
    (sources / "report.md").write_text(
        "---\ntype: source\ntitle: Report\npublished: 2026-07-13\n"
        "url: https://example.test/report\nraw: raw/feed/report.md\n---\n" +
        ("Substantial captured source text. " * 12) + "\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    labels = {badge["label"] for badge in m.api_page(path="sources/2026/07/report")["quality"]}
    assert "no sources" not in labels
    assert "ungrounded" not in labels


def test_priority_candidate_leads_are_provenance_not_support(tmp_path, monkeypatch):
    sources = tmp_path / "wiki/sources"
    sources.mkdir(parents=True)
    (sources / "report.md").write_text("---\ntype: source\ntitle: Report\n---\nBody.\n")
    priorities = tmp_path / "wiki/hypotheses/priorities"
    priorities.mkdir(parents=True)
    (priorities / "one.md").write_text(
        "---\ntype: actor-assessment-priority\ntitle: Priority\n"
        "candidate_evidence:\n- artifact: sources/report\n  evidence_role: candidate-lead\n"
        "  artifact_digest: sha256:" + "a" * 64 + "\n---\n" + ("Priority detail. " * 30))
    m = _load(tmp_path, monkeypatch)
    result = m.api_page(path="hypotheses/priorities/one")
    labels = {row["label"] for row in result["quality"]}
    assert "no sources" not in labels
    assert "1 candidate lead" in labels
    assert result["provenance"]["candidate_pages"] == 1
    assert result["citations"] == []


def test_ops_auto_appends_before_browse(tmp_path, monkeypatch):
    """Ops is inserted BEFORE `browse` so browse stays at the tail (next to Chat), per UI feedback."""
    (tmp_path / "wiki" / "operational").mkdir(parents=True)
    (tmp_path / "wiki" / "operational" / "kb-health-snapshots.md").write_text(
        "---\ntitle: KB\n---\nx\n", encoding="utf-8")
    (tmp_path / "schema.yaml").write_text("cockpit:\n  tabs: [overview, browse]\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    tabs = m.api_config()["tabs"]
    assert "ops" in tabs and tabs.index("ops") < tabs.index("browse")   # ops before browse
    assert tabs[-1] == "browse"                                          # browse stays last


def test_ops_tab_absent_when_no_operational_pages(tmp_path, monkeypatch):
    """Ops is content-gated: a vault the engine health crons haven't populated shows no Ops tab
    (no empty nav entry)."""
    (tmp_path / "wiki" / "briefings").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    assert m.api_ops()["groups"] == []
    assert "ops" not in m.api_config()["tabs"]


def test_chat_grounding_contract_hides_the_machinery(tmp_path, monkeypatch):
    """The server-prepended chat contract must forbid naming the machinery (Hermes / model / tools)
    and signing off as an 'agent', while still allowing vault + source references. Regression: a
    chat report signed off "GENERATED BY HERMES AGENT"."""
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    contract = m._AGENT_SYSTEM.lower()
    assert "never mention hermes" in contract
    assert "model or model provider" in contract and "tools/functions you use" in contract
    assert "sign a reply" in contract          # no "GENERATED BY … AGENT" sign-off
    assert "the vault" in contract             # vault/source references stay allowed
    assert "most recent pages" in contract     # recency discipline
    assert "linked title" in contract          # cite [Title](path), not raw paths
    assert "comparison tables" in contract     # on-demand report craft
    assert "self-contained document" in contract   # a report excludes search/pull narration
    assert "do not narrate" in contract            # one acknowledgement line, not per-round chatter


def test_export_flattens_internal_links_keeps_external(tmp_path, monkeypatch):
    """Export must drop internal vault links (they resolve only in-app) to text, keep external
    http links + images. Regression: the agent's `[Title](path)` citations became dead links in
    exported md/docx/pdf."""
    m = _load(tmp_path, monkeypatch)
    s = "See the [APT41 profile](entities/a/apt41) and [MITRE](https://attack.mitre.org/g). ![x](img/p.png)"
    out = m._deref_local_links(s)
    assert "APT41 profile" in out and "(entities/a/apt41)" not in out   # internal flattened
    assert "[MITRE](https://attack.mitre.org/g)" in out                  # external kept
    assert "![x](img/p.png)" in out                                      # image kept
    # the chat-report cleaner adds a title and flattens
    clean = m._clean_chat_markdown("[APT41](entities/a/apt41) did X.", "Chinese Threat")
    assert clean.startswith("# Chinese Threat") and "APT41 did X." in clean and "(entities/a/apt41)" not in clean


def test_report_export_strips_progress_narration(tmp_path, monkeypatch):
    """A local model prefaces reports with progress narration ('Checking… now. Pulling…'); the export
    drops it when it's delimited by a `---`/heading, but never removes real content otherwise."""
    m = _load(tmp_path, monkeypatch)
    narrated = ("Checking the vault for LockBit now.\nPulling the entity page and sources now.\n"
                "Retrieving the remaining pages for completeness.\n\n---\n\n"
                "**Situation Report: LockBit**\n\nLockBit is a RaaS operation.")
    out = m._strip_report_preamble(narrated)
    assert out.startswith("**Situation Report: LockBit**") and "Checking the vault" not in out
    # heading-delimited narration also stripped
    out2 = m._strip_report_preamble("Searching now.\nGood leads.\n\n# Report\n\nBody.")
    assert out2.startswith("# Report") and "Searching now" not in out2
    # UNDELIMITED narration (the real failure): 'Found…' / 'Based on the vault, here's what…' run
    # straight into a plain section label — no `---`/heading between them — must still be stripped.
    real = ("Checking the vault for information on Dark Hotel…\n"
            "Found a page for this actor. Pulling the full details…\n"
            "Based on the vault, here's what we know about Dark Hotel:\n\n"
            "## Overview\n\nDarkhotel is a suspected South Korean threat actor.")
    out3 = m._strip_report_preamble(real)
    assert out3.startswith("## Overview"), out3
    assert "Checking the vault" not in out3 and "Found a page" not in out3 and "here's what" not in out3
    # NO false strip: prose that merely starts with a plain sentence is untouched
    prose = "LockBit is a ransomware group.\n\nIt targets finance."
    assert m._strip_report_preamble(prose) == prose
    # and an all-narration reply is never emptied (fall back to the original)
    assert m._strip_report_preamble("Checking the vault now.\nPulling pages now.") != ""


def test_chat_export_md_returns_flattened_markdown(tmp_path, monkeypatch):
    """/api/chat_export?fmt=md runs the chat text through the clean pipeline (no pandoc needed for
    md) and returns portable markdown with internal links flattened."""
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient
    (tmp_path / "wiki").mkdir(parents=True)
    m = _load(tmp_path, monkeypatch)
    c = TestClient(m.app)
    r = c.post("/api/chat_export?fmt=md",
               json={"content": "Report on [APT41](entities/a/apt41).", "title": "Threat Report"})
    assert r.status_code == 200
    body = r.text
    assert "# Threat Report" in body and "APT41" in body and "(entities/a/apt41)" not in body
    assert "attachment" in r.headers.get("content-disposition", "")


def test_backtick_wrapped_wikilink_renders_as_link_not_escaped_html(tmp_path, monkeypatch):
    """Agents wrap wikilinks in backticks (`[[x]]`); _linkify then injected the <a> inside an
    inline-code span and markdown escaped it to visible `<a …>` text in the UI. render_md must strip
    the backticks so it becomes a real link. Regression: HTML anchor tags shown as literal text."""
    m = _load(tmp_path, monkeypatch)
    html = m.render_md("The 14 pages linking to `[[concepts/supply-chain-attacks]]` document a surface.")
    assert '<a class="wl" data-page="concepts/supply-chain-attacks">' in html   # a real link
    assert "&lt;a class" not in html and "<code>" not in html                   # not escaped code
    # a genuine code span that isn't a bare wikilink is left alone
    assert "<code>" in m.render_md("call `build(force=True)` to rescan.")
    # export path flattens it to plain text (no code fencing, no dead path)
    clean = m._clean_markdown("---\ntype: concept\n---\nsee `[[concepts/x]]` here", "T")
    assert "concepts/x" not in clean and "`" not in clean.split("\n", 2)[-1]


def test_trend_cards_show_direction_glyph_not_default_arrow(tmp_path, monkeypatch):
    """The card glyph map only knew rising/falling, but trend generators write up/down/flat/emerging —
    so every arrow fell to the default →. Regression: cover both vocabularies so direction shows."""
    m = _load(tmp_path, monkeypatch)
    box = {"view": "cards", "dir_field": "direction"}
    rows = [{"title": "A", "direction": "up"}, {"title": "B", "direction": "down"},
            {"title": "C", "direction": "flat"}, {"title": "D", "direction": "emerging"}]
    html = m._v_cards(box, rows)
    assert "▲ up" in html and "▼ down" in html and "◆ emerging" in html   # real direction glyphs
    assert "→ flat" in html                                               # flat legitimately stays →
    assert html.count("▲") == 1 and html.count("▼") == 1                  # not all the default arrow


def test_render_deck_pdf_graceful_and_cached(tmp_path, monkeypatch):
    """Deck PDF render-on-miss: returns None (caller 404s, never raises) when marp is absent or the
    md is missing; when marp is present it caches by the md's mtime and reuses the render."""
    m = _load(tmp_path, monkeypatch)
    md = tmp_path / "wiki" / "briefings" / "weekly-deck-2026-07-09.md"
    md.parent.mkdir(parents=True)
    md.write_text("---\nmarp: true\n---\n# Deck\n", encoding="utf-8")

    monkeypatch.setattr(m, "_MARP", None)                       # marp not installed
    assert m._render_deck_pdf(md) is None
    assert m._render_deck_pdf(tmp_path / "nope.md") is None     # missing md

    # a stub standing in for `marp … -o OUT` that just writes a PDF to OUT
    stub = tmp_path / "fakemarp"
    stub.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nprintf "%%PDF-1.4" > "$2"\n')
    stub.chmod(0o755)
    monkeypatch.setattr(m, "_MARP", str(stub))
    monkeypatch.setattr(m, "_DECK_CACHE", tmp_path / "cache")
    p1 = m._render_deck_pdf(md)
    assert p1 and p1.is_file() and p1.read_bytes().startswith(b"%PDF")
    assert m._render_deck_pdf(md) == p1                         # reused from cache (same mtime)


def test_group_by_bar_link_page_opens_the_named_page(tmp_path, monkeypatch):
    """A group_by bar whose value NAMES a page (an ATT&CK technique id) opens that page via
    `link_page:` instead of drilling to the members that share it; a value with no page keeps the
    drilldown. Regression: Top-techniques bars linked to an actor list, not the technique."""
    tdir = tmp_path / "wiki" / "techniques" / "t" / "1"
    tdir.mkdir(parents=True)
    (tdir / "T1105.md").write_text(
        "---\nid: T1105\nattack_id: T1105\ntitle: Ingress Tool Transfer\ntype: technique\n---\nbody\n",
        encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    box = {"view": "bars", "group_by": "techniques", "link_page": {"dir": "techniques", "by": "id"},
           "dataset": {"dir": "entities", "type": "actor"}}
    rows = [{"techniques": ["T1105", "T9999"]}, {"techniques": ["T1105"]}]  # T9999 has no page

    lpm = m._link_page_map(box)
    assert lpm.get("T1105", {}).get("path") == "techniques/t/1/T1105"  # resolved to the real page (shards included)
    assert lpm.get("T1105", {}).get("label") == ""       # no label_field configured -> empty label
    assert "T9999" not in lpm                            # no page -> not in the map

    html = m._v_bars(box, rows, drill=("adversaries", 3))
    assert 'data-dpage="techniques/t/1/T1105"' in html   # the T1105 bar opens the technique page
    t1105_row = [seg for seg in html.split('<div class="brow') if "T1105" in seg][0]
    assert "data-dval" not in t1105_row                  # ...and does NOT also carry an actor drill
    assert 'data-dval="T9999"' in html                   # the pageless value falls back to the drill


def test_shape_conflicts_survives_scalar_values(tmp_path, monkeypatch):
    """Same M28 guard as the reader's copy: scalar conflicts.values entries must not 500 the cockpit
    page — the identical unguarded .get() lived here too (invariant-audit M28)."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    fm = {"conflicts": [{"field": "severity", "headline": "high", "values": ["high", "medium"]},
                        {"field": "actor", "headline": "x", "values": "scalar-string"},
                        {"field": "count", "headline": "y", "values": 42}]}   # scalar container too (M28 round-2)
    out = m._shape_conflicts(fm)
    assert isinstance(out, list) and len(out) == 3
    assert all(c["values"] == [] for c in out)
    assert m._shape_conflicts({"conflicts": 42}) == []
    # third container: scalar `sources` inside a well-shaped entry must not crash (round-2 re-verify)
    fm2 = {"conflicts": [{"field": "f", "headline": "h", "values": [{"value": "hi", "sources": 42}]}]}
    assert m._shape_conflicts(fm2)[0]["values"][0]["sources"] == []


def test_stream_dates_recurses_partitioned_dir(tmp_path, monkeypatch):
    """A stream with no `type` (glob/default mode) over a PARTITIONED dir (dates sharded into
    sub-dirs) must still yield dates — glob.glob was non-recursive and returned nothing (M-528)."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  streams:\n    - {key: briefs, label: Briefs, dir: briefings}\n  tabs: [home]\n",
        encoding="utf-8")
    sub = tmp_path / "wiki" / "briefings" / "2026" / "07"
    sub.mkdir(parents=True)
    (sub / "daily-2026-07-10.md").write_text("---\ntype: briefing\n---\nB\n", encoding="utf-8")
    # a RESERVED sub-dir (_archived/) holds retired dated pages — rglob must NOT surface them as live
    # (round-2 re-verify: a leaf-only _ check missed _archived/ because the leaf filename is dated).
    arch = tmp_path / "wiki" / "briefings" / "_archived"
    arch.mkdir(parents=True)
    (arch / "daily-2019-01-01.md").write_text("---\ntype: briefing\n---\nold\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m._stream_dates("briefs") == ["2026-07-10"], "archived pages must not appear as live dates"


def test_stream_pages_type_branch_hides_archived_subdir(tmp_path, monkeypatch):
    """The `type:` stream branch is ALSO recursive — it must drop reserved _archived/ sub-dirs at any
    depth. It kept leaf-only hygiene while only the glob branch got _visible_page (round-3 re-verify);
    a retired page keeps its `type`, so a same-type archived page leaked into /api/streams."""
    (tmp_path / "schema.yaml").write_text(
        "cockpit:\n  streams:\n    - {key: b, label: B, dir: briefs, type: brief}\n  tabs: [home]\n",
        encoding="utf-8")
    (tmp_path / "wiki" / "briefs").mkdir(parents=True)
    (tmp_path / "wiki" / "briefs" / "daily-2026-07-10.md").write_text(
        "---\ntype: brief\n---\nlive\n", encoding="utf-8")
    arch = tmp_path / "wiki" / "briefs" / "_archived" / "2025"
    arch.mkdir(parents=True)
    (arch / "daily-2025-01-01.md").write_text("---\ntype: brief\n---\nretired\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m._stream_dates("b") == ["2026-07-10"], "archived same-type page must not surface"


def test_v_doc_recurses_partitioned_and_reads_subdir(tmp_path, monkeypatch):
    """_v_doc must find AND read the latest doc in a PARTITIONED dir — glob was non-recursive (empty)
    and safe_read(base, basename) 404'd a sub-dir hit, killing the whole tab (M-1513)."""
    sub = tmp_path / "wiki" / "reports" / "2026" / "07"
    sub.mkdir(parents=True)
    (sub / "weekly-2026-07-10.md").write_text("---\ntype: report\n---\n# Weekly\n\nThe body here.\n",
                                              encoding="utf-8")
    # a FLAT, letter-leading, non-dated sibling: a full-PATH sort ranks it above the YYYY/-sharded
    # dated page (letter > digit is false, but a 'zzz' name sorts last only by name, not by path).
    # The winner must be the newest DATED page, sorted by filename date (round-2 re-verify).
    (tmp_path / "wiki" / "reports" / "zzz-planning-notes.md").write_text(
        "---\ntype: report\n---\n# Planning\n\nStale flat note.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    html, stem = m._v_doc({"dir": "reports", "glob": "*"})
    assert stem == "weekly-2026-07-10", "latest DATED doc must win, not a flat/letter-leading sibling"
    assert "The body here" in html


def test_api_page_resolves_pack_and_walkup_namespace_basename(tmp_path, monkeypatch):
    """Bare-basename click-through must resolve a page in a pack-owned namespace NOT in the old
    hardcoded _PAGE_DIRS list, and a walk-up sub-domain path — both 404'd before (M-1758)."""
    (tmp_path / "wiki" / "detections").mkdir(parents=True)
    (tmp_path / "wiki" / "detections" / "sigma-rule-1.md").write_text(
        "---\ntype: detection\n---\n# Rule\n", encoding="utf-8")
    wu = tmp_path / "wiki" / "acme-sub" / "actor"
    wu.mkdir(parents=True)
    (wu / "apt-x.md").write_text("---\ntype: actor\n---\n# APT X\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m.api_page(path="sigma-rule-1") is not None    # pack namespace, no raise
    assert m.api_page(path="apt-x") is not None           # walk-up nested, no raise


def test_resolve_source_basename_fallback_uses_content_dirs(tmp_path, monkeypatch):
    """_resolve_source (behind /api/download) shares the basename fallback — it must use _content_dirs(),
    not the DELETED _PAGE_DIRS. The round-2 re-verify caught the second callsite left referencing the
    removed global: a bare-basename download NameError'd -> HTTP 500 for EVERY page, including ones the
    old hardcoded list resolved fine."""
    (tmp_path / "wiki" / "detections").mkdir(parents=True)
    (tmp_path / "wiki" / "detections" / "sig-1.md").write_text(
        "---\ntype: detection\n---\n# Rule\nbody text\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    raw, basefn, title = m._resolve_source(None, None, "sig-1")   # bare basename -> fallback branch
    assert "body text" in raw and basefn == "sig-1"


def test_cockpit_search_glob_exempts_reshard_bucket(tmp_path, monkeypatch):
    """The cockpit search ripgrep glob must be `!_?*` (underscore + ≥1 char), not `!_*` — the latter
    also prunes the bare-`_` reshard bucket (entities/x/_/x-force.md), making a resharded entity
    browsable-but-unfindable. Search must agree with browse (batch-2 gate)."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    cap = {}

    class _P:
        stdout = ""
    monkeypatch.setattr(m.subprocess, "run", lambda cmd, **k: (cap.__setitem__("cmd", cmd), _P())[1])
    m.api_search(q="hello")
    assert "!_?*" in cap["cmd"] and "!_*" not in cap["cmd"], cap["cmd"]


def test_unreviewed_ungrounded_entity_is_quarantined(tmp_path, monkeypatch):
    """The Gentlemen regression: warning chips may not decorate a polished canonical profile."""
    ent = tmp_path / "wiki" / "entities" / "g"
    ent.mkdir(parents=True)
    (ent / "gentlemen-ransomware-group-storm-2698.md").write_text(
        "---\ntype: actor\nname: The Gentlemen\nneeds_review: true\n"
        "sources: [https://example.invalid/rumor]\n---\n"
        "# Summary\n\nAn unverified but polished-looking actor profile.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="entities/g/gentlemen-ransomware-group-storm-2698")
    assert d["trust"]["state"] == "quarantined"
    assert set(d["trust"]["reasons"]) >= {"ungrounded", "needs review"}


def test_tombstoned_entity_points_to_canonical_instead_of_rendering_as_profile(tmp_path, monkeypatch):
    ent = tmp_path / "wiki" / "entities" / "g"
    ent.mkdir(parents=True)
    (ent / "duplicate.md").write_text(
        "---\ntype: actor\nstatus: tombstoned\n"
        "redirect_to: entities/g/canonical\n---\nOld body.\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    d = m.api_page(path="entities/g/duplicate")
    assert d["trust"] == {"state": "retired", "reasons": ["superseded record"],
                           "redirect_to": "entities/g/canonical"}


def test_non_entity_without_sources_is_not_quarantined(tmp_path, monkeypatch):
    dash = tmp_path / "wiki" / "dashboards"
    dash.mkdir(parents=True)
    (dash / "health.md").write_text("---\ntype: dashboard\n---\n# Health\n", encoding="utf-8")
    m = _load(tmp_path, monkeypatch)
    assert m.api_page(path="dashboards/health")["trust"]["state"] == "normal"


def test_ds_cell_pct_formats_epss_probability(tmp_path, monkeypatch):
    """okengine#259: a `pct: true` column renders a 0..1 probability (EPSS score) as a percentage —
    one decimal below 10% so small scores don't collapse to 0%, whole numbers above."""
    m = _load(tmp_path, monkeypatch)
    col = {"field": "epss_score", "pct": True}
    assert ">0.8%<" in m._ds_cell({"epss_score": 0.00783}, col)   # small: one decimal, not "1%"
    assert ">94%<" in m._ds_cell({"epss_score": 0.94}, col)       # large: whole percent
    assert ">100%<" in m._ds_cell({"epss_score": 1.0}, col)
    assert ">—<" in m._ds_cell({"epss_score": None}, col)         # missing -> placeholder, no crash
    assert "n/a" in m._ds_cell({"epss_score": "n/a"}, col)        # non-numeric -> left as-is


def test_ds_cell_tone_by_colors_severity_by_value(tmp_path, monkeypatch):
    """okengine#259: `tone_by` picks the cell tone FROM the value (severity enum) rather than one
    static column colour; unmapped values and blanks fall back cleanly."""
    m = _load(tmp_path, monkeypatch)
    col = {"field": "severity", "tone_by": {"critical": "crit", "high": "warn", "medium": "acc"}}
    assert 't-crit' in m._ds_cell({"severity": "critical"}, col)
    assert 't-warn' in m._ds_cell({"severity": "high"}, col)
    assert 't-acc' in m._ds_cell({"severity": "medium"}, col)
    assert 't-crit' in m._ds_cell({"severity": "Critical"}, col)  # case-insensitive fallback
    out_low = m._ds_cell({"severity": "low"}, col)                # not in map -> no tone class
    assert "t-crit" not in out_low and "t-warn" not in out_low
    assert ">—<" in m._ds_cell({"severity": None}, col)           # blank -> placeholder


def test_operation_control_requires_plan_and_uses_generic_operation_name(tmp_path, monkeypatch):
    monkeypatch.setenv("OKENGINE_OPERATION_API", "http://operation-runner:8732")
    monkeypatch.setenv("OKENGINE_OPERATION_TOKEN", "fixture-token")
    monkeypatch.setenv("OKENGINE_REVIEWER_NAME", "operator")
    monkeypatch.setenv("OKENGINE_REVIEW_TRUSTED_NETWORK", "1")
    m = _load(tmp_path, monkeypatch)
    html = m._v_operation_control({
        "operation": "actor-review", "arguments": ["--all"],
        "description": "Review every canonical actor.",
    })
    assert 'data-operation="actor-review"' in html
    assert "Plan scope" in html and "Start operation" in html
    assert 'data-operation-run disabled' in html
    assert "Review every canonical actor" in html


def test_operation_control_client_plans_before_starting():
    js = (REPO / "okengine-cockpit/static/app.js").read_text(encoding="utf-8")
    assert "planOperation(panel)" in js and "startOperation(panel)" in js
    assert js.index("panel.dataset.planDigest") < js.index("confirm(`Start ${name}")
    assert '"X-OKEngine-Operation":"1"' in js
