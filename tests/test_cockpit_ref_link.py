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
    mod._REF_TITLE_CACHE.clear()
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


def _title_of(monkeypatch, tmp_path, text):
    page = tmp_path / "wiki" / "refs" / "p.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    return _app(monkeypatch, tmp_path / "wiki")._ref_title("refs/p")


@pytest.mark.parametrize("text, title", [
    ("# no frontmatter\ntitle: Body Line\n", ""),
    ("Plain prose first\ntitle: Body Line\n", ""),
    ("---\ntype: actor\n---\ntitle: Body Line\n", ""),
    ("---\ntitle: APT29: Cozy Bear\n---\n", "APT29: Cozy Bear"),
    ("---\ntitle: \"Quoted\"\n---\n", "Quoted"),
    ("---\n" + "k: v\n" * 399 + "title: Last Line In Window\n---\n", "Last Line In Window"),
    ("---\n" + "k: v\n" * 400 + "title: Past The Window\n---\n", ""),
    ("---\ntype: actor\n", ""),
], ids=["no-frontmatter", "prose-first", "title-in-body", "colon-in-title", "quoted", "line-400",
        "line-401", "unterminated"])
def test_ref_title_reads_only_the_frontmatter_title(monkeypatch, tmp_path, text, title):
    """The frontmatter window is the first 400 lines after the opening `---`; a `title:` in the
    body, past the window, or without frontmatter is not the page's title."""
    assert _title_of(monkeypatch, tmp_path, text) == title


def test_an_unreadable_ref_has_no_title(monkeypatch, tmp_path):
    page = tmp_path / "wiki" / "refs" / "p.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntitle: X\n---\n", encoding="utf-8")
    mod = _app(monkeypatch, tmp_path / "wiki")
    real_open = Path.open

    def failing(self, *a, **kw):
        if self.name == "p.md":
            raise OSError(5, "EIO")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", failing)
    assert mod._ref_title("refs/p") == ""


_LONG = ("Operation Shadow Harvest: a long-running espionage campaign targeting telecom operators "
         "in South Asia")


@pytest.mark.parametrize("fm", [
    {"type": "campaign", "title": _LONG},
    {"type": "campaign", "title": _LONG.replace(": ", " - ")},
    {"type": "report", "title": _LONG + " " + _LONG},
], ids=["quoted-wrap", "plain-wrap", "multi-line-wrap"])
def test_a_title_the_write_path_wrapped_is_read_whole(monkeypatch, tmp_path, fm):
    """okengine#1042 [26]: the write path dumps frontmatter with yaml.safe_dump (default width), so a
    long title continues on the next line. Reading only the `title:` line cut it mid-sentence and
    the revalidating memo then served the cut title for as long as the file was unchanged."""
    import yaml
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    assert "\n  " in dumped, "the fixture must reproduce the write path's wrapped title"
    assert _title_of(monkeypatch, tmp_path, f"---\n{dumped}---\n# body\n") == fm["title"]


def test_a_non_string_title_is_rendered_as_text(monkeypatch, tmp_path):
    assert _title_of(monkeypatch, tmp_path, "---\ntitle: 2024\n---\n") == "2024"
    assert _title_of(monkeypatch, tmp_path, "---\ntitle:\n---\n") == ""


@pytest.mark.parametrize("text, title", [
    ("---\nkind: x\ntitle: APT29: Cozy Bear\n---\n", "APT29: Cozy Bear"),
    ("---\nkind: a: b\n---\n", ""),
    ("---\n- a list\n---\n", ""),
    # Valid YAML whose timestamp constructor raises ValueError, not YAMLError: an impossible date
    # beside the title took the whole assessments tab down with a 500 (v0.15.13 audit).
    ("---\ntitle: APT29\nfirst_seen: 2026-02-30\n---\n", "APT29"),
    ("---\nfirst_seen: 2026-13-01\n---\n", ""),
], ids=["invalid-yaml-title-later", "invalid-yaml-no-title", "not-a-mapping", "impossible-date",
        "impossible-date-no-title"])
def test_frontmatter_that_is_not_a_yaml_mapping_falls_back_to_the_title_line(monkeypatch, tmp_path,
                                                                              text, title):
    """[26]: hand-written frontmatter that YAML cannot parse keeps the old line read."""
    assert _title_of(monkeypatch, tmp_path, text) == title


def test_a_title_ahead_of_a_frontmatter_longer_than_the_window_is_still_read(monkeypatch, tmp_path):
    """[26] review: a 450-source actor has frontmatter past the 400-line window. Its title line sits
    inside the window and was read before the YAML parse; the parse must not lose it."""
    sources = "".join(f"  - sources/s{i:03d}\n" for i in range(450))
    text = f"---\ntype: actor\ntitle: LockBit\nsources:\n{sources}---\n# body\n"
    assert _title_of(monkeypatch, tmp_path, text) == "LockBit"


def test_a_wrapped_title_closing_exactly_at_the_window_is_parsed_whole(monkeypatch, tmp_path):
    """[26]: frontmatter that fills the 400-line window and closes on the next line is complete,
    so it is parsed as YAML and a wrapped title is read whole, not from its first line."""
    import yaml
    dumped = yaml.safe_dump({"title": _LONG}, allow_unicode=True)
    assert dumped.count("\n") == 2
    text = "---\n" + "k: v\n" * 398 + dumped + "---\n"
    assert _title_of(monkeypatch, tmp_path, text) == _LONG


@pytest.mark.parametrize("after", ["k: v\n", "# a note\n"], ids=["key", "comment"])
def test_frontmatter_that_runs_past_the_window_is_never_parsed_as_yaml(monkeypatch, tmp_path,
                                                                       after):
    """[26]: a window that ends WITHOUT the closing fence holds a partial document, so the title is
    read from its line (YAML of a cut-off document is not the page's frontmatter). A wrapped title
    there keeps its first line; whatever follows the window is not a fence."""
    import yaml
    dumped = yaml.safe_dump({"title": _LONG}, allow_unicode=True)
    text = "---\n" + dumped + "k: v\n" * 398 + after + "---\n"
    first_line = dumped.splitlines()[0].split(":", 1)[1].strip().strip("\"'")
    assert _title_of(monkeypatch, tmp_path, text) == first_line != _LONG
