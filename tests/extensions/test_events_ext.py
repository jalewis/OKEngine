"""okengine.events — deterministic domain event ledger (okengine#155). Built on the #63 drop-in
model (no_agent lane in crons/*.cron.json); derived L1 dashboard, no own type."""
import importlib.util
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
REPO = Path(__file__).resolve().parent.parent.parent
EXT = REPO / "extensions" / "okengine.events"
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
LEDGER = EXT / "build_event_ledger.py"
SCORING = EXT / "event_scoring.py"
pytestmark = pytest.mark.skipif(not EXT.is_dir(), reason="okengine.events absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m


def _manifest():
    return yaml.safe_load((EXT / "extension.yaml").read_text())


def test_manifest_valid_dropin_no_schema():
    mod = _load("extension_manifest", MANIFEST)
    m = _manifest()
    assert m["id"] == "okengine.events" and mod.is_reserved_id(m["id"])
    assert "operation" not in m and "operations" not in m and "schema" not in m  # drop-in, derived
    errors, _ = mod.validate_manifest(m)
    assert not errors, errors


def test_dropin_composes_two_no_agent_lanes():
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(
        {"id": "okengine.events", "tier": "engine", "dir": str(EXT), "manifest": _manifest()})
    assert not errors, errors
    assert [j["name"] for j in jobs] == [
        "okengine.events:event-ledger", "okengine.events:event-scoring"]
    assert all(job["no_agent"] is True for job in jobs)


def _run(vault: Path):
    return subprocess.run([sys.executable, str(LEDGER)], capture_output=True, text=True,
                          env={**os.environ, "WIKI_PATH": str(vault),
                               "OKENGINE_MCP_WRITE_DATE": "2026-06-28"}).stdout


def test_ledger_compiles_scored_events(tmp_path):
    w = tmp_path / "wiki"
    (w / "deals").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "event_types: [deal, incident]\nevent_score_weights: {deal: 2, incident: 1}\n")
    (w / "deals" / "a.md").write_text("---\ntype: deal\ntitle: A\ndate: 2026-06-20\n---\nx\n")
    (w / "deals" / "b.md").write_text("---\ntype: incident\ntitle: B\ndate: 2026-06-25\n---\nx\n")
    (w / "deals" / "c.md").write_text("---\ntype: concept\ntitle: C\n---\nx\n")  # not an event
    _run(tmp_path)
    led = (w / "dashboards" / "event-ledger.md").read_text()
    assert "**2 events**" in led
    assert "| 2026-06-25 | incident | 1 |" in led and "| 2026-06-20 | deal | 2 |" in led
    assert "C |" not in led            # the concept page is excluded


def test_no_event_types_is_a_clean_noop(tmp_path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {}\n")     # no event_types
    out = _run(tmp_path)
    assert "nothing to compile" in out
    assert not (tmp_path / "wiki" / "dashboards" / "event-ledger.md").exists()


def test_ledger_self_contained():
    imports = re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", LEDGER.read_text(), re.M)
    allowed = {"__future__", "json", "os", "re", "datetime", "pathlib", "yaml", "typing", "collections"}
    assert not [i for i in imports if i.split(".")[0] not in allowed]


def test_ledger_parses_year_month_dates(tmp_path):
    """Partial dates are common (campaign first_seen: 2025-10) — they must parse to a real date,
    not silently fall back to `updated` (the bug found rolling to sec)."""
    w = tmp_path / "wiki"
    (w / "e").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("event_types: [campaign]\nevent_date_field: first_seen\n")
    (w / "e" / "camp.md").write_text(
        "---\ntype: campaign\ntitle: Camp\nfirst_seen: 2025-10\nupdated: 2026-06-27\n---\nx\n")
    _run(tmp_path)
    led = (w / "dashboards" / "event-ledger.md").read_text()
    assert "2025-10-01" in led and "2026-06-27" not in led   # year-month padded, not the fallback


def test_ledger_date_schema_and_frontmatter_edges(tmp_path, monkeypatch):
    module = _load("event_ledger_edges", LEDGER)
    assert module._norm_date("seen 2026-08-04") == "2026-08-04"
    assert module._norm_date("2026-08") == "2026-08-01"
    assert module._norm_date("during 2026") == "2026-01-01"
    assert module._norm_date("unknown") is None

    monkeypatch.setattr(module, "VAULT", tmp_path)
    monkeypatch.setattr(module, "WIKI", tmp_path / "wiki")
    assert module._schema() == {}
    composed = tmp_path / ".okengine/composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text("- not-a-mapping\n")
    (tmp_path / "schema.yaml").write_text("event_types: [incident]\n")
    assert module._schema()["event_types"] == ["incident"]
    composed.write_text("[broken")
    assert module._schema()["event_types"] == ["incident"]

    absent = tmp_path / "absent.md"
    assert module._fm(absent) == {}
    for content in ("body", "---\n- list\n---\n", "---\n[broken\n---\n"):
        page = tmp_path / "page.md"
        page.write_text(content)
        assert module._fm(page) == {}
    assert module._event_date({"published": "2026-08"}, "custom") == "2026-08-01"
    assert module._event_date({"custom": "unknown", "created": "2025"}, "custom") == "2025-01-01"
    assert module._event_date({}, "custom") is None


def test_ledger_main_filters_limits_and_replaces_daily_snapshot(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    events = wiki / "events"
    events.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "event_types: [incident]\nevent_date_field: occurred\nevent_score_weights: []\n"
    )
    (events / "dated.md").write_text(
        "---\ntype: incident\nname: Dated\noccurred: 2026-08-03\n---\n"
    )
    (events / "undated.md").write_text("---\ntype: incident\n---\n")
    (events / "other.md").write_text("---\ntype: concept\n---\n")
    (events / ".hidden.md").write_text("---\ntype: incident\ndate: 2026-09-01\n---\n")
    (events / "old.bak.copy.md").write_text("---\ntype: incident\ndate: 2026-09-01\n---\n")
    snapshot = wiki / "operational/event-ledger-snapshots.md"
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-08-04")
    module = _load("event_ledger_main_edges", LEDGER)
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    assert module.main() == 0
    snapshot.write_text(snapshot.read_text() + "| 2026-08-04 | 99 |\n| 2026-08-03 | 1 |\n")
    assert module.main() == 0
    dashboard = (wiki / "dashboards/event-ledger.md").read_text()
    assert "**2 events** (showing newest 1)" in dashboard
    assert "[[events/dated]]" in dashboard and "[[events/undated]]" not in dashboard
    text = snapshot.read_text()
    assert text.count("| 2026-08-04 |") == 1 and "| 2026-08-04 | 2 |" in text
    assert "compiled 2 event" in capsys.readouterr().out


def test_ledger_no_vault_and_snapshot_write_failure_are_nonfatal(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    no_vault = _load("event_ledger_no_vault", LEDGER)
    assert no_vault.main() == 0

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (tmp_path / "schema.yaml").write_text("event_types: [incident]\n")
    module = _load("event_ledger_snapshot_failure", LEDGER)
    monkeypatch.setattr(module, "SNAP", tmp_path / "blocked/snapshot.md")
    original_mkdir = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == module.SNAP.parent:
            raise OSError("read-only")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    assert module.main() == 0


def _run_scoring(vault: Path, data: Path):
    return subprocess.run([sys.executable, str(SCORING)], capture_output=True, text=True,
                          env={**os.environ, "WIKI_PATH": str(vault), "HERMES_DATA": str(data),
                               "OKENGINE_MCP_WRITE_DATE": "2026-07-15"}, check=True)


def test_event_scoring_vector_typed_partitions_and_ranking(tmp_path):
    wiki = tmp_path / "wiki"
    events = wiki / "events"
    events.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        """event_types: [capital-event, product-event]
event_date_field: occurred
event_score_weights: {capital-event: 0.8, product-event: 0.6}
event_scoring:
  source_kind_weights: {primary: 0.9}
  evidence_phrases: [raised]
  watchlist_tier_weights: {priority: 0.9}
  typed_extractors: {capital-event: funding, product-event: product-launch}
""", encoding="utf-8")

    def event(slug, page_type, occurred, body):
        (events / f"{slug}.md").write_text(
            f"---\ntype: {page_type}\ntitle: {slug}\noccurred: {occurred}\n"
            "entity: '[[entities/acme]]'\nreliability: A\nsource_kind: primary\n"
            f"competitor_tier: priority\n---\n{body}\n", encoding="utf-8")

    event("new-round", "capital-event", "2026-07-15",
          "Acme raised $20M Series A led by Example Ventures.")
    event("old-round", "capital-event", "2026-06-15", "Acme raised $5M seed.")
    event("launch", "product-event", "2026-07-14", "Now available in general availability.")
    data = tmp_path / "data"
    result = _run_scoring(tmp_path, data)
    assert "scored 3 event(s)" in result.stdout

    score_path = data / "state" / "okengine.events" / "event-scores.jsonl"
    rows = [__import__("json").loads(line) for line in score_path.read_text().splitlines()]
    by_id = {row["event_id"]: row for row in rows}
    new = by_id["events/new-round"]
    assert set(new["scores"]) == {
        "source_reliability_score", "claim_credibility_score", "signal_strength",
        "materiality", "novelty", "watchlist_relevance", "recency_decay",
        "corroboration_count"}
    assert new["scores"] == {
        "source_reliability_score": 1.0, "claim_credibility_score": 0.996,
        "signal_strength": 0.87, "materiality": 0.592, "novelty": 0.5,
        "watchlist_relevance": 0.9, "recency_decay": 1.0, "corroboration_count": 1}
    assert by_id["events/old-round"]["scores"]["recency_decay"] == 0.5
    assert by_id["events/launch"]["scores"]["novelty"] == 1.0

    typed_dir = score_path.parent / "typed-events"
    funding = [__import__("json").loads(line)
               for line in (typed_dir / "capital-event.jsonl").read_text().splitlines()]
    assert len(funding) == 2
    assert funding[0]["typed_fields"]["amount_usd"] in (20_000_000, 5_000_000)
    product = __import__("json").loads(
        (typed_dir / "product-event.jsonl").read_text().splitlines()[0])
    assert product["typed_fields"]["is_general_availability"] is True
    dash = (wiki / "dashboards" / "event-scoring.md").read_text()
    assert "**3 events scored.**" in dash and "## Typed extraction" in dash
    assert dash.index("[[events/new-round]]") < dash.index("[[events/old-round]]")

    first_scores = score_path.read_text()
    first_dash = dash
    _run_scoring(tmp_path, data)
    assert score_path.read_text() == first_scores
    assert (wiki / "dashboards" / "event-scoring.md").read_text() == first_dash


def test_event_scoring_covers_sources_not_linked_to_events(tmp_path):
    wiki = tmp_path / "wiki"
    sources = wiki / "sources"
    sources.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "event_types: [campaign]\n"
        "event_scoring:\n"
        "  source_kind_weights: {vendor-report: 0.8}\n",
        encoding="utf-8",
    )
    (sources / "report.md").write_text(
        "---\ntype: source\ntitle: Report\npublished: 2026-07-15\n"
        "reliability: A\nsource_kind: vendor-report\npublisher: Microsoft\n---\n"
        "Direct telemetry and analysis.\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"

    result = _run_scoring(tmp_path, data)

    assert "0 event(s) + 1 source(s)" in result.stdout
    rows = [__import__("json").loads(line) for line in (
        data / "state" / "okengine.events" / "event-scores.jsonl"
    ).read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "sources/report"
    assert row["score_scope"] == "source"
    assert row["scores"]["source_reliability_score"] == 1.0
    assert row["scores"]["signal_strength"] > 0
    assert row["scores"]["recency_decay"] == 1.0
    assert "**1 canonical sources scored.**" in (
        wiki / "dashboards" / "event-scoring.md"
    ).read_text()


def test_event_scoring_mechanism_has_no_domain_vocabulary_defaults():
    text = SCORING.read_text()
    for domain_term in ("funding", "m-and-a", "product-launch", "regulation", "high", "medium"):
        assert domain_term not in text


def test_collect_sources_computes_independent_corroboration(tmp_path, monkeypatch):  # invariant-audit #351 (A5)
    """collect_sources is the missing PRODUCER for independent_corroboration_count: a source's
    corroboration = the number of DISTINCT OTHER publishers whose sources report on any entity it
    references. Before, the field had no producer, was always 0, and score_source pinned 25% of the
    source-evidence signal to a constant 0.5."""
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("OKENGINE_MCP_WRITE_DATE", "2026-06-15")
    sdir = tmp_path / "wiki" / "sources"
    sdir.mkdir(parents=True)

    def src(name, publisher, body="reports on [[entities/acme]]"):
        (sdir / f"{name}.md").write_text(
            f"---\ntype: source\ntitle: {name}\npublisher: {publisher}\npublished: 2026-06-01\n---\n{body}\n",
            encoding="utf-8")

    src("s1", "Reuters"); src("s2", "AP"); src("s3", "Bloomberg")   # 3 distinct outlets on acme
    src("s4", "Reuters")                                            # dup outlet: no NEW independent
    src("s5", "Reuters", body="reports on [[entities/loner]]")      # lone entity -> 0
    m = _load("event_scoring", SCORING)
    rows = {r["source"].split("/")[-1]: r for r in m.collect_sources(m.config({}))}
    assert rows["s1"]["corroboration_count"] == 2, rows["s1"]   # AP + Bloomberg (own Reuters excluded)
    assert rows["s2"]["corroboration_count"] == 2               # Reuters + Bloomberg
    assert rows["s4"]["corroboration_count"] == 2               # dup Reuters still sees AP + Bloomberg
    assert rows["s5"]["corroboration_count"] == 0               # alone on 'loner'
    # an explicit frontmatter value acts as a floor
    (sdir / "s6.md").write_text(
        "---\ntype: source\ntitle: s6\npublisher: Solo\nindependent_corroboration_count: 5\n---\nno refs\n",
        encoding="utf-8")
    rows2 = {r["source"].split("/")[-1]: r for r in m.collect_sources(m.config({}))}
    assert rows2["s6"]["corroboration_count"] == 5


def test_event_scoring_helper_and_collection_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    module = _load("event_scoring_boundaries", SCORING)
    assert module._schema() == {}
    composed = tmp_path / ".okengine/composed-schema.yaml"
    composed.parent.mkdir()
    composed.write_text("[broken")
    (tmp_path / "schema.yaml").write_text("- list\n")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "schema.yaml").write_text("event_types: [incident]\n")
    assert module._schema()["event_types"] == ["incident"]

    assert module._page(tmp_path / "missing.md") == ({}, "")
    plain = tmp_path / "plain.md"
    plain.write_text("body")
    assert module._page(plain) == ({}, "body")
    malformed = tmp_path / "malformed.md"
    malformed.write_text("---\nkey: [\n---\nbody")
    assert module._page(malformed) == ({}, "body")
    assert module._date(datetime(2026, 7, 15, 12)) == date(2026, 7, 15)
    assert module._date("invalid") is None
    assert module._num_map({"good": "1", "bad": "x"}) == {"good": 1.0}

    cfg = module.config({})
    assert module._source({}, cfg) == ({}, "")
    assert module._source({"source": "sources/missing"}, cfg) == ({}, "")
    assert module._entity_tier("", {}, cfg) == ""
    assert module._entity_tier("acme", {"competitor_tier": "priority"}, cfg) == "priority"
    entity = wiki / "entities/a/acme.md"
    entity.parent.mkdir(parents=True)
    entity.write_text("---\ntype: vendor\ncompetitor_tier: tracked\n---\n")
    assert module._entity_tier("acme", {}, cfg) == "tracked"
    assert module._entity_tier("absent", {}, cfg) == ""

    assert module._source_entities(
        {"entity": ["[[entities/frontmatter.md]]", "[[entities/second]]", ""]},
        "[[entities/body.md]] [[concepts/ignored]]", cfg,
    ) == {"frontmatter", "second", "body"}


def test_event_scoring_collects_malformed_sources_and_no_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    module = _load("event_scoring_collection_edges", SCORING)
    cfg = module.config({"event_types": ["incident"], "event_scoring": {
        "typed_extractors": {"incident": "unsupported"}
    }})
    assert module.collect(cfg) == []
    assert module.collect_sources(cfg) == []
    assert module.main() == 0

    source_dir = tmp_path / "wiki/sources"
    source_dir.mkdir(parents=True)
    (source_dir / "INDEX.md").write_text("ignored")
    (source_dir / "other.md").write_text("---\ntype: concept\n---\n")
    (source_dir / "bad-count.md").write_text(
        "---\ntype: source\nindependent_corroboration_count: invalid\n"
        "entities: ['[[entities/acme]]']\n---\n"
    )
    rows = module.collect_sources(cfg)
    assert len(rows) == 1 and rows[0]["corroboration_count"] == 0

    event_dir = tmp_path / "wiki/events"
    event_dir.mkdir()
    (event_dir / ".hidden.md").write_text("---\ntype: incident\n---\n")
    (event_dir / "event.md").write_text("---\ntype: incident\n---\n")
    (event_dir / "dated.md").write_text(
        "---\ntype: incident\ndate: 2026-07-15\nentity: entities/acme\n---\nraised $2M"
    )
    collected = module.collect(cfg)
    assert len(collected) == 2 and any(row["date"] is None for row in collected)
    scored, typed = module.score(collected, date.today(), cfg)
    assert len(scored) == 2 and typed == {}

    supported = {**cfg, "typed_extractors": {"incident": "funding"}}
    scored, typed = module.score(collected, date.today(), supported)
    assert len(scored) == 2 and len(typed["incident"]) == 2

    monkeypatch.setattr(module, "_schema", lambda: {
        "event_types": ["incident"],
        "event_scoring": {"typed_extractors": {"incident": "unsupported"}},
    })
    assert module.main() == 0
    monkeypatch.setattr(module, "_schema", lambda: {
        "event_types": ["incident"],
        "event_scoring": {"typed_extractors": {"incident": "funding"}},
    })
    assert module.main() == 0
