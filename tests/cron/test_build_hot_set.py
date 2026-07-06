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


# --- forecast-movement signals: status-filtered recent + due-soon open (predictions in HOT.md) ---

def _pred(p: Path, status, resolves_by=None, last_updated=None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntype: prediction\nstatus: {status}\nsubject: x\nconfidence: medium\n"
    if resolves_by:
        fm += f"resolves_by: {resolves_by}\n"
    if last_updated:
        fm += f"last_updated: {last_updated}\n"
    p.write_text(fm + "---\n# pred\n")


def test_select_recent_status_filter(tmp_path):
    """recent + status_values = recently *resolved* (recent-by-graded-date AND status in set)."""
    vault = tmp_path
    pr = vault / "wiki" / "predictions"
    t = TODAY.isoformat()
    _pred(pr / "graded-recent.md", "confirmed", last_updated=f"{t}T10:00:00Z")
    _pred(pr / "open-recent.md", "open", last_updated=f"{t}T10:00:00Z")   # recent but NOT resolved
    _pred(pr / "graded-old.md", "refuted", last_updated="2019-01-01")     # resolved but old
    m = _load(vault)
    rows = m._select_recent(
        {"namespace": "predictions", "date_field": "last_updated", "status_field": "status",
         "status_values": ["confirmed", "refuted", "partial", "expired-ungraded"]},
        TODAY - timedelta(days=3))
    assert {p.stem for _, p, _ in rows} == {"graded-recent"}


def test_select_open_due_within_includes_overdue_sorted(tmp_path):
    vault = tmp_path
    pr = vault / "wiki" / "predictions"
    _pred(pr / "due-soon.md", "open", resolves_by=(TODAY + timedelta(days=3)).isoformat())
    _pred(pr / "overdue.md", "open", resolves_by=(TODAY - timedelta(days=2)).isoformat())
    _pred(pr / "due-far.md", "open", resolves_by=(TODAY + timedelta(days=90)).isoformat())
    _pred(pr / "resolved.md", "confirmed", resolves_by=(TODAY + timedelta(days=3)).isoformat())
    m = _load(vault)
    rows = m._select_open(
        {"namespace": "predictions", "status_field": "status", "open_values": ["open", "active"],
         "secondary_field": "resolves_by", "due_within_days": 7})
    assert [p.stem for p, _ in rows] == ["overdue", "due-soon"]   # within horizon, soonest-first


def test_select_open_without_due_filter_unchanged(tmp_path):
    vault = tmp_path
    pr = vault / "wiki" / "predictions"
    _pred(pr / "a.md", "open", resolves_by=(TODAY + timedelta(days=200)).isoformat())
    m = _load(vault)
    rows = m._select_open({"namespace": "predictions", "status_field": "status",
                           "open_values": ["open"]})
    assert {p.stem for p, _ in rows} == {"a"}   # no due_within_days -> all open (backward-compatible)
