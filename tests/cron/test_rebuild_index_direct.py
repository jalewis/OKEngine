from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "rebuild_index.py"


def _load(vault: Path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    spec = importlib.util.spec_from_file_location(f"rebuild_index_{id(vault)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(path: Path, fm: str = "", body: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n{body}")


def test_namespace_fallback_parse_titles_and_missing_dirs(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(m.schema_lib, "knowledge_namespaces", lambda _s: {"declared"})
    assert m.knowledge_namespaces() == ["declared"]
    monkeypatch.setattr(m.schema_lib, "knowledge_namespaces", lambda _s: [])
    monkeypatch.setattr(m.schema_lib, "excluded_dirs", lambda _s: {"excluded"})
    wiki = tmp_path / "wiki"
    assert m.knowledge_namespaces() == []
    (wiki / ".hidden").mkdir(parents=True)
    (wiki / "_private").mkdir()
    (wiki / "excluded").mkdir()
    (wiki / "empty").mkdir()
    _page(wiki / "custom" / "a.md", "type: note\n", "# [[entities/a/acme|Acme]] *News*\n")
    assert m.knowledge_namespaces() == ["custom"]
    assert m.list_dir("missing") == []
    assert m.group_by_type("missing") == {}
    assert m.short_title("slug", None) == "slug"
    assert m.short_title("slug", "***") == "slug"
    assert m.short_title("slug", "[[entities/a/acme|ignored]] `News`") == "acme News"

    page = wiki / "custom" / "a.md"
    fm, title = m.parse_page(page)
    assert fm["type"] == "note" and title.startswith("[[")
    page.write_text("plain\n# Heading\n")
    assert m.parse_page(page) == (None, "Heading")
    page.write_text("---\n[\n---\n# H\n")
    assert m.parse_page(page) == (None, "H")
    page.write_text("---\n- one\n---\n")
    assert m.parse_page(page) == (None, None)
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m.parse_page(page) == (None, None)


def test_list_and_group_filter_operational_untyped_and_shards(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    root = tmp_path / "wiki" / "entities"
    _page(root / "_skip.md", "type: actor\n", "# Skip\n")
    _page(root / "a" / "actor.md", "type: actor\n", "# Actor\n")
    _page(root / "b" / "operational.md", "type: dashboard\n", "# Dash\n")
    _page(root / "c" / "untyped.md", "", "")
    assert m.is_operational({"type": "dashboard"}) and not m.is_operational(None)
    lines = m.list_dir("entities", limit=1, sort_recent=True)
    assert len(lines) <= 1
    assert all("operational" not in line for line in m.list_dir("entities"))
    groups = m.group_by_type("entities")
    assert set(groups) == {"actor", "untyped"}
    assert "[[entities/a/actor]] — Actor" in groups["actor"][0]


def test_render_every_namespace_mode_drift_empty_and_lint(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch)
    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    (wiki / "entities").mkdir()
    (wiki / "questions").mkdir()
    _page(wiki / "sources" / "2026-01-01-a.md", "type: source\n", "# A\n")
    _page(wiki / "entities" / "a.md", "type: actor\n", "# A\n")
    _page(wiki / "entities" / "z.md", "type: drift\n", "# Z\n")
    _page(wiki / "lint-2026.md", "type: report\n", "# Lint\n")
    monkeypatch.setattr(m, "knowledge_namespaces",
                        lambda: ["sources", "entities", "questions"])
    monkeypatch.setattr(m.schema_lib, "canonical_types", lambda _s: {"actor", "missing"})
    body = m.render_index()
    assert "Sources (most recent 30)" in body
    assert "### actor (1)" in body and "### drift (1) — DRIFT" in body
    assert "## Questions (0)" in body and "_(none)_" in body
    assert "Latest weekly audit" in body

    monkeypatch.setattr(m, "knowledge_namespaces", lambda: [])
    body = m.render_index()
    assert "no knowledge namespaces found" in body


def test_main_missing_dry_write_backup_and_entrypoint(tmp_path, monkeypatch, capsys):
    missing = _load(tmp_path / "missing", monkeypatch)
    monkeypatch.setattr(sys, "argv", ["rebuild_index"])
    assert missing.main() == 1
    assert "vault not found" in capsys.readouterr().err

    wiki = tmp_path / "vault" / "wiki"
    wiki.mkdir(parents=True)
    m = _load(tmp_path / "vault", monkeypatch)
    monkeypatch.setattr(m, "render_index", lambda: "# Index\n")
    monkeypatch.setattr(sys, "argv", ["rebuild_index", "--dry-run"])
    assert m.main() == 0
    assert capsys.readouterr().out == "# Index\n"

    monkeypatch.setattr(sys, "argv", ["rebuild_index"])
    assert m.main() == 0
    assert not m.INDEX.with_name("index.md.bak").exists()
    capsys.readouterr()

    m.INDEX.write_text("old")
    monkeypatch.setattr(sys, "argv", ["rebuild_index"])
    assert m.main() == 0
    assert m.INDEX.read_text() == "# Index\n"
    assert m.INDEX.with_name("index.md.bak").read_text() == "old"
    assert '"wakeAgent": false' in capsys.readouterr().out

    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0


def test_retired_actor_navigation_excluded_before_limit_and_group_counts(tmp_path, monkeypatch):
    (tmp_path / 'schema.yaml').write_text('type_aliases: {threat_actor: actor}\ntypes: {actor: {}, malware: {}}\n')
    root = tmp_path / 'wiki/entities'
    originals = {}
    for name, typ, status in [('a-retired', 'Actor', 'tombstoned'),
                              ('b-retired-alias', 'threat_actor', ' Tombstoned '),
                              ('c-genuine', 'actor', 'active'), ('d-unknown', 'actor', 'unknown'),
                              ('e-missing', 'actor', ''), ('f-fourth', 'actor', 'active'), ('g-other', 'malware', 'tombstoned')]:
        p = root / f'{name}.md'
        _page(p, f'type: {typ}\nstatus: "{status}"\n', f'# {name}\n')
        originals[p] = p.read_bytes()
    source = tmp_path / 'wiki/sources/2026-10-01-victim.md'
    _page(source, 'type: source\n', '# Victim report\n');originals[source] = source.read_bytes()
    m = _load(tmp_path, monkeypatch)
    assert m.list_dir('entities', limit=1) == ['- [[entities/c-genuine]]']
    assert len(m.group_by_type('entities')['actor']) == 4
    text = m.render_index()
    assert '### actor (4)' in text and '### malware (1)' in text and '## Entities (5)' in text
    assert 'retired' not in text.split('> Rebuilt')[1]
    assert '2026-10-01-victim' in text
    assert [line for line in m.render_index().splitlines() if not line.startswith('> Generated:')] == [line for line in text.splitlines() if not line.startswith('> Generated:')]
    assert all(p.read_bytes() == data for p, data in originals.items())


def test_source_limit_keeps_existing_operational_slot_semantics(tmp_path, monkeypatch):
    root = tmp_path / 'wiki/sources'
    _page(root / 'a-source.md', 'type: source\n', '# A\n')
    _page(root / 'b-dashboard.md', 'type: dashboard\n', '# B\n')
    _page(root / 'c-source.md', 'type: source\n', '# C\n')
    m = _load(tmp_path, monkeypatch)
    lines = m.list_dir('sources', limit=2)
    assert len(lines) == 1 and 'a-source' in lines[0]
    assert 'c-source' not in ''.join(lines)


def test_capped_source_navigation_does_not_read_unused_bodies(tmp_path, monkeypatch):
    from unittest.mock import create_autospec
    root = tmp_path / 'wiki/sources'
    for name, typ in [('a-source', 'source'), ('b-dashboard', 'dashboard'), ('c-source', 'source')]:
        _page(root / f'{name}.md', f'type: {typ}\n', f'# {name}\n')
    m = _load(tmp_path, monkeypatch)
    original = Path.read_text
    reader = create_autospec(original, side_effect=lambda path, *args, **kw: original(path, *args, **kw))
    monkeypatch.setattr(Path, 'read_text', reader)
    assert m.list_dir('sources', limit=2) == ['- [[sources/a-source]]']
    assert root / 'c-source.md' not in {call.args[0] for call in reader.call_args_list}
    # Preserve the existing Python-slice boundary contract too.
    assert m.list_dir('sources', limit=-1) == ['- [[sources/a-source]]']
    assert len(m.list_dir('sources', limit=0)) == 2


def test_an_impossible_date_is_an_unparseable_page_not_a_crash(tmp_path, monkeypatch):
    """okengine#993: YAML's timestamp constructor raises ValueError (not YAMLError) for
    `2026-02-30`, and parse_page caught only YAMLError, so one such page crashed the lane."""
    page = tmp_path / 'wiki/entities/bad-date.md'
    _page(page, 'type: actor\nlast_updated: 2026-02-30\n', '# Bad date\n')
    m = _load(tmp_path, monkeypatch)
    assert m.parse_page(page) == (None, 'Bad date')
    assert m.list_dir('entities') == ['- [[entities/bad-date]] — Bad date']


def test_index_and_active_view_reader_agree_on_a_crlf_tombstoned_actor(tmp_path, monkeypatch):
    """One frontmatter pattern for both readers: a CRLF page is the same page to rebuild_index and
    to the generators that use frontmatter_lib (build_hot_set, build_index_tree)."""
    sys.path.insert(0, str(REPO / 'scripts' / 'cron'))
    import frontmatter_lib
    page = tmp_path / 'wiki/entities/crlf.md'
    page.parent.mkdir(parents=True)
    page.write_bytes(b'---\r\ntype: actor\r\nstatus: tombstoned\r\n---\r\n# CRLF\r\n')
    m = _load(tmp_path, monkeypatch)
    assert m.FRONTMATTER_RE is frontmatter_lib.FRONTMATTER_RE
    assert m.parse_page(page)[0] == frontmatter_lib.read_frontmatter(page) == {
        'type': 'actor', 'status': 'tombstoned'}
    assert m.list_dir('entities') == []
