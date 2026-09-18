"""okengine.timeline — dated-page collection + month-grouped dashboard render."""
import importlib.util
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "build_timeline", REPO / "extensions" / "okengine.timeline" / "build_timeline.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _page(wiki, rel, fm):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\nbody\n"
    p.write_text(body, encoding="utf-8")


def test_collect_dated_newest_first_excludes_derived(tmp_path):
    m = _mod(); wiki = tmp_path / "wiki"
    _page(wiki, "sources/a/s1.md", {"type": "source", "title": "S1", "published": "2026-06-01"})
    _page(wiki, "entities/a/e1.md", {"type": "model", "title": "E1", "updated": "2026-06-15"})
    _page(wiki, "entities/a/nodate.md", {"type": "model", "title": "ND"})           # no date → skip
    _page(wiki, "dashboards/x.md", {"type": "dashboard", "updated": "2026-06-20"})  # excluded ns
    slugs = [s for _, s, _, _ in m.collect(wiki)]
    assert slugs == ["entities/a/e1", "sources/a/s1"]


def test_render_groups_by_month(tmp_path):
    m = _mod()
    out = m.render([(date(2026, 6, 15), "entities/a/e1", "model", "E1"),
                    (date(2026, 6, 14), "entities/a/e2", "model", "E2"),
                    (date(2026, 5, 1), "sources/a/s1", "source", "S1")])
    assert "type: dashboard" in out and "# Timeline" in out
    assert "## 2026-06" in out and "## 2026-05" in out and "[[entities/a/e1]]" in out


def test_render_empty(tmp_path):
    assert "No dated pages" in _mod().render([])


def test_frontmatter_and_date_defensive_edges(monkeypatch):
    m = _mod()
    assert m.frontmatter("body") == {}
    monkeypatch.setattr(m, "yaml", None)
    assert m.frontmatter("---\ntype: source\n---\n") == {}
    monkeypatch.undo()
    m = _mod()
    assert m.frontmatter("---\n- list\n---\n") == {}
    assert m.frontmatter("---\n[broken\n---\n") == {}
    instant = datetime(2026, 8, 4, 12, 0)
    assert m.to_date(instant) == date(2026, 8, 4)
    assert m.to_date(date(2026, 8, 3)) == date(2026, 8, 3)
    assert m.to_date("published 2026-08-02T00:00Z") == date(2026, 8, 2)
    assert m.to_date(None) is None and m.to_date("2026-02-31") is None


def test_collect_fallbacks_read_error_and_missing_tree(tmp_path, monkeypatch):
    m = _mod()
    assert m.collect(tmp_path / "absent") == []
    wiki = tmp_path / "wiki"
    _page(wiki, "operational/skip.md", {"created": "2026-01-01"})
    _page(wiki, "concepts/no-fm.md", {})
    _page(wiki, "concepts/created.md", {"created": "2026-08-01"})
    bad = wiki / "concepts/unreadable.md"
    bad.write_text("---\ncreated: 2026-08-02\n---\n")
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == bad:
            raise OSError("gone")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    rows = m.collect(wiki)
    assert rows == [(date(2026, 8, 1), "concepts/created", "page", "created")]


def test_render_limit_and_main_write(tmp_path, monkeypatch, capsys):
    m = _mod()
    monkeypatch.setattr(m, "MAX_ENTRIES", 1)
    rows = [(date(2026, 8, 2), "entities/b", "entity", "B"),
            (date(2026, 8, 1), "entities/a", "entity", "A")]
    rendered = m.render(rows)
    assert "[[entities/b]]" in rendered and "[[entities/a]]" not in rendered
    assert "1 older pages omitted" in rendered
    monkeypatch.setattr(m, "WIKI", tmp_path / "wiki")
    monkeypatch.setattr(m, "DASH_PATH", tmp_path / "wiki/dashboards/timeline.md")
    monkeypatch.setattr(m, "collect", lambda _wiki: rows)
    assert m.main() == 0
    assert "2 dated page" in capsys.readouterr().out
    assert m.DASH_PATH.is_file()
