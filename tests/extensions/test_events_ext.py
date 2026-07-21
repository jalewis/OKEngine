"""okengine.events — deterministic domain event ledger (okengine#155). Built on the #63 drop-in
model (no_agent lane in crons/*.cron.json); derived L1 dashboard, no own type."""
import importlib.util
import os
import re
import subprocess
import sys
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
