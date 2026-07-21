"""Unit tests for the render-lint checks (scripts/cron/render_lint.py).

The crawl itself is exercised against a live stack by the smoke harness; here we pin the pure
predicate — lint_html — that decides whether a rendered page is clean, since that's the logic that
must catch the real regression classes and must NOT false-positive on legitimate content.
"""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "cron" / "render_lint.py"


def _load():
    spec = importlib.util.spec_from_file_location("render_lint", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rl = _load()


def test_reader_fetch_rejects_non_http_scheme():
    with pytest.raises(ValueError, match=r"http\(s\)"):
        rl._get_json("file:///etc/passwd")


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


def test_escaped_wl_anchor_markup_inside_code_is_allowed():
    html = ('<pre><code class="language-markdown">'
            '&lt;a class="wl" data-page="entities/a/b"&gt;b&lt;/a&gt;'
            '</code></pre>')
    assert rl.lint_html("docs/x", html) == []


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


def test_crawl_retries_parallel_fetch_errors_after_pool_drains(monkeypatch):
    calls = {}

    def overloaded(_reader, path):
        calls[path] = calls.get(path, 0) + 1
        if calls[path] == 1 and path in {"transient", "real-defect"}:
            return path, ["fetch-error"]
        if path == "real-defect":
            return path, ["literal-wikilink"]
        return path, []

    monkeypatch.setattr(rl, "_lint_one", overloaded)
    offenders = rl.crawl("http://reader", ["clean", "transient", "real-defect"], workers=3)
    assert offenders == {"real-defect": ["literal-wikilink"]}
    assert calls == {"clean": 1, "transient": 2, "real-defect": 2}


def test_worker_defaults_bound_cron_pressure_and_preserve_on_demand(monkeypatch):
    monkeypatch.delenv("RENDER_LINT_WORKERS", raising=False)
    assert rl.default_workers(cron_mode=True) == 4
    assert rl.default_workers(cron_mode=False) == 16


def test_worker_default_honors_safe_operator_override(monkeypatch):
    monkeypatch.setenv("RENDER_LINT_WORKERS", "7")
    assert rl.default_workers(cron_mode=True) == 7
    monkeypatch.setenv("RENDER_LINT_WORKERS", "0")
    assert rl.default_workers(cron_mode=True) == 1
    monkeypatch.setenv("RENDER_LINT_WORKERS", "invalid")
    assert rl.default_workers(cron_mode=True) == 4


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


def test_partial_report_never_claims_clean():
    partial = rl.render_report(100, {}, "2026-01-01T00:00:00Z", checked=25, pending=75)
    assert "25** of **100" in partial and "75** pending" in partial
    assert "not yet a clean full-vault result" in partial
    assert "_No rendered-output defects. Clean._" not in partial


def test_incremental_plan_prioritizes_changed_before_unseen():
    state = {"version": 1, "pages": {
        "changed": {"updated": "old", "violations": []},
        "same": {"updated": "v1", "violations": []},
    }}
    records = [{"path": "unseen", "revision": "v1"},
               {"path": "same", "revision": "v1"},
               {"path": "changed", "revision": "v2"}]
    selected, current = rl.plan_incremental(records, state, batch_size=1)
    assert selected == ["changed"]
    assert current == {"unseen": "v1", "same": "v1", "changed": "v2"}


def test_incremental_plan_retries_fetch_errors_before_changed_pages():
    state = {"version": rl._STATE_VERSION, "pages": {
        "fetch": {"updated": "v1", "violations": ["fetch-error"]},
        "changed": {"updated": "old", "violations": []},
    }}
    records = [{"path": "changed", "revision": "v2"},
               {"path": "fetch", "revision": "v1"}]
    selected, _current = rl.plan_incremental(records, state, batch_size=1)
    assert selected == ["fetch"]


def test_incremental_apply_invalidates_changes_and_removes_deleted():
    state = {"version": 1, "pages": {
        "deleted": {"updated": "v1", "violations": ["literal-wikilink"]},
        "same": {"updated": "v1", "violations": ["wl-markup-leak"]},
        "changed": {"updated": "old", "violations": ["fetch-error"]},
    }}
    current = {"same": "v1", "changed": "v2", "new": "v1"}
    offenders, checked, pending = rl.apply_incremental(
        state, current, {"changed": [], "new": ["literal-wikilink"]}, "2026-01-01T00:00:00Z")
    assert "deleted" not in state["pages"]
    assert offenders == {"same": ["wl-markup-leak"], "new": ["literal-wikilink"]}
    assert checked == 3 and pending == 0
    assert state["last_full_sweep"] == "2026-01-01T00:00:00Z"


def test_incremental_partial_cycle_preserves_pending_without_false_full():
    state = {"version": 1, "pages": {}}
    current = {"a": "1", "b": "1"}
    offenders, checked, pending = rl.apply_incremental(
        state, current, {"a": []}, "2026-01-01T00:00:00Z")
    assert offenders == {} and checked == 1 and pending == 1
    assert "last_full_sweep" not in state
    assert state["cycle_started_at"] == "2026-01-01T00:00:00Z"


def test_state_roundtrip_and_corruption_rebuild(tmp_path, capsys):
    p = tmp_path / "state" / "render-lint.json"
    state = {"version": rl._STATE_VERSION, "pages": {"x": {"updated": "1", "violations": []}}}
    rl.save_state(p, state)
    assert rl.load_state(p) == state
    p.write_text("{broken")
    assert rl.load_state(p) == {"version": rl._STATE_VERSION, "pages": {}}
    assert "ignoring corrupt state" in capsys.readouterr().err
