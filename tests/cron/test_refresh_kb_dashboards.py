"""Regression: the recent-ingest dashboard derives a source's ingest date with a fallback chain
(ingested -> last_updated -> updated -> published), not only `ingested` — else it renders an
all-empty board even as sources stream in, because sources carry `last_updated`/`published`,
not `ingested`."""
import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("yaml")
CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"


def _load():
    sys.path.insert(0, str(CRON))
    spec = importlib.util.spec_from_file_location("refresh_kb_dashboards", CRON / "refresh_kb_dashboards.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_import_adds_cron_directory_when_absent(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(CRON)])
    spec = importlib.util.spec_from_file_location("refresh_kb_dashboards_path", CRON / "refresh_kb_dashboards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sys.path[0] == str(CRON)


def test_ingest_date_fallback_chain():
    m = _load()
    assert m._ingest_date({"ingested": "2026-06-01"}) == date(2026, 6, 1)
    assert m._ingest_date({"last_updated": "2026-06-21"}) == date(2026, 6, 21)          # no ingested
    assert m._ingest_date({"created": "2026-06-10", "last_updated": "2026-06-21"}) == date(2026, 6, 10)  # created beats last_updated
    assert m._ingest_date({"published": "2026-06-04T12:05:31+00:00"}) == date(2026, 6, 4)  # only published
    assert m._ingest_date({}) is None
    # precedence: explicit ingested wins over last_updated
    assert m._ingest_date({"ingested": "2026-06-01", "last_updated": "2026-06-21"}) == date(2026, 6, 1)


def _page(path: Path, fm: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\nBody.\n", encoding="utf-8")


def test_discovery_loading_and_source_rating_gate(tmp_path, monkeypatch):
    m = _load()
    wiki = tmp_path / "wiki"
    _page(wiki / "actors" / "a" / "acme.md",
          "type: actor\ntitle: Acme\nconfidence: low\nsources: [one]\n")
    _page(wiki / "actors" / ".hidden.md", "type: actor\n")
    monkeypatch.setattr(m, "WIKI", wiki)
    monkeypatch.setattr(m, "_SCHEMA", {
        "partitioning": {"namespaces": {"actors": {"strategy": "by-letter"}}},
        "types": {"source": {"optional": ["reliability", "credibility"]}},
    })
    assert m._discover_namespaces() == ["actors"]
    assert m._schema_declares_source_rating() is True
    pages = m.load_dir("actors")
    assert len(pages) == 1
    assert pages[0]["_name"] == "acme"
    assert m._n_sources(pages[0]) == 1
    assert "Acme" in m._wl(pages[0])
    assert m._esc("a|b\nc") == "a\\|b c"


def test_main_writes_generic_and_source_dashboards(tmp_path, monkeypatch, capsys):
    m = _load()
    wiki = tmp_path / "wiki"
    _page(wiki / "actors" / "a" / "acme.md",
          "type: actor\ntitle: Acme\nconfidence: low\nupdated: 2026-05-01\n")
    _page(wiki / "sources" / "2026" / "07" / "report.md",
          "type: source\ntitle: Report\npublisher: Example\nsource_kind: report\n"
          "published: 2026-07-22\nreliability: A\ncredibility: 1\n"
          "bias_flags: [vendor]\n")
    monkeypatch.setattr(m, "VAULT", tmp_path)
    monkeypatch.setattr(m, "WIKI", wiki)
    monkeypatch.setattr(m, "DASH_DIR", wiki / "dashboards")
    monkeypatch.setattr(m, "_SCHEMA", {
        "partitioning": {"namespaces": {
            "actors": {"strategy": "by-letter"},
            "sources": {"strategy": "by-date"},
        }},
        "types": {"source": {"fields": {
            "reliability": {}, "credibility": {},
        }}},
    })
    monkeypatch.setattr(m.tz_lib, "deployment_today", lambda: date(2026, 7, 23))
    monkeypatch.setattr(
        m.tz_lib, "deployment_now",
        lambda: datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc),
    )

    assert m.main() == 0
    names = {p.name for p in (wiki / "dashboards").glob("*.md")}
    assert names == {
        "latest-pages-by-confidence.md",
        "latest-stale-content.md",
        "latest-source-density.md",
        "latest-source-quality.md",
        "latest-recent-ingest.md",
    }
    assert "Acme" in (wiki / "dashboards" / "latest-stale-content.md").read_text()
    quality = (wiki / "dashboards" / "latest-source-quality.md").read_text()
    assert "A / 1" in quality and "Bias-flagged (1)" in quality
    assert '"wakeAgent": false' in capsys.readouterr().out


def test_discovery_schema_and_frontmatter_edge_paths(tmp_path, monkeypatch):
    m=_load();wiki=tmp_path/"wiki";wiki.mkdir()
    (wiki/"actors").mkdir();(wiki/"dashboards").mkdir();(wiki/"_hidden").mkdir()
    monkeypatch.setattr(m,"WIKI",wiki);monkeypatch.setattr(m,"_SCHEMA",{})
    assert m._discover_namespaces()==["actors"]
    monkeypatch.setattr(m,"WIKI",tmp_path/"missing")
    assert m._discover_namespaces()==[]
    for schema in ({"types":[]},{"types":{"x":[]}},{"types":{"x":{"required":["reliability"]}}}):
        monkeypatch.setattr(m,"_SCHEMA",schema);assert not m._schema_declares_source_rating()
    monkeypatch.setattr(m,"_SCHEMA",{"types":{"x":{"required":("reliability","credibility")}}})
    assert m._schema_declares_source_rating()
    assert m._parse_date(date(2026,1,1))==date(2026,1,1)
    assert m._parse_date(datetime(2026,1,2))==date(2026,1,2)
    assert m._parse_date("bad") is None and m._parse_date(1) is None
    assert m._parse_date("2026-99-99") is None
    plain=tmp_path/"plain.md";plain.write_text("body");assert m._frontmatter(plain)=={}
    unreadable=tmp_path/"unreadable.md";unreadable.mkdir();assert m._frontmatter(unreadable)=={}
    bad=tmp_path/"bad.md";bad.write_text("---\n[bad\n---\n");assert m._frontmatter(bad)=={}
    scalar=tmp_path/"scalar.md";scalar.write_text("---\n- x\n---\n");assert m._frontmatter(scalar)=={}
    assert m.load_dir("absent")==[]
    assert m._table(["A"],[])=="_none_\n" and m._esc(None)==""


def test_dashboard_variants_cover_unrated_recent_and_empty(tmp_path, monkeypatch):
    m=_load();dash=tmp_path/"dashboards";dash.mkdir()
    monkeypatch.setattr(m,"DASH_DIR",dash)
    pages=[
      {"_name":"a","_sub":"actors","type":"actor","name":"A","confidence":"high",
       "sources":["1","2","3","4","5"],"updated":"2026-01-01"},
      {"_name":"b","_sub":"actors","type":"actor","sources":[]},
    ]
    m.dash_pages_by_confidence(pages,"ts")
    m.dash_stale_content(pages,date(2026,3,1),"ts")
    m.dash_source_density(pages,"ts")
    sources=[
      {"_name":"a","_sub":"sources","type":"source","publisher":"P","published":"2026-01-01"},
      {"_name":"old","_sub":"sources","type":"source","publisher":"P","published":"2025-01-01"},
      {"_name":"skip","_sub":"sources","type":"note"},
    ]
    quality=m.dash_source_quality(sources,"ts").read_text()
    recent=m.dash_recent_ingest(sources,date(2026,1,2),"ts").read_text()
    assert "Unrated backlog (no reliability) (2)" in quality
    assert "Last 24h (1)" in recent


def test_main_without_sources_omits_source_specific_dashboards(tmp_path, monkeypatch):
    m=_load();wiki=tmp_path/"wiki";(wiki/"actors").mkdir(parents=True)
    monkeypatch.setattr(m,"VAULT",tmp_path);monkeypatch.setattr(m,"WIKI",wiki)
    monkeypatch.setattr(m,"DASH_DIR",wiki/"dashboards")
    monkeypatch.setattr(m,"_SCHEMA",{"partitioning":{"namespaces":{"actors":{}}}})
    monkeypatch.setattr(m.tz_lib,"deployment_today",lambda:date(2026,1,1))
    monkeypatch.setattr(m.tz_lib,"deployment_now",lambda:datetime(2026,1,1,tzinfo=timezone.utc))
    assert m.main()==0
    assert not (wiki/"dashboards/latest-recent-ingest.md").exists()
def test_confidence_dashboard_surfaces_and_orders_full_qualitative_scale(tmp_path, monkeypatch):
    m = _load(); dash = tmp_path / "dashboards"; dash.mkdir()
    monkeypatch.setattr(m, "DASH_DIR", dash)
    pages = [
        {"_name": label, "_sub": "predictions", "type": "prediction",
         "confidence": label, "sources": ["a", "b"]}
        for label in ("medium", "medium-low", "low", "very-low", "very-high")
    ]
    rendered = m.dash_pages_by_confidence(pages, "ts").read_text()
    review = rendered.split("## Low-confidence / single-source review", 1)[1]
    positions = [review.index(f"[[predictions/{label}")
                 for label in ("very-low", "low", "medium-low", "medium")]
    assert positions == sorted(positions)
    assert "[[predictions/very-high" not in review
