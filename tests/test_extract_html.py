"""Regression: the generic HTML article extractor (stdlib heuristic path)."""
import importlib.util
import sys
from pathlib import Path

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
