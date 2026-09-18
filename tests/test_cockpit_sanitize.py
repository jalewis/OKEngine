"""okengine#659: the cockpit renders agent- and feed-derived markdown into innerHTML, so its
markdown->HTML output MUST pass the same nh3 allowlist the reader applies. Before the fix the
cockpit shipped `nh3` in requirements.txt but never imported it -- stored XSS from any ingested
page on a `trust:public` deployment."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("yaml")
pytest.importorskip("nh3")

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


def _page(root: Path, rel: str, body: str) -> None:
    p = root / "wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: entity\ntitle: T\n---\n{body}\n", encoding="utf-8")


XSS_BODY = (
    "# Acme\n\n"
    "<script>alert(1)</script>\n\n"
    '<img src=x onerror="alert(2)">\n\n'
    "[click](javascript:alert(3))\n\n"
    '<a href="https://example.com" onclick="alert(4)">ok</a>\n\n'
    "| a | b |\n|---|---|\n| 1 | 2 |\n"
)


def test_render_md_strips_script_handlers_and_javascript_urls(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    html = m.render_md(XSS_BODY)
    assert "<script" not in html
    assert "onerror" not in html and "onclick" not in html
    assert "javascript:" not in html
    assert "alert(" not in html
    # legitimate structure survives the allowlist
    assert "<h1" in html and "<table" in html
    assert 'href="https://example.com"' in html


def test_api_page_html_is_sanitized_end_to_end(tmp_path, monkeypatch):
    """The served field is what app.js injects via innerHTML -- assert on it, not on a helper."""
    _page(tmp_path, "entities/a/acme.md", XSS_BODY)
    m = _load(tmp_path, monkeypatch)
    html = m.api_page("entities/a/acme")["html"]
    assert "<script" not in html and "onerror" not in html and "javascript:" not in html


def test_panel_svg_survives_but_scripts_inside_it_do_not(tmp_path, monkeypatch):
    """okengine.viz panel-svg blocks are static shapes; keep them, strip executable vectors.
    Mirrors tests/test_reader.py::test_inline_panel_svg_survives_sanitizer so the two UIs cannot
    drift apart on what an inline chart may contain."""
    (tmp_path / "wiki").mkdir()
    m = _load(tmp_path, monkeypatch)
    body = ('<!-- panel-svg v=abc123 -->\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 520" width="100%">\n'
            '<rect x="0" y="0" width="820" height="520" fill="#fafafa"/>\n'
            '<circle cx="200" cy="300" r="5" fill="#1d4ed8"/>\n'
            '<text x="209" y="304" font-size="11" fill="#111827">Node</text>\n'
            '<script>alert(1)</script>\n'
            '<circle cx="1" cy="1" r="1" onload="alert(2)"/>\n'
            '</svg>\n<!-- /panel-svg -->\n')
    html = m.render_md(body)
    assert "<svg" in html and "<circle" in html and "Node" in html
    assert "<script" not in html and "onload" not in html and "alert(1)" not in html
