"""Regression: source-staleness must resolve citations to date-partitioned sources.

The score map is keyed `sources/<stem>`, but normalize_link returned the full citation path
(`sources/2026/07/foo`), so a citation to any date-partitioned source never matched its score
entry — staleness was silently never applied to partitioned sources (the common case). This pins
the `<namespace>/<slug>` stem collapse so partitioned and flat citations both resolve.
"""
import importlib.util
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "select_source_staleness.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    os.environ.pop("DECAY_ENTITY_TYPES", None)   # empty ⇒ accept any entity/concept type
    import sys
    spec = importlib.util.spec_from_file_location("select_source_staleness", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["select_source_staleness"] = m   # register before exec so dataclasses resolve __module__
    spec.loader.exec_module(m)
    return m


def test_normalize_link_collapses_partition_to_stem(tmp_path):
    m = _load(tmp_path)
    assert m.normalize_link("[[sources/2026/07/foo]]") == "sources/foo"
    assert m.normalize_link("sources/2026/07/foo.md") == "sources/foo"
    assert m.normalize_link("sources/foo") == "sources/foo"          # flat still works
    assert m.normalize_link("[[sources/2026/07/foo|Foo]]") == "sources/foo"


def test_import_bootstraps_cron_path_and_missing_sources_are_empty(tmp_path, monkeypatch):
    cron = str(MOD.parent)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != cron])
    m = _load(tmp_path)
    assert sys.path[0] == cron
    assert m.score_all_sources(date(2026, 1, 1)) == {}


def test_partitioned_source_citation_resolves_end_to_end(tmp_path):
    wiki = tmp_path / "wiki"
    src = wiki / "sources" / "2026" / "01"
    src.mkdir(parents=True)
    # a clearly-stale source (old, low reliability) at a date-partitioned path
    (src / "old-report.md").write_text(
        "---\ntype: source\npublished: 2020-01-01\nreliability: C\ncredibility: 3\n"
        "source_kind: news\n---\n# Old Report\n", encoding="utf-8")
    con = wiki / "concepts" / "x"
    con.mkdir(parents=True)
    # concept cites the source via the FULL partition path — the case that used to fail
    (con / "foo.md").write_text(
        "---\ntype: concept\nsources: ['[[sources/2026/01/old-report]]']\n---\n# Foo\n",
        encoding="utf-8")
    m = _load(tmp_path)
    scores = m.score_all_sources(date(2026, 7, 7))
    assert "sources/old-report" in scores                      # producer key (stem)
    anchors = m.discover_anchors(scores)
    # the concept's citation must have resolved to a score (non-empty) — the bug made this empty
    assert any(a.segment == "concepts" and a.rel_path == "concepts/foo" for a in anchors), \
        "partitioned-source citation did not resolve — anchor was dropped"


def test_helpers_primary_citations_and_bands(tmp_path):
    m = _load(tmp_path)
    assert m.to_date("2026-07-24") == date(2026, 7, 24)
    assert m.to_date("not-a-date") is None
    assert m.normalize_link(None) is None
    assert m._primary_citations({
        "sources": ["[[sources/2026/07/one]]", "prose", "[[entities/nope]]"],
    }, "entities") == ["sources/one"]
    assert m._primary_citations({
        "basis": ["[[sources/one]]", "[[sources/two]]"],
    }, "predictions") == ["sources/one", "sources/two"]
    assert m._band(.9).startswith("0.85")
    assert m._band(.75).startswith("0.70")
    assert m._band(.6).startswith("0.50")
    assert m._band(.4).startswith("0.30")
    assert m._band(.2).startswith("0.10")
    assert m._band(.01).startswith("0.00")


def test_render_and_main_cover_stale_weak_and_oov(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    sources = wiki / "sources" / "2026" / "07"
    sources.mkdir(parents=True)
    (sources / "old.md").write_text(
        "---\ntype: source\npublished: 2010-01-01\nreliability: Z\ncredibility: 9\n"
        "source_kind: news\n---\n# Old\n")
    (sources / "fresh.md").write_text(
        "---\ntype: source\npublished: 2026-07-24\nreliability: D\ncredibility: 4\n"
        "source_kind: report\n---\n# Fresh\n")
    concepts = wiki / "concepts"
    concepts.mkdir()
    (concepts / "stale.md").write_text(
        "---\ntype: concept\nupdated: 2020-01-01\nsources: ['[[sources/old]]']\n"
        "---\n# Stale Concept\n")
    (concepts / "weak.md").write_text(
        "---\ntype: concept\nsources: ['[[sources/old]]', '[[sources/fresh]]']\n"
        "---\n# Weak Concept\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "DASH_PATH", wiki / "dashboards" / "source-staleness.md")
    monkeypatch.setattr(m.tz_lib, "deployment_today", lambda: date(2026, 7, 24))
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(m, "datetime", FrozenDateTime)

    scores = m.score_all_sources(date(2026, 7, 24))
    anchors = m.discover_anchors(scores)
    dashboard = m.render_dashboard(scores, anchors, date(2026, 7, 24))
    assert "unrecognized reliability/credibility grade" in dashboard
    assert "Stale Concept" in dashboard
    assert "Weak Concept" in dashboard
    assert m.main() == 0
    assert m.DASH_PATH.is_file()
    assert "dashboard: updated" in capsys.readouterr().out
    # The dynamic updated timestamp is stable within this immediate rerun.
    assert m.main() == 0
    assert "dashboard: unchanged" in capsys.readouterr().out


def test_schema_enum_and_parsing_edge_paths(tmp_path, monkeypatch):
    m = _load(tmp_path)
    assert m._ordered_enum({"field_enums":{"x":["a",2]}},"x")==["a","2"]
    assert m._ordered_enum({"field_enums":{"x":{"enum":"named"}},"enums":{"named":["a"]}},"x")==["a"]
    assert m._ordered_enum({"field_enums":{"x":{"enum":"missing"}}},"x")==[]
    assert m.to_date(date(2026,1,2))==date(2026,1,2)
    from datetime import datetime
    assert m.to_date(datetime(2026,1,3))==date(2026,1,3)
    assert m.to_date(3) is None
    assert m.normalize_link("  ") is None
    plain=tmp_path/"plain.md";plain.write_text("body")
    assert m.parse_fm_and_body(plain)==({},"body")
    malformed=tmp_path/"bad.md";malformed.write_text("---\n[bad\n---\nbody")
    assert m.parse_fm_and_body(malformed)==({},"body")
    monkeypatch.setattr(Path,"read_text",lambda *a,**k:(_ for _ in ()).throw(OSError()))
    assert m.parse_fm_and_body(plain)==({},"")


def test_source_and_anchor_filters_and_titles(tmp_path):
    wiki=tmp_path/"wiki";sources=wiki/"sources";sources.mkdir(parents=True)
    (sources/"_skip.md").write_text("---\ntype: source\n---\n")
    (sources/"wrong.md").write_text("---\ntype: note\n---\n")
    (sources/"2020-01-01-undated.md").write_text("---\ntype: source\n---\n")
    (sources/"nodate.md").write_text("---\ntype: source\npublished: bad\n---\n")
    m=_load(tmp_path);scores=m.score_all_sources(date(2026,7,1))
    assert set(scores)=={"sources/2020-01-01-undated","sources/nodate"}
    for segment in ("concepts","entities","predictions"):
        d=wiki/segment;d.mkdir()
        (d/"INDEX.md").write_text("---\ntype: concept\nsources: [sources/nodate]\n---\n")
        (d/"_skip.md").write_text("---\ntype: concept\nsources: [sources/nodate]\n---\n")
    (wiki/"predictions/p.md").write_text("---\ntype: note\nbasis: [sources/nodate]\n---\n")
    (wiki/"entities/untyped.md").write_text("---\nsources: [sources/nodate]\n---\n")
    (wiki/"concepts/no-cites.md").write_text("---\ntype: concept\n---\n")
    (wiki/"concepts/unknown.md").write_text("---\ntype: concept\nsources: [sources/missing]\n---\n")
    (wiki/"concepts/fallback-title.md").write_text("---\ntype: concept\nsources: [sources/nodate]\n---\n")
    anchors=m.discover_anchors(scores)
    assert [(a.rel_path,a.title) for a in anchors]==[("concepts/fallback-title","Fallback Title")]


def test_primary_nonlist_wikilinks_and_render_empty_or_truncated(tmp_path, monkeypatch):
    m=_load(tmp_path)
    assert m._primary_citations({"sources":"not-list"},"entities")==[]
    assert m._wikilink("sources/x")=="[[sources/x]]"
    scores={
      "sources/a":m.SourceScore("sources/a",.2,True,None,5,.4),
      "sources/b":m.SourceScore("sources/b",.8,False,"news",1,.9),
    }
    anchors=[
      m.Anchor("entities/a","A","entities",["sources/a"],[.2],None),
      m.Anchor("entities/b","B","entities",["sources/a"],[.2],date(2020,1,1)),
    ]
    monkeypatch.setattr(m,"TOP_PAGES_PER_SECTION",1)
    text=m.render_dashboard(scores,anchors,date(2026,1,1))
    assert "and 1 more" in text and "_None._" in text and "(unset)" in text
    empty=m.render_dashboard({},[],date(2026,1,1))
    assert "No stale anchors" in empty


def test_pack_entity_type_filter_handles_scalar_and_out_of_scope_types(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    sources = wiki / "sources"; sources.mkdir(parents=True)
    (sources / "s.md").write_text(
        "---\ntype: source\npublished: 2020-01-01\n---\n# S\n")
    entities = wiki / "entities"; entities.mkdir()
    (entities / "scalar.md").write_text("---\ntype: 3\nsources: [sources/s]\n---\n")
    (entities / "other.md").write_text("---\ntype: other\nsources: [sources/s]\n---\n")
    (entities / "accepted.md").write_text("---\ntype: actor\nsources: [sources/s]\n---\n")
    m = _load(tmp_path)
    monkeypatch.setattr(m, "ENTITY_TYPES", {"actor"})
    anchors = m.discover_anchors(m.score_all_sources(date(2026, 1, 1)))
    assert {anchor.rel_path for anchor in anchors} == {"entities/other", "entities/accepted"}


def test_prediction_anchor_bypasses_entity_type_filter(tmp_path):
    wiki = tmp_path / "wiki"
    sources = wiki / "sources"; sources.mkdir(parents=True)
    (sources / "s.md").write_text("---\ntype: source\npublished: 2020-01-01\n---\n")
    predictions = wiki / "predictions"; predictions.mkdir()
    (predictions / "p.md").write_text(
        "---\ntype: prediction\nbasis: [sources/s]\n---\n# Prediction\n")
    m = _load(tmp_path)
    anchors = m.discover_anchors(m.score_all_sources(date(2026, 1, 1)))
    assert [anchor.rel_path for anchor in anchors] == ["predictions/p"]
