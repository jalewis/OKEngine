"""Regression: the generic HTML article extractor (stdlib heuristic path)."""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
EH = REPO / "scripts" / "extract-html.py"


def _load():
    spec = importlib.util.spec_from_file_location("extract_html", EH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["extract_html"] = m
    spec.loader.exec_module(m)
    return m


_PAGE = """<html><head><title>T</title><script>var x=1;</script>
<style>.a{color:red}</style></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article>
<h1>The Headline</h1>
<p>This is the first paragraph of the real article body, long enough to be content.</p>
<p>Second paragraph with more substantive text that should be extracted cleanly.</p>
</article>
<footer>Copyright 2026 Example Corp</footer>
</body></html>"""


def test_heuristic_extracts_article_drops_boilerplate():
    m = _load()
    backend, text = m.extract_article(_PAGE)
    assert backend in ("trafilatura", "readability", "heuristic")
    assert "first paragraph of the real article body" in text
    assert "Second paragraph" in text
    # boilerplate is gone
    assert "var x=1" not in text and "color:red" not in text
    assert "Home" not in text and "About" not in text
    assert "Copyright" not in text


def test_prefers_article_zone_over_chrome():
    m = _load()
    # text outside <article> (and not sentence-like) must not leak in
    out = m._heuristic_extract(_PAGE)
    assert "The Headline" in out                  # inside <article>
    assert "Example Corp" not in out


def test_thin_boilerplate_only_page_is_short():
    m = _load()
    page = "<html><body><nav><a href='/'>Home</a><a href='/x'>X</a></nav></body></html>"
    _, text = m.extract_article(page)
    assert len(text) < 200                         # main() flags this as a failed extraction


def test_selector_without_lxml_falls_back_gracefully():
    m = _load()
    # if lxml/cssselect is absent, _by_selector returns None (no crash)
    res = m._by_selector(_PAGE, ".article-body")
    assert res is None or isinstance(res, str)


def test_backend_precedence_and_fallback(monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "_by_selector", lambda *_: "selected")
    assert m.extract_article("x", ".article")[0] == "selector"
    monkeypatch.setattr(m, "_by_selector", lambda *_: None)
    monkeypatch.setattr(m, "_try_trafilatura", lambda *_: "traf")
    assert m.extract_article("x", ".missing") == ("trafilatura", "traf")
    monkeypatch.setattr(m, "_try_trafilatura", lambda *_: None)
    monkeypatch.setattr(m, "_try_readability", lambda *_: "readable")
    assert m.extract_article("x") == ("readability", "readable")
    monkeypatch.setattr(m, "_try_readability", lambda *_: None)
    monkeypatch.setattr(m, "_heuristic_extract", lambda *_: "plain")
    assert m.extract_article("x") == ("heuristic", "plain")


def test_backend_adapters_and_heuristic_parser_errors(monkeypatch):
    m = _load()
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(
        extract=lambda html, **_kwargs: "traf text"))
    assert m._try_trafilatura("html") == "traf text"

    class Document:
        def __init__(self, _html): pass
        def summary(self): return "<article><p>Readable sentence.</p></article>"
    monkeypatch.setitem(sys.modules, "readability", SimpleNamespace(Document=Document))
    assert m._try_readability("html") == "Readable sentence."

    parser = m._Heuristic()
    parser._cur = [" "]
    parser._flush()
    assert parser._chunks == [] and parser._cur == []
    monkeypatch.setattr(m._Heuristic, "feed", lambda *_: (_ for _ in ()).throw(ValueError("bad")))
    assert m._heuristic_extract("broken") == ""

    node = SimpleNamespace(text_content=lambda: "one\n\n\n\ntwo")
    fake_html = SimpleNamespace(fromstring=lambda _html: SimpleNamespace(
        cssselect=lambda _css: [node]))
    monkeypatch.setitem(sys.modules, "lxml", SimpleNamespace(html=fake_html))
    monkeypatch.setitem(sys.modules, "lxml.html", fake_html)
    assert m._by_selector("html", ".x") == "one\n\ntwo"


def test_main_missing_root_dry_run_skip_extract_and_thin(
    tmp_path, monkeypatch, capsys
):
    m = _load()
    assert m.main([str(tmp_path / "missing")]) == 1
    assert "raw root not found" in capsys.readouterr().err

    raw = tmp_path / "raw"
    raw.mkdir()
    first = raw / "first.html"
    first.write_text(_PAGE)
    ignored = raw / "note.txt"
    ignored.write_text("not html")
    assert m.main(["--dry-run", str(raw)]) == 0
    output = capsys.readouterr().out
    assert "DRY:" in output and "1 HTML files scanned" in output

    companion = first.with_name("first.html.txt")
    companion.write_text("existing")
    os.utime(companion, (first.stat().st_mtime + 10,) * 2)
    assert m.main([str(raw)]) == 0
    assert "skipped (companion newer): 1" in capsys.readouterr().out

    second = raw / "second.htm"
    second.write_text("<article>tiny</article>")
    monkeypatch.setattr(m, "extract_article", lambda *_: ("heuristic", "tiny"))
    assert m.main(["--force", "--min-chars", "10", str(raw)]) == 1
    assert "failed (thin/unreadable): 2" in capsys.readouterr().out

    monkeypatch.setattr(m, "extract_article", lambda *_: ("heuristic", "long enough"))
    assert m.main(["--force", "--min-chars", "5", str(raw)]) == 0
    output = capsys.readouterr().out
    assert "extracted: 2" in output
    assert "stdlib heuristic" in output
    assert first.with_name("first.html.txt").read_text() == "long enough\n"


def test_main_unreadable_and_progress_reporting(tmp_path, monkeypatch, capsys):
    m = _load()
    raw = tmp_path / "raw"; raw.mkdir()
    unreadable = raw / "unreadable.html"; unreadable.write_text("html")
    for index in range(100):
        (raw / f"{index:03}.html").write_text("<article>content</article>")
    monkeypatch.setattr(m, "extract_article", lambda *_: ("heuristic", "long enough"))
    original = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda path, *args, **kwargs:
                        (_ for _ in ()).throw(OSError("unreadable"))
                        if path == unreadable else original(path, *args, **kwargs))
    assert m.main(["--force", "--min-chars", "1", str(raw)]) == 1
    output = capsys.readouterr().out
    assert "... 100 extracted" in output
    assert "failed (thin/unreadable): 1" in output
