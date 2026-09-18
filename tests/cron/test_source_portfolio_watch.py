"""source_portfolio_watch: pure-script corpus-COMPOSITION dashboard (complements source-staleness).
Generic — signal_class sections are conditional; every field optional; wakeAgent always False."""
import importlib.util
import io
import contextlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m


def _src(d: Path, slug: str, **fm):
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: source"] + [f"{k}: {v}" for k, v in fm.items()] + ["---", "# s", ""]
    (d / f"{slug}.md").write_text("\n".join(lines))


def _run(tmp, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp))
    m = _load("source_portfolio_watch", "scripts/cron/source_portfolio_watch.py")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main()
    assert rc == 0
    wake = json.loads(buf.getvalue().strip().splitlines()[-1])["wakeAgent"]
    dash = (tmp / "wiki" / "dashboards" / "source-portfolio.md").read_text()
    return wake, dash


def test_composition_sections_and_never_wakes(tmp_path, monkeypatch):
    s = tmp_path / "wiki" / "sources"
    _src(s, "a", signal_class="current-market-signal", source_kind="article", publisher="Reuters",
         reliability="A", ingested="2026-06-30")
    _src(s, "b", signal_class="historical-baseline", source_kind="blog", publisher="VendorBlog",
         reliability="C", ingested="2026-06-29")
    _src(s, "c", signal_class="historical-baseline", source_kind="blog", publisher="VendorBlog",
         reliability="C", ingested="2026-01-01")
    wake, dash = _run(tmp_path, monkeypatch)
    assert wake is False                                       # pure no_agent, never wakes
    assert "Headline distribution (by signal_class)" in dash   # signal_class present -> class axis
    assert "Source kind × signal class" in dash
    assert "Top 20 publishers" in dash and "VendorBlog" in dash
    assert "Reliability distribution" in dash
    assert "n_sources: 3" in dash


def test_generic_when_no_signal_class(tmp_path, monkeypatch):
    # a pack without signal_class still gets every section; the axis falls back to source_kind.
    s = tmp_path / "wiki" / "sources"
    _src(s, "a", source_kind="article", publisher="Reuters", ingested="2026-06-30")
    _src(s, "b", source_kind="filing", publisher="SEC", ingested="2026-06-30")
    wake, dash = _run(tmp_path, monkeypatch)
    assert wake is False
    assert "Headline distribution (by source_kind)" in dash     # no signal_class -> source_kind axis
    assert "Source kind distribution" in dash                   # crosstab collapses (no class cols)


def test_prediction_bearing_coverage(tmp_path, monkeypatch):
    s = tmp_path / "wiki" / "sources"
    _src(s, "cited", source_kind="article", ingested="2026-06-30")
    _src(s, "uncited", source_kind="article", ingested="2026-06-30")
    pr = tmp_path / "wiki" / "predictions"
    pr.mkdir(parents=True)
    # an OPEN prediction citing 'cited' in basis; a RESOLVED one must not count.
    (pr / "open.md").write_text(
        "---\ntype: prediction\nstatus: open\nbasis:\n- '[[sources/cited]]'\n---\n# p\n")
    (pr / "done.md").write_text(
        "---\ntype: prediction\nstatus: confirmed\nbasis:\n- '[[sources/uncited]]'\n---\n# p\n")
    _, dash = _run(tmp_path, monkeypatch)
    assert "Sources cited in `basis:` by an OPEN prediction: **1** of 2" in dash


def test_list_shaped_publisher_does_not_crash(tmp_path, monkeypatch):  # invariant-audit #29
    """The write path can store a LIST for an unquoted `publisher: [[wiki]]` value; a list key is
    unhashable and used to kill the whole lane. _collect must stringify these fields so the render
    degrades to one bucket instead of crashing."""
    m = _load("source_portfolio_watch", "scripts/cron/source_portfolio_watch.py")
    assert m._s(["entities/p/recorded-future"]) == "entities/p/recorded-future"
    assert m._s(None) == "(unset)" and m._s([]) == "(unset)"
    # a source page with a list-shaped publisher must not crash the run
    s = tmp_path / "wiki" / "sources"
    s.mkdir(parents=True)
    (s / "x.md").write_text(
        "---\ntype: source\nsignal_class: current-market-signal\nsource_kind: article\n"
        "publisher:\n  - entities/p/recorded-future\nreliability: A\ningested: 2026-06-30\n---\n# s\n")
    wake, dash = _run(tmp_path, monkeypatch)
    assert "recorded-future" in dash


def test_helpers_subdomains_dates_and_malformed_pages(tmp_path,monkeypatch):
    monkeypatch.setenv("WIKI_PATH",str(tmp_path))
    m=_load("source_portfolio_watch_edges","scripts/cron/source_portfolio_watch.py")
    assert m._bases("sources") == [tmp_path / "wiki" / "sources"]
    wiki=tmp_path/"wiki";wiki.mkdir()
    (wiki/"ordinary").mkdir()
    sub=wiki/"sub";sub.mkdir();(sub/"schema.yaml").write_text("{}")
    assert m._bases("sources")==[wiki/"sources",sub/"sources"]
    assert m._parse_fm("plain") is None
    assert m._parse_fm("---\n[bad\n---\n") is None
    assert m._parse_fm("---\n- x\n---\n") is None
    from datetime import date,datetime
    assert m._date(datetime(2026,1,2))==date(2026,1,2)
    assert m._date(date(2026,1,3))==date(2026,1,3)
    assert m._date("bad") is None and m._date(1) is None
    assert m._pct(1,0)=="0.0%"
    assert "0.0%" in m.render([],date(2026,1,1))


def test_collect_and_prediction_filters(tmp_path,monkeypatch):
    monkeypatch.setenv("WIKI_PATH",str(tmp_path))
    m=_load("source_portfolio_watch_filters","scripts/cron/source_portfolio_watch.py")
    sources=tmp_path/"wiki/sources";sources.mkdir(parents=True)
    (sources/"_skip.md").write_text("x")
    (sources/"plain.md").write_text("plain")
    _src(sources,"valid",source_kind="report",created="2026-01-01")
    predictions=tmp_path/"wiki/predictions";predictions.mkdir()
    (predictions/"_skip.md").write_text("x")
    (predictions/"plain.md").write_text("plain")
    (predictions/"wrong.md").write_text("---\ntype: note\n---\n")
    (predictions/"scalar.md").write_text("---\ntype: prediction\nbasis: scalar\n---\n")
    (predictions/"open.md").write_text(
      "---\ntype: prediction\nbasis: ['[[sources/path/valid.md#h|V]]']\n---\n")
    rows=m._collect()
    assert len(rows)==1 and rows[0]["ingested"].isoformat()=="2026-01-01"
    assert m._open_prediction_basis_slugs()=={"valid"}


def test_collect_tolerates_source_vanishing_mid_scan(tmp_path,monkeypatch):
    monkeypatch.setenv("WIKI_PATH",str(tmp_path))
    m=_load("source_portfolio_watch_race","scripts/cron/source_portfolio_watch.py")
    source=tmp_path/"wiki/sources/x.md";source.parent.mkdir(parents=True);source.write_text("---\ntype: source\n---\n")
    sub=tmp_path/"wiki/sub";sub.mkdir();(sub/"schema.yaml").write_text("{}")
    original=Path.read_text
    monkeypatch.setattr(Path,"read_text",lambda self,*a,**k:
                        (_ for _ in ()).throw(OSError()) if self==source else original(self,*a,**k))
    assert m._collect()==[]


def test_prediction_basis_tolerates_page_vanishing_mid_scan(tmp_path,monkeypatch):
    monkeypatch.setenv("WIKI_PATH",str(tmp_path))
    m=_load("source_portfolio_watch_prediction_race","scripts/cron/source_portfolio_watch.py")
    page=tmp_path/"wiki/predictions/x.md";page.parent.mkdir(parents=True);page.write_text("---\ntype: prediction\n---\n")
    original=Path.read_text
    monkeypatch.setattr(Path,"read_text",lambda self,*a,**k:
                        (_ for _ in ()).throw(OSError()) if self==page else original(self,*a,**k))
    assert m._open_prediction_basis_slugs()==set()
