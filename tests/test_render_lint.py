"""Unit tests for the render-lint checks (scripts/cron/render_lint.py).

The crawl itself is exercised against a live stack by the smoke harness; here we pin the pure
predicate — lint_html — that decides whether a rendered page is clean, since that's the logic that
must catch the real regression classes and must NOT false-positive on legitimate content.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "cron" / "render_lint.py"


def _load():
    spec = importlib.util.spec_from_file_location("render_lint", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rl = _load()


# ── clean pages must not be flagged ──────────────────────────────────────────

def test_clean_page_is_clean():
    html = '<h1>APT X</h1><p>Uses <a class="wl" data-page="entities/m/y">y</a> widely.</p>'
    assert rl.lint_html("entities/x", html) == []


def test_literal_wikilink_inside_code_is_allowed():
    """A `[[…]]` inside a <code> span is intentional (documenting a wikilink) — not a leak."""
    html = "<p>Author a link as <code>[[entities/a/b]]</code> in the body.</p>"
    assert rl.lint_html("docs/x", html) == []


# ── each real regression class is caught ─────────────────────────────────────

def test_escaped_wl_anchor_markup_is_flagged():
    """The HTML-in-the-UI bug: the builder's anchor got escaped and shows as literal text."""
    html = '<p>see &lt;a class="wl" data-page="entities/a/b"&gt;b&lt;/a&gt; here</p>'
    assert "wl-markup-leak" in rl.lint_html("entities/x", html)


def test_literal_wikilink_in_prose_is_flagged():
    html = "<p>Attributed to [[entities/a/apt-x]] last week.</p>"
    assert "literal-wikilink" in rl.lint_html("briefings/d", html)


def test_backtick_wikilink_residue_is_flagged():
    html = "<p>ref `[[entities/a/b]]` lingered.</p>"
    codes = rl.lint_html("x", html)
    assert "backtick-wikilink" in codes


def test_unresolved_embed_is_flagged():
    html = "<p>![[entities/a/apt-x]]</p>"
    assert "unresolved-embed" in rl.lint_html("dashboards/o", html)


# ── crawler: transient fetch failures retry, not false-positive ──────────────

def test_lint_one_retries_transient_fetch_error(monkeypatch):
    """A single-worker reader occasionally times out under a concurrent sweep; that's a crawler
    artifact, not a page defect. A fetch that fails once then succeeds must NOT be recorded as a
    fetch-error."""
    calls = {"n": 0}

    def flaky(url, timeout=60):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("transient")
        return {"html": "<p>clean</p>"}

    monkeypatch.setattr(rl, "_get_json", flaky)
    path, viol = rl._lint_one("http://r", "entities/a/x")
    assert viol == [], f"transient failure was not retried away: {viol}"


def test_lint_one_records_fetch_error_when_all_attempts_fail(monkeypatch):
    monkeypatch.setattr(rl, "_get_json", lambda url, timeout=60: (_ for _ in ()).throw(OSError("down")))
    path, viol = rl._lint_one("http://r", "entities/a/x", retries=2)
    assert viol == ["fetch-error"]


# ── report shape ─────────────────────────────────────────────────────────────

def test_report_clean_and_dirty():
    clean = rl.render_report(10, {}, "2026-01-01T00:00:00Z")
    assert "Clean" in clean and "10" in clean
    dirty = rl.render_report(3, {"entities/a/x": ["literal-wikilink"],
                                 "briefings/d": ["wl-markup-leak", "unresolved-embed"]},
                             "2026-01-01T00:00:00Z")
    assert "| literal-wikilink | 1 |" in dirty
    assert "| wl-markup-leak | 1 |" in dirty
    assert "entities/a/x" in dirty and "briefings/d" in dirty
