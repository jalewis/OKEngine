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

# A FIXED date, pinned into the module's single clock seam by _load(): a module-level
# `datetime.now()` here plus main()'s own read later failed whenever UTC midnight fell between
# them (okengine#1005 — the 30-day boundary page aged to 31 days mid-test).
TODAY = date(2026, 6, 15)
OLD = date(2019, 1, 1)


def _load(vault: Path, pin_clock: bool = True):
    os.environ["WIKI_PATH"] = str(vault)
    spec = importlib.util.spec_from_file_location("build_hot_set", BHS)
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_hot_set"] = m
    spec.loader.exec_module(m)
    if pin_clock:
        m._utc_today = lambda: TODAY   # the module's one clock seam: tests and code share a date
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


def test_unrecognized_section_kind_is_surfaced(tmp_path, capsys):  # okengine#326 [19]
    """A hot_set section whose `kind` isn't recent/open was SILENTLY DROPPED — the section vanished
    from HOT.md with no signal. Now it fails loud on the cron's stderr; the run still completes
    (best-effort HOT.md), so a typo'd or future kind is visible, not invisible."""
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "hot_set:\n"
        "  days: 30\n"
        "  sections:\n"
        "    - {kind: bogus, namespace: sources, title: Broken}\n"
        "    - {kind: recent, namespace: sources, date_field: published, title: Recent}\n",
        encoding="utf-8")
    m = _load(tmp_path)
    assert m.main() == 0                                  # best-effort: the run still completes
    err = capsys.readouterr().err
    assert "unrecognized kind" in err and "bogus" in err, err
    # the valid section still rendered (HOT.md written)
    assert (tmp_path / "wiki" / "HOT.md").is_file()


def test_helper_error_and_invalid_date_paths(tmp_path, monkeypatch):
    m=_load(tmp_path)
    assert m._hot_set_cfg()==m._DEFAULT_HOT_SET
    (tmp_path/"schema.yaml").write_text("[bad")
    assert m._hot_set_cfg()==m._DEFAULT_HOT_SET
    (tmp_path/"schema.yaml").write_text("hot_set: []\n")
    assert m._hot_set_cfg()==m._DEFAULT_HOT_SET
    for value in (None,"bad","2026-02-30"):
        assert m._d(value) is None
    assert m._resolve_date({"updated":"bad","last_updated":TODAY.isoformat()},"updated")==TODAY
    assert m._resolve_date({}, "") is None
    plain=tmp_path/"plain.md";plain.write_text("body");assert m._fm(plain)=={}
    bad=tmp_path/"bad.md";bad.write_text("---\n[bad\n---");assert m._fm(bad)=={}
    scalar=tmp_path/"scalar.md";scalar.write_text("---\n- x\n---");assert m._fm(scalar)=={}
    original_read = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **k: (_ for _ in ()).throw(OSError("gone"))
        if self == plain else original_read(self, *a, **k),
    )
    assert m._fm(plain) == {}
    assert m._select_recent({"namespace":"missing"},TODAY)==[]
    assert m._select_open({"namespace":"missing"})==[]
    assert m._path_upper_date("2026/02/31/x.md")==date(2026,2,28)


def test_index_filters_and_render_optional_columns(tmp_path, monkeypatch):
    wiki=tmp_path/"wiki";src=wiki/"sources";pred=wiki/"predictions"
    _src(src/"INDEX.md",TODAY);_src(src/"_skip.md",TODAY);_src(src/"keep.md",TODAY)
    _pred(pred/"INDEX.md","open");_pred(pred/"keep.md","open")
    (tmp_path/"schema.yaml").write_text(
      "hot_set:\n  days: 30\n  cap: 5\n  sections:\n"
      "  - {kind: open, namespace: predictions, open_values: [open], title: Open}\n"
      "  - {kind: recent, namespace: sources, date_field: published, show_type: true, show_status: true, title: Recent}\n")
    m=_load(tmp_path);monkeypatch.setattr(m.tz_lib,"deployment_now",lambda:datetime.now(timezone.utc))
    assert m.main()==0
    hot=(wiki/"HOT.md").read_text()
    assert "[keep]" in hot and "Type" in hot and "Status" in hot


def test_main_missing_wiki_returns_error(tmp_path):
    m=_load(tmp_path)
    assert m.main()==1


def test_retired_actors_excluded_from_hot_rows_counts_and_cap(tmp_path):
    """Recent/open rendering excludes every tombstone (false identities and the retired
    non-actor alike), not the live source or live roster."""
    (tmp_path / 'schema.yaml').write_text(
        'type_aliases: {threat_actor: actor}\n'
        'hot_set:\n  cap: 1\n  sections:\n'
        '  - {kind: recent, namespace: entities, title: Actors, date_field: updated}\n'
        '  - {kind: open, namespace: entities, title: Lifecycle, open_values: [active, tombstoned]}\n')
    root = tmp_path / 'wiki/entities'
    originals = {}
    for name, typ, status in [('retired', 'Actor', 'tombstoned'),
                              ('retired-alias', 'threat_actor', 'tombstoned'),
                              ('genuine', 'actor', 'active'), ('unknown', 'actor', 'unknown'),
                              ('missing', 'actor', ''), ('fourth', 'actor', 'active'), ('other', 'malware', 'tombstoned')]:
        p = root / f'{name}.md';p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\ntype: {typ}\nstatus: {status}\nupdated: {TODAY}\n---\n# {name}\n')
        originals[p] = p.read_bytes()
    source = tmp_path / 'wiki/sources/victim-report.md';_src(source, TODAY)
    originals[source] = source.read_bytes()
    m = _load(tmp_path)
    assert {p.stem for _, p, _ in m._select_recent({'namespace': 'entities', 'date_field': 'updated'}, TODAY)} == {'genuine', 'unknown', 'missing', 'fourth'}
    assert {p.stem for p, _ in m._select_open({'namespace': 'entities', 'open_values': ['active', 'tombstoned']})} == {'genuine', 'fourth'}
    assert m.main() == 0
    text = (tmp_path / 'wiki/HOT.md').read_text()
    assert 'Actors: **4**' in text and 'Lifecycle: **2**' in text
    assert '(entities/retired.md)' not in text and '(entities/retired-alias.md)' not in text
    assert '(entities/other.md)' not in text, 'a tombstoned non-actor is cold too (#994 [11])'
    rendered = [line for line in text.splitlines() if '(entities/' in line]
    assert len(rendered) == 2  # one eligible row per section, regardless of filesystem walk order
    assert all(any(f'(entities/{name}.md)' in line
                   for name in ('genuine', 'unknown', 'missing', 'fourth'))
               for line in rendered)
    assert m.main() == 0
    assert all(p.read_bytes() == data for p, data in originals.items())


def test_long_frontmatter_actor_is_judged_on_its_whole_envelope(tmp_path):
    """okengine#991 audit 2 [2]: a 4000-char read window turned a long actor's frontmatter into
    {}. A live actor then vanished from the recent section (no date), and a retired one was
    excluded only by accident. Both must be judged from the full envelope."""
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    root = tmp_path / 'wiki/entities'
    root.mkdir(parents=True)
    rows = '\n'.join(f'  - https://example.org/report/{i:05d}-a-fairly-long-report-slug'
                     for i in range(150))
    for name, status in (('long-live', 'active'), ('long-retired', 'tombstoned')):
        (root / f'{name}.md').write_text(
            f'---\ntype: actor\nupdated: {TODAY}\nsources:\n{rows}\nstatus: {status}\n---\n# x\n')
    assert (root / 'long-live.md').read_text().index('\n---\n# x') > 4000
    m = _load(tmp_path)
    recent = m._select_recent({'namespace': 'entities', 'date_field': 'updated'}, TODAY)
    assert {p.stem for _, p, _ in recent} == {'long-live'}


def _page(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{body}\n---\n# {p.stem}\n")


def test_tombstoned_non_actor_pages_are_cold_in_every_section(tmp_path):
    """okengine#994 [11]: HOT.md listed tombstoned sources/concepts as recent (live on two
    deployments) because only retired ACTORS were excluded, while tier_of calls any tombstone
    cold. Any tombstone, any type, any case, is excluded from recent and open sections."""
    (tmp_path / 'schema.yaml').write_text(
        'hot_set:\n  sections:\n'
        '  - {kind: recent, namespace: sources, date_field: published, title: Recent sources}\n'
        '  - {kind: recent, namespace: concepts, date_field: updated, title: Concepts}\n'
        '  - {kind: open, namespace: predictions, status_field: state, open_values: [open],'
        ' title: Open}\n'
        # The pack's tier block says the same thing: a hot_set definition of open that the tier
        # block contradicts is replaced by the tier's (okengine#1044 [7]/[12]).
        'tier:\n  namespaces:\n    predictions: {date_field: resolves_by, status_field: state,'
        ' open_values: [open], open_floor: hot}\n')
    w = tmp_path / 'wiki'
    _page(w / 'sources/2026/live.md', f'type: source\npublished: {TODAY}')
    _page(w / 'sources/2026/dead.md', f'type: source\nstatus: tombstoned\npublished: {TODAY}')
    _page(w / 'sources/feed/dead-upper.md',
          f'type: source\nstatus: " Tombstoned "\npublished: {TODAY}')
    _page(w / 'concepts/live-c.md', f'type: concept\nstatus: active\nupdated: {TODAY}')
    _page(w / 'concepts/dead-c.md', f'type: concept\nstatus: tombstoned\nlast_updated: {TODAY}')
    _page(w / 'predictions/live-p.md', 'type: prediction\nstate: open')
    _page(w / 'predictions/dead-p.md', 'type: prediction\nstate: open\nstatus: tombstoned')
    m = _load(tmp_path)
    assert m.main() == 0
    text = (w / 'HOT.md').read_text()
    assert 'Recent sources: **1**' in text and 'Concepts: **1**' in text and 'Open: **1**' in text
    for live in ('sources/2026/live', 'concepts/live-c', 'predictions/live-p'):
        assert f'({live}.md)' in text, f'live page {live} must stay in HOT.md'
    for dead in ('sources/2026/dead', 'sources/feed/dead-upper', 'concepts/dead-c',
                 'predictions/dead-p'):
        assert f'({dead}.md)' not in text, f'tombstoned page {dead} listed in HOT.md'


def test_hot_set_and_tier_of_agree_that_a_tombstone_is_cold(tmp_path):
    """HOT.md must not call a page hot that tier_of calls cold: one retirement predicate."""
    sys.path.insert(0, str(REPO / 'scripts' / 'cron'))
    import tier_lib
    m = _load(tmp_path)
    for fm in ({'status': 'tombstoned'}, {'status': 'TOMBSTONED', 'type': 'source'},
               {'status': ' tombstoned ', 'type': 'concept'}):
        assert m.is_tombstoned(fm) and tier_lib.is_tombstoned(fm), fm
    for fm in ({}, None, {'status': 'active'}, {'status': 'tombstone'}, {'status': None}):
        assert not m.is_tombstoned(fm) and not tier_lib.is_tombstoned(fm), fm


def test_selectors_skip_generated_pages_keep_walking_and_sort_newest_first(tmp_path):
    """Generated INDEX / pagination / underscore pages are never content rows, a skipped page must
    not end the walk (continue, not break), and recent rows are newest first."""
    w = tmp_path / 'wiki'
    days = [TODAY - timedelta(days=n) for n in range(4)]
    for i, d in enumerate(days):
        _src(w / f'sources/s{i}.md', d)
    for name in ('INDEX.md', 'INDEX-p02.md', '_about.md'):
        _src(w / f'sources/{name}', TODAY)
        _page(w / f'predictions/{name}', 'type: prediction\nstatus: open')
    _page(w / 'predictions/zz-open.md', 'type: prediction\nstatus: open')
    _page(w / 'predictions/aa-open.md', 'type: prediction\nstatus: open')
    m = _load(tmp_path)
    recent = m._select_recent({'namespace': 'sources', 'date_field': 'published'},
                              TODAY - timedelta(days=10))
    assert [p.stem for _, p, _ in recent] == ['s0', 's1', 's2', 's3']
    assert [dv for dv, _, _ in recent] == days
    opened = {p.name for p, _ in m._select_open({'namespace': 'predictions',
                                                  'open_values': ['open']})}
    # Only INDEX.md is pinned here; pagination/underscore pages in `open` are #994 [23]'s scope.
    assert {'zz-open.md', 'aa-open.md'} <= opened and 'INDEX.md' not in opened


def test_recent_includes_a_page_on_the_cutoff_day_and_skips_older_shards(tmp_path):
    w = tmp_path / 'wiki'
    cutoff = TODAY - timedelta(days=5)
    _src(w / f'sources/{cutoff:%Y/%m/%d}/edge.md', cutoff)
    old = cutoff - timedelta(days=40)
    _src(w / f'sources/{old:%Y/%m/%d}/stale.md', TODAY)   # shard predates cutoff: skipped cheaply
    m = _load(tmp_path)
    got = {p.stem for _, p, _ in m._select_recent({'namespace': 'sources'}, cutoff)}
    assert got == {'edge'}


def test_default_hot_set_window_and_cap(tmp_path, monkeypatch):
    """No `hot_set` in the schema: the engine default is a 30-day window capped at 300 rows."""
    monkeypatch.delenv('HOT_DAYS', raising=False)
    w = tmp_path / 'wiki'
    _src(w / 'sources/in.md', TODAY - timedelta(days=30))
    _src(w / 'sources/out.md', TODAY - timedelta(days=31))
    m = _load(tmp_path)
    assert m._hot_set_cfg()['days'] == 30 and m._hot_set_cfg()['cap'] == 300
    assert m.main() == 0
    text = (w / 'HOT.md').read_text()
    assert '(sources/in.md)' in text and '(sources/out.md)' not in text
    assert '(≤30d)' in text and '_Derived working set (last 30d + live)' in text
    for i in range(301):
        _page(w / f'predictions/p{i:03d}.md', 'type: prediction\nstatus: open')
    assert m.main() == 0
    rows = [ln for ln in (w / 'HOT.md').read_text().splitlines() if '(predictions/' in ln]
    assert len(rows) == 300
    assert 'Open / active predictions: **301**' in (w / 'HOT.md').read_text()


# --- okengine#1005: one clock per run, and tests pin it -------------------------------------------

def _clock(*instants: datetime):
    """A module-level `datetime` stand-in whose now() returns `instants` in turn (last one sticks):
    the wall clock as build_hot_set sees it, independent of the test's own date arithmetic."""
    seq = list(instants)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            t = seq.pop(0) if len(seq) > 1 else seq[0]
            return t.astimezone(tz) if tz else t

    return _Clock


def test_window_follows_the_callers_date_when_midnight_passes_before_main(tmp_path, monkeypatch):
    """okengine#1005: the test fixed its date D, then UTC midnight passed before main() ran. main()
    re-read the wall clock (D+1), so the 30-day boundary page became 31 days old and fell out of
    the window. A caller that pins `today` must get the window it pinned."""
    monkeypatch.delenv('HOT_DAYS', raising=False)
    d = date(2026, 3, 10)
    w = tmp_path / 'wiki'
    _src(w / 'sources/in.md', d - timedelta(days=30))
    _src(w / 'sources/out.md', d - timedelta(days=31))
    m = _load(tmp_path)
    monkeypatch.setattr(m, 'datetime', _clock(datetime(2026, 3, 11, 0, 0, 1, tzinfo=timezone.utc)))
    assert m.main(today=d) == 0
    text = (w / 'HOT.md').read_text()
    assert '(sources/in.md)' in text, 'the boundary page left the window: main() ignored `today`'
    assert '(sources/out.md)' not in text


def test_one_run_reads_the_clock_once_across_midnight(tmp_path, monkeypatch):
    """okengine#1005: main() and the due-soon horizon each read the clock, so a run straddling UTC
    midnight cut `recent` at day D and `open` at day D+1 — two windows in one HOT.md. One run reads
    the clock once: a prediction due D+1 is outside a 0-day horizon taken on D."""
    d = date(2026, 3, 10)
    w = tmp_path / 'wiki'
    _pred(w / 'predictions/tomorrow.md', 'open', resolves_by=(d + timedelta(days=1)).isoformat())
    _pred(w / 'predictions/today.md', 'open', resolves_by=d.isoformat())
    (tmp_path / 'schema.yaml').write_text(
        "hot_set:\n  days: 30\n  sections:\n"
        "  - {kind: open, namespace: predictions, open_values: [open], secondary_field: resolves_by,"
        " due_within_days: 0, title: Due}\n", encoding='utf-8')
    m = _load(tmp_path, pin_clock=False)   # exercise the real seam against a ticking clock
    monkeypatch.setattr(m, 'datetime', _clock(datetime(2026, 3, 10, 23, 59, 59, tzinfo=timezone.utc),
                                              datetime(2026, 3, 11, 0, 0, 1, tzinfo=timezone.utc)))
    assert m.main() == 0
    text = (w / 'HOT.md').read_text()
    assert '[today]' in text
    assert '[tomorrow]' not in text, 'the open horizon re-read the clock after midnight'


# --- okengine#1042 [11]: the cap keeps a reproducible set; [37]: open means what tier_of means ---

def _hot_rows(w: Path, prefix: str) -> list[str]:
    return [ln for ln in (w / 'HOT.md').read_text().splitlines() if f'({prefix}/' in ln]


def _reversed_walk(monkeypatch):
    """Walk every directory in the reverse of its natural order: what a restore, rsync or a sync
    to another filesystem can do to readdir order."""
    real = Path.rglob
    monkeypatch.setattr(Path, 'rglob', lambda self, pat: iter(sorted(real(self, pat), reverse=True)))


def test_the_capped_recent_set_does_not_depend_on_readdir_order(tmp_path, monkeypatch):
    """okengine#1042 [11]: 400 sources with the same `published` date, cap 300. The kept 300 were
    the first 300 the walk happened to yield, so a restored copy of the same vault loaded a
    different working set. The tie is broken by path, so both walks keep the same rows."""
    w = tmp_path / 'wiki'
    for i in range(400):
        _src(w / f'sources/s{i:03d}.md', TODAY)
    m = _load(tmp_path)
    assert m.main() == 0
    forward = _hot_rows(w, 'sources')
    _reversed_walk(monkeypatch)
    assert m.main() == 0
    assert _hot_rows(w, 'sources') == forward
    assert len(forward) == 300 and '(sources/s000.md)' in forward[0]


def test_the_capped_open_set_does_not_depend_on_readdir_order(tmp_path, monkeypatch):
    """[11]: an open section without `due_within_days` listed pages in walk order, so the cap
    dropped an arbitrary tail. Open rows sort by the secondary date (soonest first, undated last),
    then path, and the cap keeps the most urgent ones whatever the walk order."""
    w = tmp_path / 'wiki'
    for i in range(310):
        _pred(w / f'predictions/p{i:03d}.md', 'open',
              resolves_by=(TODAY + timedelta(days=400 - i)).isoformat())
    _pred(w / 'predictions/undated.md', 'open')
    m = _load(tmp_path)
    assert m.main() == 0
    forward = _hot_rows(w, 'predictions')
    _reversed_walk(monkeypatch)
    assert m.main() == 0
    assert _hot_rows(w, 'predictions') == forward
    assert '(predictions/p309.md)' in forward[0], 'the soonest-due page leads'
    assert not any('(predictions/undated.md)' in ln for ln in forward), 'undated sorts last'
    assert not any('(predictions/p000.md)' in ln for ln in forward), 'the latest-due falls off'


def test_a_truncated_section_says_how_many_rows_it_dropped(tmp_path):
    """[11]: the header counted 400 while the table held 300, with nothing saying rows were cut.
    A capped table now ends with a marker that names the shown and total counts; an uncapped one
    has none."""
    w = tmp_path / 'wiki'
    for i in range(305):
        _src(w / f'sources/s{i:03d}.md', TODAY)
    _pred(w / 'predictions/one.md', 'open')
    m = _load(tmp_path)
    assert m.main() == 0
    text = (w / 'HOT.md').read_text()
    assert 'Recent sources: **305**' in text
    assert '_Showing 300 of 305 (cap 300); 5 not listed._' in text
    assert text.count('_Showing ') == 1, 'an uncapped section carries no marker'


def test_the_fallback_open_values_are_the_composed_tier_open_values(tmp_path):
    """okengine#1042 [37]: with no pack `hot_set`, HOT.md listed `proposed`/`pending` predictions
    as open while tier_of (base-schema `tier.namespaces.predictions.open_values: [open, active]`)
    tiers them by date. The fallback's open set comes from the same composed tier block, so the
    two cannot disagree; a pack that widens its tier open set widens HOT.md with it."""
    sys.path.insert(0, str(REPO / 'scripts' / 'cron'))
    import tier_lib
    w = tmp_path / 'wiki'
    long_past = (TODAY - timedelta(days=3000)).isoformat()   # tier_of: cold unless open
    for status in ('open', 'active', 'proposed', 'pending'):
        _pred(w / f'predictions/{status}.md', status, resolves_by=long_past)
    m = _load(tmp_path)
    assert m.main() == 0
    listed = {s for s in ('open', 'active', 'proposed', 'pending')
              if f'(predictions/{s}.md)' in (w / 'HOT.md').read_text()}
    hot_by_tier = {s for s in ('open', 'active', 'proposed', 'pending')
                   if tier_lib.tier_of_file(w / f'predictions/{s}.md', w,
                                            tier_lib.load_cfg(tmp_path), TODAY) == 'hot'}
    assert listed == hot_by_tier == {'open', 'active'}
    (tmp_path / 'schema.yaml').write_text(
        'tier:\n  namespaces:\n    predictions: {date_field: resolves_by, status_field: status,'
        ' open_values: [open, active, proposed], open_floor: hot}\n')
    assert m.main() == 0
    assert '(predictions/proposed.md)' in (w / 'HOT.md').read_text()


def test_the_literal_fallback_open_values_mirror_base_schema():
    """[37]: the literal default (used only when no composed tier block names the namespace) must
    equal config/base-schema.yaml's tier open values, the way tier_lib's own fallback is pinned."""
    import yaml
    base = yaml.safe_load((REPO / 'config' / 'base-schema.yaml').read_text())
    m = _load(Path('/nonexistent-vault'))
    sec = next(s for s in m._DEFAULT_HOT_SET['sections'] if s['kind'] == 'open')
    assert sec['open_values'] == base['tier']['namespaces']['predictions']['open_values']


# okengine#1044 [7]/[12]: the [37] agreement held only on the no-`hot_set` fallback. A pack that
# declares its own `hot_set` (the pack skeleton ships one) had its open section used verbatim, so
# HOT.md and tier_of disagreed as soon as either side was edited. Acceptance set: the skeleton pack
# with a widened tier, a pack hot_set copying the old engine default, a status-field mismatch, and
# the legitimate cases that must stay as the pack wrote them.
_STATUSES = ('open', 'active', 'proposed', 'pending')


def _open_pred_vault(tmp_path: Path, schema_text: str) -> Path:
    long_past = (TODAY - timedelta(days=3000)).isoformat()   # tier_of: cold unless open
    for status in _STATUSES:
        _pred(tmp_path / 'wiki' / f'predictions/{status}.md', status, resolves_by=long_past)
    (tmp_path / 'schema.yaml').write_text(schema_text)
    return tmp_path


def _listed_and_hot(tmp_path: Path, m) -> tuple[set, set]:
    sys.path.insert(0, str(REPO / 'scripts' / 'cron'))
    import tier_lib
    w = tmp_path / 'wiki'
    text = (w / 'HOT.md').read_text()
    cfg = tier_lib.load_cfg(tmp_path)
    listed = {s for s in _STATUSES if f'(predictions/{s}.md)' in text}
    hot = {s for s in _STATUSES
           if tier_lib.tier_of_file(w / f'predictions/{s}.md', w, cfg, TODAY) == 'hot'}
    return listed, hot


def test_a_skeleton_pack_that_widens_its_tier_widens_hot_md(tmp_path):
    """[7]: the real pack skeleton declares `hot_set` with literal [open, active]. Widening only its
    tier block to add `proposed` made `kb_search --tier hot` call proposed pages hot while HOT.md
    omitted them."""
    import yaml
    skel = yaml.safe_load((REPO / 'templates' / 'pack' / 'skeleton' / 'schema.yaml').read_text())
    assert isinstance(skel.get('hot_set'), dict), 'the skeleton ships a hot_set block'
    skel['tier']['namespaces']['predictions']['open_values'] = ['open', 'active', 'proposed']
    vault = _open_pred_vault(tmp_path, yaml.safe_dump(skel))
    m = _load(vault)
    assert m.main() == 0
    listed, hot = _listed_and_hot(vault, m)
    assert listed == hot == {'open', 'active', 'proposed'}, (listed, hot)


def test_a_pack_hot_set_copying_the_old_default_lists_only_tier_open_pages(tmp_path):
    """[12]: a pack hot_set naming [open, active, proposed, pending] (the pre-[37] engine default)
    over the base tier block's [open, active] listed proposed/pending as open while tier_of dates
    them cold."""
    vault = _open_pred_vault(tmp_path, (
        'hot_set:\n  sections:\n'
        '    - {kind: open, namespace: predictions, status_field: status,'
        ' open_values: [open, active, proposed, pending], title: Open predictions}\n'))
    m = _load(vault)
    assert m.main() == 0
    listed, hot = _listed_and_hot(vault, m)
    assert listed == hot == {'open', 'active'}, (listed, hot)


def test_the_tier_status_field_wins_over_a_pack_hot_set_status_field(tmp_path):
    """[12]: the status field is half of the definition of open; a pack section reading another
    field disagreed with tier_of the same way."""
    vault = _open_pred_vault(tmp_path, (
        'hot_set:\n  sections:\n'
        '    - {kind: open, namespace: predictions, status_field: state,'
        ' open_values: [open, active], title: Open predictions}\n'))
    m = _load(vault)
    sec = next(s for s in m._hot_set_cfg()['sections'] if s['kind'] == 'open')
    assert (sec['status_field'], sec['open_values']) == ('status', ['open', 'active'])


def test_a_narrower_or_other_field_definition_is_reported_too(tmp_path, capsys):
    """Disagreement is any difference, not only a wider declared set."""
    for field, values in (('status', '[open]'), ('state', '[open, active]')):
        _open_pred_vault(tmp_path, (
            'hot_set:\n  sections:\n'
            f'    - {{kind: open, namespace: predictions, status_field: {field},'
            f' open_values: {values}, title: Open predictions}}\n'))
        _load(tmp_path)._hot_set_cfg()
        err = capsys.readouterr().err
        assert f"declares open as {field} in " in err and 'says status in' in err, (field, err)


def test_a_pack_hot_set_keeps_everything_but_the_definition_of_open(tmp_path):
    """Legitimate: a pack's own window, cap, titles, secondary field, due-soon horizon and recent
    sections are its presentation choices and survive verbatim."""
    vault = _open_pred_vault(tmp_path, (
        'hot_set:\n  days: 7\n  cap: 12\n  sections:\n'
        '    - {kind: open, namespace: predictions, status_field: status, open_values: [open],'
        ' secondary_field: resolves_by, due_within_days: 14, title: Due soon}\n'
        '    - {kind: recent, namespace: sources, date_field: published, title: Fresh}\n'))
    m = _load(vault)
    cfg = m._hot_set_cfg()
    assert (cfg['days'], cfg['cap']) == (7, 12)
    assert cfg['sections'][0] == {
        'kind': 'open', 'namespace': 'predictions', 'status_field': 'status',
        'open_values': ['open', 'active'], 'secondary_field': 'resolves_by',
        'due_within_days': 14, 'title': 'Due soon'}
    assert cfg['sections'][1] == {'kind': 'recent', 'namespace': 'sources',
                                  'date_field': 'published', 'title': 'Fresh'}


def test_an_open_section_over_an_untiered_namespace_is_left_as_written(tmp_path):
    """Legitimate: with no tier block for the namespace there is no other definition of open to
    disagree with, so the pack's own values stand (tier_of returns None for it)."""
    vault = _open_pred_vault(tmp_path, (
        'hot_set:\n  sections:\n'
        '    - {kind: open, namespace: tasks, status_field: phase, open_values: [todo, doing],'
        ' title: Open tasks}\n'))
    m = _load(vault)
    sec = m._hot_set_cfg()['sections'][0]
    assert (sec['status_field'], sec['open_values']) == ('phase', ['todo', 'doing'])


def test_a_disagreeing_pack_open_section_is_reported_on_stderr(tmp_path, capsys):
    """The override is not silent: the cron names the section whose declared definition of open
    was replaced by the tier block's, so the pack author can correct the schema."""
    vault = _open_pred_vault(tmp_path, (
        'hot_set:\n  sections:\n'
        '    - {kind: open, namespace: predictions, status_field: status,'
        ' open_values: [open, active, proposed], title: Open predictions}\n'))
    m = _load(vault)
    assert m.main() == 0
    err = capsys.readouterr().err
    assert "'Open predictions'" in err and 'tier.namespaces.predictions' in err, err
    assert 'declares open as status in ' in err, err
    assert "['open', 'active', 'proposed']" in err and "['open', 'active']" in err, err


def test_an_agreeing_pack_open_section_reports_nothing(tmp_path, capsys):
    """Order and case are not disagreement: tier_of compares lower-cased membership."""
    vault = _open_pred_vault(tmp_path, (
        'hot_set:\n  sections:\n'
        '    - {kind: open, namespace: predictions, status_field: status,'
        ' open_values: [Active, open], title: Open predictions}\n'))
    m = _load(vault)
    assert m.main() == 0
    assert 'tier.namespaces' not in capsys.readouterr().err


def test_an_open_floor_below_hot_is_reported_on_stderr(tmp_path, capsys):
    """tier_of tiers an open page at `open_floor`; under `warm` the open pages HOT.md lists are not
    hot. What HOT.md should show then is a design question (okengine#1044 follow-up); until it is
    answered the disagreement is reported, not silent."""
    for floor in ('warm', 'cold'):
        vault = _open_pred_vault(tmp_path, (
            'tier:\n  namespaces:\n    predictions: {date_field: resolves_by, status_field: status,'
            f' open_values: [open, active], open_floor: {floor}}}\n'))
        m = _load(vault)
        assert m.main() == 0
        err = capsys.readouterr().err
        assert f'open_floor: {floor}' in err and 'predictions' in err, err


# --- v0.15.14 audit: a pack's own hot_set is aligned without crashing on shapes v0.15.13 ran ---
# v0.15.14 made _with_tier_open_values walk a PACK's hot_set with bare indexing. v0.15.13 ran
# `hot_set: {days: 14}` (no sections) and a section without `kind` (reported DROPPED, okengine#326
# [19]); the alignment raised KeyError before main(), so HOT.md went stale. Run as cron-plus does:
# the real script in a subprocess, no test PYTHONPATH, cwd outside the repo.

def _run_cron(vault: Path):
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["WIKI_PATH"] = str(vault)
    return subprocess.run([sys.executable, str(BHS)], cwd=vault, env=env,
                          capture_output=True, text=True, timeout=120)


def _shaped_vault(tmp_path: Path, schema_text: str) -> Path:
    for status in _STATUSES:
        _pred(tmp_path / "wiki" / f"predictions/{status}.md", status)
    (tmp_path / "wiki" / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / "schema.yaml").write_text(schema_text, encoding="utf-8")
    return tmp_path


def test_a_pack_hot_set_without_sections_still_writes_hot_md(tmp_path):
    vault = _shaped_vault(tmp_path, "hot_set:\n  days: 14\n")
    proc = _run_cron(vault)
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr, proc.stderr
    assert (vault / "wiki" / "HOT.md").is_file()


def test_a_pack_section_missing_kind_is_reported_dropped_not_a_crash(tmp_path):
    vault = _shaped_vault(tmp_path, (
        "hot_set:\n  sections:\n"
        "    - {namespace: predictions, title: Kindless}\n"
        "    - {kind: open, namespace: predictions, title: Open predictions}\n"))
    proc = _run_cron(vault)
    assert proc.returncode == 0, proc.stderr
    assert "'Kindless' has unrecognized kind None" in proc.stderr and "DROPPED" in proc.stderr
    hot = (vault / "wiki" / "HOT.md").read_text(encoding="utf-8")
    assert "(predictions/open.md)" in hot and "(predictions/active.md)" in hot


def test_a_non_dict_pack_section_is_reported_dropped_not_a_crash(tmp_path):
    vault = _shaped_vault(tmp_path, (
        "hot_set:\n  sections:\n"
        "    - just-a-string\n"
        "    - {kind: open, namespace: predictions, title: Open predictions}\n"))
    proc = _run_cron(vault)
    assert proc.returncode == 0, proc.stderr
    assert "'just-a-string'" in proc.stderr and "DROPPED" in proc.stderr, proc.stderr
    assert "(predictions/open.md)" in (vault / "wiki" / "HOT.md").read_text(encoding="utf-8")


def test_a_scalar_string_tier_open_value_is_one_value_not_characters(tmp_path):
    """`open_values: active` is the one value `active`; list() made it a, c, t, i, v, e."""
    vault = _shaped_vault(tmp_path, (
        "tier:\n  namespaces:\n    predictions: {status_field: status, open_values: active}\n"
        "hot_set:\n  sections:\n"
        "    - {kind: open, namespace: predictions, open_values: [active], title: Open predictions}\n"))
    proc = _run_cron(vault)
    assert proc.returncode == 0, proc.stderr
    hot = (vault / "wiki" / "HOT.md").read_text(encoding="utf-8")
    assert "(predictions/active.md)" in hot, hot
    assert "(predictions/open.md)" not in hot, hot
    assert "declares open as" not in proc.stderr, proc.stderr   # [active] agrees with `active`


def test_alignment_tolerates_odd_shapes_in_process(tmp_path):
    """The same shapes at the function boundary, so coverage and mutation see them."""
    m = _load(tmp_path)
    tier = {"namespaces": {"predictions": {"open_values": "active"}, "x": {"open_values": 5}}}
    assert m._with_tier_open_values({"days": 14}, tier) == {"days": 14, "sections": []}
    assert m._with_tier_open_values({"sections": None}, tier) == {"sections": []}
    odd = ["s", {"namespace": "predictions"}, {"kind": "open"}]
    assert m._with_tier_open_values({"sections": odd}, tier)["sections"] == odd
    out = m._with_tier_open_values({"sections": [
        {"kind": "open", "namespace": "predictions"},
        {"kind": "open", "namespace": "x"}]}, tier)["sections"]
    assert out[0]["open_values"] == ["active"] and out[0]["status_field"] == "status"
    assert out[1]["open_values"] == [5]
    assert m._open_set({"status_field": "s", "open_values": "Active"}) == ("s", frozenset({"active"}))


def test_main_drops_a_non_dict_section_in_process(tmp_path, capsys):
    vault = _shaped_vault(tmp_path, (
        "hot_set:\n  sections:\n    - 7\n"
        "    - {kind: open, namespace: predictions, title: Open predictions}\n"))
    m = _load(vault)
    assert m.main() == 0
    assert "section 7 is not a mapping — DROPPED" in capsys.readouterr().err
    assert "Open predictions: **2**" in (vault / "wiki" / "HOT.md").read_text(encoding="utf-8")
