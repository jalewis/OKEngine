"""okengine.timeline — dated-page collection + month-grouped dashboard render."""
import importlib.util
from datetime import date
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
                    (date(2026, 5, 1), "sources/a/s1", "source", "S1")])
    assert "type: dashboard" in out and "# Timeline" in out
    assert "## 2026-06" in out and "## 2026-05" in out and "[[entities/a/e1]]" in out


def test_render_empty(tmp_path):
    assert "No dated pages" in _mod().render([])
