"""Regression: a relationship column must link the OTHER end of the relationship."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "okengine-cockpit" / "app.py"
pytestmark = pytest.mark.skipif(not APP.is_file(), reason="cockpit absent")
fastapi = pytest.importorskip("fastapi")


def _app(monkeypatch, wiki: Path):
    sys.path.insert(0, str(APP.parent))
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "WIKI", wiki)
    mod._ref_title.cache_clear()
    return mod


def test_ref_link_renders_the_target_not_the_row(monkeypatch, tmp_path):
    """`link: true` links the ROW's page and ignores `field` — right for a title column, wrong for
    every relationship column. Pointing `attributed_to` at it rendered the campaign's own name in
    three consecutive columns."""
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    (wiki / "entities" / "a" / "apt29.md").write_text("---\ntype: actor\ntitle: APT29\n---\n",
                                                      encoding="utf-8")
    mod = _app(monkeypatch, wiki)
    html = mod._ds_cell({"attributed_to": "entities/a/apt29", "title": "Some campaign"},
                        {"field": "attributed_to", "ref_link": True})
    assert "APT29" in html and "Some campaign" not in html
    assert 'data-page="entities/a/apt29"' in html


def test_a_title_behind_a_long_alias_list_still_resolves(monkeypatch, tmp_path):
    """Frontmatter length is bounded by the frontmatter; file length is not.

    A first version capped the read at 2048 bytes and silently missed `scattered-spider`, whose
    `title:` sits at line 67 behind a 60-alias list — the board fell back to the slug and showed
    "scattered spider" beside properly titled peers.
    """
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "s").mkdir(parents=True)
    aliases = "".join(f"- ALIAS{i}\n" for i in range(80))
    (wiki / "entities" / "s" / "spider.md").write_text(
        f"---\ntype: actor\naliases:\n{aliases}title: Scattered Spider\n---\n", encoding="utf-8")
    mod = _app(monkeypatch, wiki)
    assert mod._ref_title("entities/s/spider") == "Scattered Spider"


def test_an_unresolvable_ref_reads_as_what_it_pointed_at(monkeypatch, tmp_path):
    """A missing target is information, not absence — it must not vanish to an em-dash."""
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    mod = _app(monkeypatch, wiki)
    html = mod._ds_cell({"attributed_to": "entities/g/gone-actor"},
                        {"field": "attributed_to", "ref_link": True})
    assert "gone actor" in html


def test_an_empty_ref_falls_back_to_the_columns_empty_text(monkeypatch, tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    mod = _app(monkeypatch, wiki)
    html = mod._ds_cell({}, {"field": "attributed_to", "ref_link": True,
                             "empty": "Attribution open"})
    assert html == "Attribution open"
