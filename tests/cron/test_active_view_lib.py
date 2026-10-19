"""Retirement is explicit actor lifecycle, not a name or a universal status filter."""
import importlib
import sys
from pathlib import Path

import pytest

CRON = Path(__file__).resolve().parents[2] / 'scripts/cron'


@pytest.mark.parametrize('fm,expected', [
    (None, False), ([], False), ({}, False),
    ({'type': 'actor'}, False), ({'type': 'actor', 'status': 'unknown'}, False),
    ({'type': 'actor', 'status': 'active'}, False),
    ({'type': 'actor', 'status': ' Tombstoned '}, True),
    ({'type': 'Actor', 'status': 'tombstoned'}, True),
    ({'type': 'threat_actor', 'status': 'tombstoned'}, True),
    ({'type': 'source', 'status': 'tombstoned'}, False),
    ({'type': 'malware', 'status': 'tombstoned'}, False),
    ({'status': 'tombstoned'}, False),
])
def test_only_explicit_retired_actor_is_excluded(tmp_path, fm, expected):
    sys.path.insert(0, str(CRON))
    helper = importlib.import_module('active_view_lib')
    (tmp_path / 'schema.yaml').write_text('type_aliases: {threat_actor: actor}\n')
    page = tmp_path / 'wiki/entities/a/example.md'
    page.parent.mkdir(parents=True)
    assert helper.is_retired_actor(fm, tmp_path, page) is expected


def test_page_governing_alias_overrides_vault_alias(tmp_path):
    sys.path.insert(0, str(CRON))
    helper = importlib.import_module('active_view_lib')
    (tmp_path / 'schema.yaml').write_text('type_aliases: {participant: actor}\n')
    namespace = tmp_path / 'wiki/local/entities'
    namespace.mkdir(parents=True)
    (namespace.parent / 'schema.yaml').write_text('type_aliases: {participant: company}\n')
    assert not helper.is_retired_actor({'type': 'participant', 'status': 'tombstoned'},
                                      tmp_path, namespace / 'example.md')


def _long_frontmatter(status: str, sources: int) -> str:
    """An actor page whose `status` sits below a long `sources:` list, the shape merged actors
    take (okengine#991 audit 2 [2]: 58 kB of frontmatter on a live tombstoned actor)."""
    lines = ['---', 'type: actor', 'title: Long Actor', 'sources:']
    lines += [f'  - https://example.org/report/{i:05d}-a-fairly-long-report-slug' for i in range(sources)]
    lines += [f'status: {status}', '---', '# Long Actor', '']
    return '\n'.join(lines)


@pytest.mark.parametrize('sources', [80, 400])  # closes past 4000 chars, and past 16384
def test_read_frontmatter_has_no_window_so_a_late_status_is_seen(tmp_path, sources):
    sys.path.insert(0, str(CRON))
    helper = importlib.import_module('active_view_lib')
    page = tmp_path / 'long.md'
    page.write_text(_long_frontmatter('tombstoned', sources))
    assert page.read_text().index('\n---\n# Long') > (4000 if sources == 80 else 16384)
    fm = helper.read_frontmatter(page)
    assert fm.get('status') == 'tombstoned', 'a status below a long sources list was cut off'
    assert len(fm['sources']) == sources
    assert 'status: tombstoned' in helper.frontmatter_block(page)


@pytest.mark.parametrize('text', [
    'no envelope here\n', '---\n[unclosed: yaml\n---\n', '---\n- a\n- list\n---\n',
    '---\nunterminated: true\n'])
def test_read_frontmatter_is_empty_for_absent_malformed_or_nondict_envelopes(tmp_path, text):
    sys.path.insert(0, str(CRON))
    helper = importlib.import_module('active_view_lib')
    page = tmp_path / 'page.md'
    page.write_text(text)
    assert helper.read_frontmatter(page) == {}


def test_read_frontmatter_of_an_unreadable_path_is_empty(tmp_path):
    sys.path.insert(0, str(CRON))
    helper = importlib.import_module('active_view_lib')
    assert helper.frontmatter_block(tmp_path / 'missing.md') == ''
    assert helper.read_frontmatter(tmp_path / 'missing.md') == {}
    (tmp_path / 'dir.md').mkdir()
    assert helper.read_frontmatter(tmp_path / 'dir.md') == {}


@pytest.mark.parametrize('typ,expected', [('intrusion-set', True), ('apt', True),
                                          ('Threat-Actor', True), ('malware', False)])
def test_a_declared_actor_type_is_retired_like_an_actor(tmp_path, typ, expected):
    """okengine#1020: the generators exclude tombstones of every declared actor type. Red before:
    only `actor` (and its aliases) were excluded, so retired intrusion sets stayed listed."""
    sys.path.insert(0, str(CRON))
    helper = importlib.import_module('active_view_lib')
    (tmp_path / 'schema.yaml').write_text(
        'types:\n  intrusion-set: {role: actor}\n  threat-actor: {role: actor}\n'
        '  malware: {}\ntype_aliases: {apt: intrusion-set}\n')
    page = tmp_path / 'wiki/entities/a/example.md'
    page.parent.mkdir(parents=True)
    assert helper.is_retired_actor({'type': typ, 'status': 'tombstoned'}, tmp_path, page) is expected
