"""build_index_tree — INDEX page-list excludes structural + _-prefixed scaffolding."""
import importlib, os, pathlib, sys
from datetime import datetime, timezone


def test_index_excludes_underscore_scaffolding(tmp_path, monkeypatch):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    assert bit._listable("supply-chain-integrity-drift.md")   # a real finding -> listed
    assert not bit._listable("_about.md")                     # namespace card -> excluded
    assert not bit._listable("INDEX.md")                      # structural -> excluded
    assert not bit._listable("_review-queue.md")              # operational scaffold -> excluded


def test_index_emits_fullpath_wikilinks(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    (tmp_path / "wiki" / "lacuna" / "drift.md").write_text("---\ntype: lacuna\ntitle: Drift\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(tmp_path / "wiki" / "lacuna", "now")
    idx = (tmp_path / "wiki" / "lacuna" / "INDEX.md").read_text()
    assert "[[lacuna/drift|drift]]" in idx        # full-path wikilink (cockpit + reader resolve it)
    assert "(drift.md)" not in idx                # not a relative markdown link


def test_index_shows_a_date_from_the_autostamp(tmp_path, monkeypatch):
    # A page whose slug carries no date (lacuna/entity/concept) still shows WHEN in the INDEX,
    # keyed off the write-path auto-stamp (last_updated/created) — the reader-parity date column.
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    (tmp_path / "wiki" / "lacuna" / "drift.md").write_text(
        "---\ntype: lacuna\ntitle: Drift\nlast_updated: '2026-07-01T01:34:15Z'\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(tmp_path / "wiki" / "lacuna", "now")
    idx = (tmp_path / "wiki" / "lacuna" / "INDEX.md").read_text()
    assert "| Page | Type | Created | Updated | Title |" in idx   # both date columns
    assert "2026-07-01" in idx                           # date rendered (YYYY-MM-DD, not the full ts)
    assert "01:34:15" not in idx                         # trimmed to date, no time noise


def test_index_sorts_newest_created_first(tmp_path, monkeypatch):
    # An INDEX's first job is surfacing NEW pages: rows order by `created` desc (published as
    # the fallback), pages without either sort last.
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    w = tmp_path / "wiki" / "lacuna"
    (w / "old.md").write_text("---\ntype: lacuna\ntitle: Old\ncreated: '2026-06-01T00:00:00Z'\n---\n")
    (w / "new.md").write_text("---\ntype: lacuna\ntitle: New\ncreated: '2026-07-02T00:00:00Z'\n---\n")
    (w / "mid.md").write_text("---\ntype: lacuna\ntitle: Mid\npublished: 2026-06-15\n---\n")
    (w / "undated.md").write_text("---\ntype: lacuna\ntitle: Undated\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(w, "now")
    idx = (w / "INDEX.md").read_text()
    positions = {s: idx.index(f"|{s}]]") for s in ("new", "mid", "old", "undated")}
    assert positions["new"] < positions["mid"] < positions["old"] < positions["undated"]
    assert "| 2026-07-02 |" in idx                       # the Created column carries the stamp


def test_index_folds_about(tmp_path, monkeypatch):
    (tmp_path / "wiki" / "lacuna").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("types:\n  source: {}\n")
    (tmp_path / "wiki" / "lacuna" / "_about.md").write_text(
        "---\ntype: about\n---\n# Lacuna\nStructural-gap discovery here.\n")
    (tmp_path / "wiki" / "lacuna" / "drift.md").write_text("---\ntype: lacuna\ntitle: Drift\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    import importlib, pathlib as _pl, sys as _sys
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(tmp_path / "wiki" / "lacuna", "now")
    idx = (tmp_path / "wiki" / "lacuna" / "INDEX.md").read_text()
    assert "Structural-gap discovery here." in idx   # _about body folded in
    assert "[[lacuna/drift|drift]]" in idx           # finding still listed


def test_main_writes_complete_structure_and_paginated_indexes(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki"
    ns = wiki / "entities" / "a"
    ns.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text("exclude: [operational]\n")
    for name in ("one", "two", "three"):
        (ns / f"{name}.md").write_text(
            f"---\ntype: entity\ntitle: {name.title()}\ncreated: 2026-07-24\n---\n")
    (wiki / "operational").mkdir()
    (wiki / "operational" / "hidden.md").write_text("---\ntype: dashboard\n---\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    monkeypatch.setattr(bit, "MAX_ENTRIES", 2)
    monkeypatch.setattr(
        bit.tz_lib, "deployment_now",
        lambda: datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc),
    )

    assert bit.main() == 0
    assert (ns / "INDEX-p02.md").is_file()
    assert "entities/" in (wiki / "INDEX.md").read_text()
    assert "operational/" not in (wiki / "INDEX.md").read_text()
    for name in ("BUNDLE.md", "HEALTH.md", "AGENTS.md"):
        assert (wiki / name).is_file()
    assert '"wakeAgent": false' in capsys.readouterr().out


def test_main_rejects_missing_wiki(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    assert bit.main() == 1
    assert "wiki not found" in capsys.readouterr().err


def test_frontmatter_and_namespace_discovery_edge_paths(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    empty = wiki / "empty"; empty.mkdir(parents=True)
    populated = wiki / "populated"; populated.mkdir()
    (populated / "page.md").write_text("body")
    (wiki / "plain-file").write_text("not a directory")
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    assert bit._discover_namespaces(wiki) == ["populated"]
    assert bit._fm(populated / "page.md") == {}
    bad = populated / "bad.md"; bad.write_text("---\n[broken\n---\n")
    assert bit._fm(bad) == {}
    unreadable = populated / "unreadable.md"; unreadable.mkdir()
    assert bit._fm(unreadable) == {}


def test_empty_about_and_disappearing_namespace_are_tolerated(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"; ns = wiki / "ns"; ns.mkdir(parents=True)
    (ns / "_about.md").write_text("---\ntype: about\n---\n")
    (tmp_path / "schema.yaml").write_text("types: {}\n")
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
    bit = importlib.reload(importlib.import_module("build_index_tree"))
    bit.gen_index(ns, "now")
    monkeypatch.setattr(bit, "_discover_namespaces", lambda _wiki: ["missing"])
    monkeypatch.setattr(bit.tz_lib, "deployment_now", lambda: datetime.now(timezone.utc))
    assert bit.main() == 0
    assert "missing/" not in (wiki / "INDEX.md").read_text()


def _load_retirement_tree(vault, monkeypatch):
    monkeypatch.setenv('WIKI_PATH', str(vault))
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'scripts/cron'))
    return importlib.reload(importlib.import_module('build_index_tree'))


def test_retired_actors_filtered_before_index_counts_and_pagination(tmp_path, monkeypatch):
    (tmp_path / 'schema.yaml').write_text('type_aliases: {threat_actor: actor}\n')
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    originals = {}
    for name, typ, status in [('retired', 'Actor', 'tombstoned'),
                              ('alias', 'threat_actor', ' Tombstoned '),
                              ('one', 'actor', 'active'), ('two', 'actor', 'unknown'),
                              ('three', 'actor', ''), ('fourth', 'actor', 'active'), ('other', 'malware', 'tombstoned')]:
        p = ns / f'{name}.md'
        p.write_text(f'---\ntype: {typ}\nstatus: "{status}"\n---\n# {name}\n')
        originals[p] = p.read_bytes()
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 2)
    bit.gen_index(ns, 'fixed')
    text = (ns / 'INDEX.md').read_text() + (ns / 'INDEX-p02.md').read_text() + (ns / 'INDEX-p03.md').read_text()
    assert '5 page(s) here' in text and 'retired' not in text and '|alias]]' not in text
    assert all(f'|{name}]]' in text for name in ['one', 'two', 'three', 'fourth', 'other'])
    assert not (ns / 'INDEX-p04.md').exists()
    # The count is the listed population: live pages only, never INDEX/INDEX-pNN (okengine#994 [23]).
    assert bit._count_md(ns) == 5
    bit.gen_index(ns, 'fixed')
    assert (ns / 'INDEX.md').read_text() + (ns / 'INDEX-p02.md').read_text() + (ns / 'INDEX-p03.md').read_text() == text
    assert all(p.read_bytes() == data for p, data in originals.items())


import pytest


@pytest.mark.parametrize('remaining', [0, 1, 2])
def test_regeneration_removes_only_obsolete_owned_pagination(tmp_path, monkeypatch, remaining):
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    for i in range(3):
        (ns / f'actor-{i}.md').write_text('---\ntype: actor\nstatus: active\n---\n# Actor\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 1)
    bit.gen_index(ns, 'before')
    assert (ns / 'INDEX-p03.md').exists()
    for i in range(remaining, 3):
        p = ns / f'actor-{i}.md'
        # okengine#1017: the retirement lands in the same timestamp tick as the first index run
        # (mtime pinned), as it does on a fast disk; the verdict cache must still see it.
        before = p.stat()
        p.write_text(p.read_text().replace('status: active', 'status: tombstoned'))
        os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
    originals = {p: p.read_bytes() for p in ns.glob('actor-*.md')}
    bit.gen_index(ns, 'after')
    assert not (ns / 'INDEX-p03.md').exists()
    assert (ns / 'INDEX-p02.md').exists() is (remaining == 2)
    text = (ns / 'INDEX.md').read_text()
    assert f'{remaining} page(s) here' in text
    assert 'actor-2' not in text
    assert bit._count_md(ns) == remaining
    bit.gen_index(ns, 'after')
    assert (ns / 'INDEX.md').read_text() == text
    assert all(p.read_bytes() == data for p, data in originals.items())


@pytest.mark.parametrize('remove', ['type', 'title', 'heading', 'nav', 'section', 'table'])
def test_non_owned_pagination_collision_preserved_and_refused(tmp_path, monkeypatch, remove):
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    for i in range(2):
        (ns / f'actor-{i}.md').write_text('---\ntype: actor\n---\n')
    (ns / 'retired.md').write_text('---\ntype: actor\nstatus: tombstoned\n---\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 1)
    bit.gen_index(ns, 'fixed')
    p = ns / 'INDEX-p02.md';text = p.read_text()
    changes = {'type': ('type: dashboard', 'type: note'),
               'title': ('Index: entities/a (p2)', 'Unrelated'),
               'heading': ('# Index: entities/a — page 2/', '# Historical 2/'),
               'nav': ('\nPages: ', '\nNavigation: '),
               'section': ('\n## Pages (2)\n', '\n## Notes\n'),
               'table': ('| Page | Type | Created | Updated | Title |', '| Historical |')}
    old, new = changes[remove];assert old in text;p.write_text(text.replace(old, new))
    before = {q: q.read_bytes() for q in ns.glob('*.md')}
    with pytest.raises(FileExistsError, match='non-generated pagination'):
        bit.gen_index(ns, 'next')
    assert all(q.read_bytes() == data for q, data in before.items())


def test_non_owned_unused_numbered_page_keeps_original_bytes(tmp_path, monkeypatch):
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    p = ns / 'INDEX-p99.md';p.write_text('---\ntype: note\n---\n# Historical notes\n')
    original = p.read_bytes()
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    bit.gen_index(ns, 'fixed')
    assert p.read_bytes() == original
    # Kept on disk, but INDEX-pNN is structural by the shared predicate, never a row (#994 [23]).
    assert 'INDEX-p99' not in (ns / 'INDEX.md').read_text()


def test_quoted_generated_example_is_not_owned_pagination(tmp_path, monkeypatch):
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    for i in range(2):
        (ns / f'actor-{i}.md').write_text('---\ntype: actor\n---\n')
    (ns / 'retired.md').write_text('---\ntype: actor\nstatus: tombstoned\n---\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 1)
    bit.gen_index(ns, 'fixed')
    p = ns / 'INDEX-p02.md';text = p.read_text();envelope = bit._FM_RE.match(text)
    p.write_text(text[:envelope.end()] + '\n# Historical example\n\n```markdown\n' + text[envelope.end():] + '\n```\n')
    before = {q: q.read_bytes() for q in ns.glob('*.md')}
    with pytest.raises(FileExistsError, match='non-generated pagination'):
        bit.gen_index(ns, 'next')
    assert all(q.read_bytes() == data for q, data in before.items())


@pytest.mark.parametrize('variant', ['missing-envelope', 'short-body', 'small-total', 'wrong-nav-count', 'wrong-nav-link'])
def test_pagination_ownership_requires_complete_current_generated_structure(tmp_path, monkeypatch, variant):
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    for i in range(2):
        (ns / f'actor-{i}.md').write_text('---\ntype: actor\n---\n')
    (ns / 'retired.md').write_text('---\ntype: actor\nstatus: tombstoned\n---\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 1)
    bit.gen_index(ns, 'fixed')
    p = ns / 'INDEX-p02.md';fm = bit._fm(p);text = p.read_text()
    # Metadata already read is not enough authority when current body differs.
    if variant == 'missing-envelope':
        text = '# Historical note\n'
    elif variant == 'short-body':
        text = text[:bit._FM_RE.match(text).end()] + '\n# Short\n'
    elif variant == 'small-total':
        text = text.replace('page 2/2', 'page 2/1')
    elif variant == 'wrong-nav-count':
        text = text.replace('Pages: **1** ', 'Pages: ')
    else:
        text = text.replace('[[entities/a/INDEX-p02|2]]', '[[other/INDEX-p02|2]]')
    p.write_text(text);before = p.read_bytes()
    assert not bit._owned_pagination(p, fm, 'entities/a')
    assert p.read_bytes() == before


def test_unrelated_nonactor_directory_keeps_old_owned_pagination(tmp_path, monkeypatch):
    ns = tmp_path / 'wiki/sources/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {source: {}}\n')
    for i in range(3):
        (ns / f'source-{i}.md').write_text('---\ntype: source\n---\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 1)
    bit.gen_index(ns, 'fixed')
    old_page = ns / 'INDEX-p03.md'
    assert bit._owned_pagination(old_page, bit._fm(old_page), 'sources/a')
    for i in range(3):
        (ns / f'source-{i}.md').write_text('---\ntype: source\nstatus: tombstoned\n---\n')
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 500)
    bit.gen_index(ns, 'next')
    assert old_page.exists(), 'Unrelated source pagination must not be cleaned by actor retirement'
    assert all(f'|source-{i}]]' in (ns / 'INDEX.md').read_text() for i in range(3))


def test_count_md_parses_a_page_at_most_once_across_every_ancestor_level(tmp_path, monkeypatch):
    """_count_md runs once per ancestor directory. Re-parsing every page at each level made the
    lane ~5x slower on a 100k-page vault, past a 600 s script timeout (v0.15.3 release audit).
    Only a page whose frontmatter spells `tombstoned` may be parsed at all, and only once."""
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    leaf = tmp_path / 'wiki/entities/a/b'
    leaf.mkdir(parents=True)
    for i in range(4):
        (leaf / f'live-{i}.md').write_text('---\ntype: actor\nstatus: active\n---\n# Live\n')
    (leaf / 'gone.md').write_text('---\ntype: actor\nstatus: TombStoned\n---\n# Gone\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    calls = []
    real_fm = bit._fm
    monkeypatch.setattr(bit, '_fm', lambda p: calls.append(p) or real_fm(p))
    for d in (tmp_path / 'wiki/entities', tmp_path / 'wiki/entities/a', leaf):
        assert bit._count_md(d) == 4, d
    assert calls == [leaf / 'gone.md']


def test_count_md_rejudges_a_page_edited_during_the_run(tmp_path, monkeypatch):
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    ns = tmp_path / 'wiki/entities/a'
    ns.mkdir(parents=True)
    page = ns / 'x.md'
    page.write_text('---\ntype: actor\nstatus: active\n---\n# X\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    assert bit._count_md(ns) == 1
    page.write_text('---\ntype: actor\nstatus: tombstoned\n---\n# X\n')
    st = page.stat()
    os.utime(page, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert bit._count_md(ns) == 0


def test_count_md_tolerates_a_vanished_page_and_a_directory_named_like_a_page(tmp_path, monkeypatch):
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    ns = tmp_path / 'wiki/entities/a'
    (ns / 'odd.md').mkdir(parents=True)          # rglob('*.md') yields it; read_text raises
    (ns / 'real.md').write_text('---\ntype: actor\nstatus: active\n---\n# Real\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    assert bit._count_md(ns) == 2                # neither is a retired actor
    assert bit._retired(ns / 'vanished.md') is False


def _long_actor(status: str) -> str:
    """Frontmatter that closes well past 4000 chars, with `status` below the long list."""
    rows = [f'  - https://example.org/report/{i:05d}-a-fairly-long-report-slug' for i in range(150)]
    return '\n'.join(['---', 'type: actor', 'sources:', *rows, f'status: {status}', '---',
                      '# Actor', ''])


def test_long_frontmatter_retired_actor_is_dropped_from_index_and_counts(tmp_path, monkeypatch):
    """okengine#991 audit 2 [2]/[4]/[8]: a 4000-char read window parsed this page as {} and kept
    the retired actor listed (blank type) and counted, while rebuild_index hid it."""
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    ns = tmp_path / 'wiki/entities/o'
    ns.mkdir(parents=True)
    (ns / 'old.md').write_text(_long_actor('tombstoned'))
    (ns / 'big.md').write_text(_long_actor('active'))
    (ns / 'new.md').write_text('---\ntype: actor\nstatus: active\n---\n# New\n')
    assert (ns / 'old.md').read_text().index('\n---\n# Actor') > 4000
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    for d in (tmp_path / 'wiki/entities', ns):
        assert bit._count_md(d) == 2, d
    bit.gen_index(ns, 'fixed')
    text = (ns / 'INDEX.md').read_text()
    assert '2 page(s) here' in text and '|old]]' not in text
    assert '| [[entities/o/big|big]] | actor |' in text, 'a long live actor lost its type'


def test_index_tree_hot_set_and_rebuild_index_agree_on_a_long_retired_actor(tmp_path, monkeypatch):
    """The three active-navigation generators must reach one verdict for the same page."""
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    page = tmp_path / 'wiki/entities/o/old.md'
    page.parent.mkdir(parents=True)
    page.write_text(_long_actor('tombstoned'))
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    cron = pathlib.Path(__file__).resolve().parents[2] / 'scripts/cron'
    sys.path.insert(0, str(cron))
    ri = importlib.reload(importlib.import_module('rebuild_index'))
    bhs = importlib.reload(importlib.import_module('build_hot_set'))
    verdicts = {
        'rebuild_index': ri.is_retired_actor(ri.parse_page(page)[0], tmp_path, page),
        'build_index_tree': bit._retired(page),
        'build_hot_set': bhs.is_tombstoned(bhs._fm(page)),
    }
    assert verdicts == dict.fromkeys(verdicts, True)


# --- okengine#994 [23]: structural pages are neither rows nor counted content ------------------

def _paginated_namespace(tmp_path, monkeypatch, pages=5):
    (tmp_path / 'schema.yaml').write_text('types: {cve: {}}\n')
    ns = tmp_path / 'wiki/cves'
    ns.mkdir(parents=True)
    (ns / '_about.md').write_text('---\ntype: about\n---\n# CVEs\nVulnerability records.\n')
    (ns / 'log.md').write_text('# log\n')
    for i in range(pages):
        (ns / f'cve-{i}.md').write_text(f'---\ntype: cve\ntitle: CVE {i}\n---\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(bit, 'MAX_ENTRIES', 2)
    return bit, ns


def test_a_regenerated_paginated_index_never_lists_its_own_pagination_pages(tmp_path, monkeypatch):
    """The live okcti cves/INDEX.md listed INDEX-p02..p04 as rows: the second run read the first
    run's pagination pages back in as content."""
    bit, ns = _paginated_namespace(tmp_path, monkeypatch)
    bit.gen_index(ns, 'first')
    assert (ns / 'INDEX-p03.md').is_file()
    bit.gen_index(ns, 'second')
    pages = [ns / 'INDEX.md', *sorted(ns.glob('INDEX-p*.md'))]
    text = ''.join(p.read_text() for p in pages)
    assert '5 page(s) here' in text, text
    assert '|INDEX-p0' not in text, 'a pagination page was listed as a row'
    assert '|_about]]' not in text and '|log]]' not in text
    assert [p.name for p in pages] == ['INDEX.md', 'INDEX-p02.md', 'INDEX-p03.md']


def test_counts_exclude_index_pagination_about_and_log_pages(tmp_path, monkeypatch):
    bit, ns = _paginated_namespace(tmp_path, monkeypatch)
    bit.gen_index(ns, 'first')
    assert bit._count_md(ns) == 5
    assert bit._count_md(tmp_path / 'wiki') == 5


def test_bundle_health_and_namespace_counts_are_content_only(tmp_path, monkeypatch):
    bit, ns = _paginated_namespace(tmp_path, monkeypatch)
    sub = ns / '2026'
    sub.mkdir()
    (sub / 'cve-x.md').write_text('---\ntype: cve\n---\n')
    monkeypatch.setattr(bit.tz_lib, 'deployment_now', lambda: datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert bit.main() == 0
    assert bit.main() == 0                          # second run: the first run's INDEX files exist
    wiki = tmp_path / 'wiki'
    assert 'entity_count: 6\n' in (wiki / 'BUNDLE.md').read_text()
    assert 'Total curated pages: **6**' in (wiki / 'HEALTH.md').read_text()
    assert '| `cves/` | 6 |' in (wiki / 'INDEX.md').read_text()
    assert '| `2026/` | 1 |' in (ns / 'INDEX.md').read_text()


@pytest.mark.parametrize('name, listable', [
    ('cve-1.md', True), ('INDEX-glossary.md', True), ('README-notes.md', True),
    ('INDEX.md', False), ('index.md', False), ('INDEX-p02.md', False), ('INDEX-p10.md', False),
    ('_about.md', False), ('log.md', False), ('BUNDLE.md', False), ('HEALTH.md', False),
    ('AGENTS.md', False), ('README.md', False), ('HOT.md', False), ('.draft.md', False),
    ('page.bak.md', False)])
def test_listable_is_the_shared_generated_page_predicate(tmp_path, monkeypatch, name, listable):
    (tmp_path / 'wiki').mkdir()
    (tmp_path / 'schema.yaml').write_text('types: {}\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    assert bit._listable(name) is listable
    assert bit._listable(name) is not bit.generated_page.is_exempt(pathlib.Path(name))


def test_only_generator_numbered_names_are_pagination_candidates(tmp_path, monkeypatch):
    """INDEX-p01 (page 1 is INDEX.md itself) is never this generator's file, and INDEX-pages is a
    content page that merely shares the prefix: neither is removed as obsolete pagination."""
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    ns = tmp_path / 'wiki/entities/a'
    ns.mkdir(parents=True)
    (ns / 'live.md').write_text('---\ntype: actor\n---\n')
    (ns / 'gone.md').write_text('---\ntype: actor\nstatus: tombstoned\n---\n')
    (ns / 'INDEX-p01.md').write_text('---\ntype: dashboard\ntitle: "Index: entities/a (p1)"\n---\n')
    (ns / 'INDEX-pages.md').write_text('---\ntype: note\ntitle: Pages\n---\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    bit.gen_index(ns, 'fixed')
    text = (ns / 'INDEX.md').read_text()
    assert (ns / 'INDEX-p01.md').is_file() and (ns / 'INDEX-pages.md').is_file()
    assert '|INDEX-pages]]' in text and '|INDEX-p01]]' not in text
    assert '2 page(s) here' in text and bit._count_md(ns) == 2


def test_a_pagination_page_that_vanishes_mid_scan_is_not_owned(tmp_path, monkeypatch):
    (tmp_path / 'wiki').mkdir()
    (tmp_path / 'schema.yaml').write_text('types: {}\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    gone = tmp_path / 'wiki/entities/a/INDEX-p02.md'
    fm = {'type': 'dashboard', 'title': 'Index: entities/a (p2)'}
    assert bit._owned_pagination(gone, fm, 'entities/a') is False


def test_retirement_verdict_sees_a_same_size_rewrite_within_one_tick(tmp_path, monkeypatch):
    """okengine#1017: the per-run verdict cache was keyed on (path, mtime_ns), so a page retired
    within the same tick as its first count kept counting as live."""
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    page = ns / 'actor-0.md'
    page.write_text('---\ntype: actor\nstatus: superseded\n---\n# Actor\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    assert bit._count_md(ns) == 1
    before = page.stat()
    page.write_text('---\ntype: actor\nstatus: tombstoned\n---\n# Actor\n')
    os.utime(page, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert page.stat().st_size == before.st_size
    assert bit._count_md(ns) == 0


def test_retirement_verdict_sees_a_different_size_rewrite_within_one_tick(tmp_path, monkeypatch):
    ns = tmp_path / 'wiki/entities/a';ns.mkdir(parents=True)
    (tmp_path / 'schema.yaml').write_text('types: {actor: {}}\n')
    page = ns / 'actor-0.md'
    page.write_text('---\ntype: actor\nstatus: active\n---\n# Actor\n')
    bit = _load_retirement_tree(tmp_path, monkeypatch)
    assert bit._count_md(ns) == 1
    before = page.stat()
    page.write_text('---\ntype: actor\nstatus: tombstoned\n---\n# Actor\n')
    os.utime(page, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert bit._count_md(ns) == 0
