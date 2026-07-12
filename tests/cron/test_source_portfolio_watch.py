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
