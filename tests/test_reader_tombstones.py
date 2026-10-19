"""The reader hides tombstoned pages everywhere a reader would find them.

A tombstone is a page the write path retired but keeps on disk as history (`status: tombstoned`).
Before this, the rail counted it, the directory listing showed it, search matched it, backlinks
cited it, and a link to it rendered it as if it were live: a duplicate briefing tombstoned on a
live vault stayed one click away in its namespace.
"""
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
READER = REPO / "okengine-reader"

pytest.importorskip("fastapi")
pytest.importorskip("nh3")

LONG_SOURCES = "".join(f"- sources/2026/09/23/a-long-source-slug-number-{i:03d}\n" for i in range(120))


@pytest.fixture(autouse=True)
def _restore_env():
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _load(vault: Path):
    os.environ["VAULT_DIR"] = str(vault)
    sys.path.insert(0, str(READER))
    sys.modules.pop("app", None)
    spec = importlib.util.spec_from_file_location("app", READER / "app.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["app"] = m
    spec.loader.exec_module(m)
    return m


def _page(wiki: Path, key: str, *, status: str = "", sources: str = "", body: str = "") -> Path:
    path = wiki / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = f"type: briefing\ntitle: {Path(key).name}\n"
    if sources:
        fm += f"sources:\n{sources}"                 # a long list pushes `status` far down
    if status:
        fm += f"status: {status}\n"
    path.write_text(f"---\n{fm}---\n\n# {Path(key).name}\n{body}\n", encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "briefings/weekly/house-weekly-2026-09-29", body="needle")
    _page(wiki, "briefings/house-brief-2026-09-29", status="tombstoned", sources=LONG_SOURCES,
          body="needle [[briefings/weekly/house-weekly-2026-09-29]]")
    _page(wiki, "briefings/house-brief-2026-09-23", body="[[briefings/weekly/house-weekly-2026-09-29]]")
    return tmp_path


def test_status_below_a_long_sources_list_is_still_read(vault):
    m = _load(vault)
    page = vault / "wiki/briefings/house-brief-2026-09-29.md"
    assert page.read_text().index("status: tombstoned") > 4000   # past the old 4,000-char read
    assert m._tombstoned(page)
    assert not m._tombstoned(vault / "wiki/briefings/house-brief-2026-09-23.md")
    assert not m._tombstoned(vault / "wiki/briefings/missing.md")


def test_directory_listing_and_rail_count_exclude_tombstones(vault):
    m = _load(vault)
    listed = [p["path"] for p in m.api_pages(dir="briefings", group="")["pages"]]
    assert "briefings/house-brief-2026-09-29" not in listed
    assert sorted(listed) == ["briefings/house-brief-2026-09-23", "briefings/weekly/house-weekly-2026-09-29"]
    count = {d["dir"]: d["count"] for d in m.api_tree()["dirs"]}["briefings"]
    assert count == len(listed) == 2


def test_a_namespace_of_only_tombstones_drops_off_the_rail(tmp_path):
    _page(tmp_path / "wiki", "gone/only", status="tombstoned")
    _page(tmp_path / "wiki", "kept/page")
    m = _load(tmp_path)
    assert [d["dir"] for d in m.api_tree()["dirs"]] == ["kept"]


def test_a_link_to_a_tombstone_is_not_found(vault):
    m = _load(vault)
    for path in ("briefings/house-brief-2026-09-29", "house-brief-2026-09-29"):
        with pytest.raises(m.HTTPException) as error:
            m.api_page(path=path)
        assert error.value.status_code == 404, path
    assert m.api_page(path="briefings/house-brief-2026-09-23")["title"] == "house-brief-2026-09-23"


def test_search_skips_tombstoned_matches(vault, monkeypatch):
    m = _load(vault)
    monkeypatch.setattr(m, "_guard", lambda *_a, **_k: (lambda: None))
    wiki = vault / "wiki"
    hits = (f"{wiki}/briefings/house-brief-2026-09-29.md:130:needle\n"
            f"{wiki}/briefings/weekly/house-weekly-2026-09-29.md:5:needle\n")
    monkeypatch.setattr(m.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout=hits))
    result = m.api_search(SimpleNamespace(client=None), q="needle")
    assert [r["path"] for r in result["results"]] == ["briefings/weekly/house-weekly-2026-09-29"]
    assert result["total"] == 1


def test_backlinks_drop_a_tombstoned_source_even_from_a_stale_artifact(vault):
    """The backlinks-refresh artifact can predate the tombstone; the response must not cite it."""
    wiki = vault / "wiki"
    (wiki / ".backlinks.json").write_text(json.dumps({"backlinks": {
        "briefings/weekly/house-weekly-2026-09-29": [
            {"key": "briefings/house-brief-2026-09-23", "title": "old"},
            {"key": "briefings/house-brief-2026-09-29", "title": "dupe"}]}}))
    m = _load(vault)
    result = m.api_backlinks(path="briefings/weekly/house-weekly-2026-09-29")
    assert [r["key"] for r in result["backlinks"]] == ["briefings/house-brief-2026-09-23"]
    assert result["count"] == 1


HUGE_SOURCES = "".join(f"- sources/2026/09/23/a-long-source-slug-number-{i:04d}\n" for i in range(1000))


def test_a_tombstone_whose_envelope_runs_past_the_16k_head_is_hidden(tmp_path):
    """okengine#993: the reader read a 16384-byte head, so a tombstone with ~58 kB of frontmatter
    (live: a merged threat-actor page) parsed as {}: it was listed, untyped,
    and served on a direct link while every generator hid it."""
    wiki = tmp_path / "wiki"
    page = _page(wiki, "entities/s/h/huge-retired", status="tombstoned", sources=HUGE_SOURCES)
    _page(wiki, "entities/s/h/live-one")
    assert page.read_text().index("status: tombstoned") > 16384
    m = _load(tmp_path)
    assert m._tombstoned(page)
    assert [p["path"] for p in m.api_pages(dir="entities", group="")["pages"]] == ["entities/s/h/live-one"]
    with pytest.raises(m.HTTPException) as error:
        m.api_page(path="entities/s/h/huge-retired")
    assert error.value.status_code == 404


def test_a_live_page_with_a_huge_envelope_keeps_its_metadata(tmp_path):
    wiki = tmp_path / "wiki"
    _page(wiki, "entities/s/h/huge-live", status="active", sources=HUGE_SOURCES)
    m = _load(tmp_path)
    [row] = m.api_pages(dir="entities", group="")["pages"]
    assert (row["type"], row["status"], row["title"]) == ("briefing", "active", "huge-live")
