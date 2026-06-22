"""Regression: build_hot_set finds recent sources under ANY layout — including the
`<publisher>/<year>/<month>/<day>/` nesting (and `<publisher>/<slug>` flat) that
feed packs use, which the old by-date month-dir scan missed (#24)."""
import importlib.util
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BHS = REPO / "scripts" / "cron" / "build_hot_set.py"

TODAY = datetime.now(timezone.utc).date()
OLD = date(2019, 1, 1)


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    spec = importlib.util.spec_from_file_location("build_hot_set", BHS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_hot_set"] = m
    spec.loader.exec_module(m)
    return m


def _src(p: Path, published) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: source\npublished: {published}\n---\n# {p.stem}\n")


def test_path_upper_date_unit():
    m = _load(Path("/tmp"))
    assert m._path_upper_date("arxiv/2026/06/18/x.md") == date(2026, 6, 18)   # day granularity
    assert m._path_upper_date("2026/06/x.md") == date(2026, 6, 30)           # month -> end of month
    assert m._path_upper_date("openai-news/some-slug.md") is None           # no date hierarchy
    assert m._path_upper_date("entities/weapon/m/m777.md") is None          # letter shard, not a date


def test_select_recent_across_layouts(tmp_path):
    vault = tmp_path
    src = vault / "wiki" / "sources"
    ymd = TODAY.strftime("%Y/%m/%d")
    ym = TODAY.strftime("%Y/%m")
    # recent, in three different layouts
    _src(src / "arxiv" / ymd / "nested-recent.md", TODAY)           # publisher/Y/M/D (feed pack)
    _src(src / "openai-news" / "flat-recent.md", TODAY)             # publisher/<slug> (no date dirs)
    _src(src / ym / "classic-recent.md", TODAY)                     # classic Y/M
    # old, date-sharded -> must be skipped
    _src(src / "arxiv" / "2019" / "01" / "01" / "old-nested.md", OLD)
    _src(src / "openai-news" / "flat-old.md", OLD)                  # old by frontmatter, no date path

    m = _load(vault)
    rows = m._select_recent({"namespace": "sources", "date_field": "published"},
                            TODAY - timedelta(days=30))
    stems = {p.stem for _, p, _ in rows}
    assert stems == {"nested-recent", "flat-recent", "classic-recent"}, stems


def test_main_writes_hot_with_recent_sources(tmp_path):
    """End-to-end: main() renders the recent feed-pack sources into HOT.md."""
    vault = tmp_path
    ymd = TODAY.strftime("%Y/%m/%d")
    _src(vault / "wiki" / "sources" / "arxiv" / ymd / "want-better-data.md", TODAY)
    (vault / "wiki" / "entities").mkdir(parents=True)
    (vault / "wiki" / "predictions").mkdir(parents=True)
    m = _load(vault)
    assert m.main() == 0
    hot = (vault / "wiki" / "HOT.md").read_text()
    assert "want-better-data" in hot
    assert "Recent sources: **1**" in hot
