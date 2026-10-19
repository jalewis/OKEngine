"""source_backlink_drain: declared source→entity updates become entity `sources:` backlinks.

The frontmatter surgery (``insert_source_backlink``) is tested as a pure function over every
`sources:` shape a page can carry; the drain itself runs in-process against disposable vaults.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "source_backlink_drain.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("source_backlink_drain_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = _load_module()
LINK = '"[[sources/2026-09-01-report]]"'


@pytest.fixture
def drain(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    monkeypatch.setattr(D, "VAULT", vault)
    monkeypatch.setattr(D, "SRC_DIR", vault / "wiki" / "sources")
    monkeypatch.setattr(D, "ENT_DIR", vault / "wiki" / "entities")
    monkeypatch.setattr(D, "OUT_DIR", vault / "wiki" / "operational")
    monkeypatch.setattr(D, "BATCH", 200)
    return vault


def _write(vault: Path, rel: str, text: str) -> Path:
    path = vault / "wiki" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _entity(vault: Path, slug: str, fm: str = "type: entity") -> Path:
    return _write(vault, f"entities/{slug[0]}/{slug}.md", f"---\n{fm}\n---\n# {slug}\n")


def _source(vault: Path, stem: str, body: str) -> Path:
    return _write(vault, f"sources/2026/09/{stem}.md", f"---\ntype: source\n---\n{body}\n")


def _report(vault: Path) -> str:
    return next((vault / "wiki" / "operational").glob("source-backlink-drain-*.md")).read_text()


# --- parse_declarations -------------------------------------------------------------------------

def test_update_create_and_enrich_verbs_declare_an_entity():
    text = ("- Updates [[entities/acme-corp]] — new field\n"
            "- Creates new entity [[entities/Beta]]\n"
            "- enriched the profile of [[entities/gamma]]\n")

    assert D.parse_declarations(text) == {"acme-corp", "beta", "gamma"}


def test_a_bare_mention_without_a_verb_is_not_a_declaration():
    assert D.parse_declarations("See also [[entities/acme]] for context.") == set()


def test_a_verb_too_far_from_the_link_is_not_a_declaration():
    text = "Updated " + "x" * 31 + "[[entities/acme]]"

    assert D.parse_declarations(text) == set()


# --- insert_source_backlink ---------------------------------------------------------------------

def test_an_existing_backlink_is_left_untouched():
    text = '---\ntype: entity\n---\nbody cites [[sources/2026-09-01-report]]\n'

    assert D.insert_source_backlink(text, "2026-09-01-report") == (text, False)


def test_a_page_without_frontmatter_is_left_untouched():
    text = "# Acme\nno frontmatter\n"

    assert D.insert_source_backlink(text, "2026-09-01-report") == (text, False)


def test_a_missing_sources_key_is_appended_to_the_frontmatter():
    new, changed = D.insert_source_backlink("---\ntype: entity\n---\nbody\n", "2026-09-01-report")

    assert changed
    assert new == f"---\ntype: entity\nsources:\n  - {LINK}\n---\nbody\n"


def test_an_empty_inline_list_becomes_a_block_list():
    new, changed = D.insert_source_backlink("---\nsources: []\ntype: entity\n---\n", "2026-09-01-report")

    assert changed
    assert new == f"---\nsources:\n  - {LINK}\ntype: entity\n---\n"


def test_a_populated_inline_list_gains_the_link_before_its_bracket():
    new, _ = D.insert_source_backlink('---\nsources: ["[[sources/a]]"]\n---\n', "2026-09-01-report")

    assert new == f'---\nsources: ["[[sources/a]]", {LINK}]\n---\n'


def test_an_inline_list_with_a_trailing_comma_gets_no_double_comma():
    new, _ = D.insert_source_backlink('---\nsources: ["[[sources/a]]",]\n---\n', "2026-09-01-report")

    assert new == f'---\nsources: ["[[sources/a]]", {LINK}]\n---\n'


def test_a_block_list_gains_the_link_after_its_last_item_at_its_indent():
    text = '---\nsources:\n    - "[[sources/a]]"\n    - "[[sources/b]]"\ntags: [x]\n---\n'

    new, _ = D.insert_source_backlink(text, "2026-09-01-report")

    assert new == ('---\nsources:\n    - "[[sources/a]]"\n    - "[[sources/b]]"\n'
                   f'    - {LINK}\ntags: [x]\n---\n')


def test_a_bare_sources_key_gets_its_first_item():
    new, _ = D.insert_source_backlink("---\nsources:\ntags: [x]\n---\n", "2026-09-01-report")

    assert new == f"---\nsources:\n  - {LINK}\ntags: [x]\n---\n"


# --- main ---------------------------------------------------------------------------------------

def test_no_entities_dir_is_a_clean_no_op(drain, capsys):
    rc = D.main()

    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out
    assert not (drain / "wiki" / "operational").exists()


def test_no_sources_dir_is_a_clean_no_op(drain, capsys):
    _entity(drain, "acme")

    rc = D.main()

    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out


def test_a_declared_update_backlinks_the_entity_and_is_reported(drain, capsys):
    entity = _entity(drain, "acme")
    _source(drain, "2026-09-01-report", "## Wiki Impact\n- Updates [[entities/acme]] — pricing")

    rc = D.main()

    assert rc == 0
    assert '[[sources/2026-09-01-report]]' in entity.read_text()
    report = _report(drain)
    assert "| [[entities/acme]] | [[sources/2026-09-01-report]] |" in report
    assert "Reconnected **1**" in report
    assert oct(os.stat(next((drain / "wiki" / "operational").iterdir())).st_mode & 0o777) == "0o644"
    assert capsys.readouterr().out.rstrip().endswith('{"wakeAgent": false}')


def test_a_second_run_is_idempotent(drain):
    entity = _entity(drain, "acme")
    _source(drain, "2026-09-01-report", "Updates [[entities/acme]]")
    D.main()
    once = entity.read_text()

    D.main()

    assert entity.read_text() == once
    assert "No unapplied source→entity declarations" in _report(drain)


def test_index_pages_undeclared_sources_and_unknown_entities_are_skipped(drain):
    entity = _entity(drain, "acme")
    _write(drain, "sources/INDEX.md", "Updates [[entities/acme]]\n")
    _write(drain, "sources/2026/INDEX-2026.md", "Updates [[entities/acme]]\n")
    _write(drain, "sources/_draft.md", "Updates [[entities/acme]]\n")
    _source(drain, "2026-09-02-mention", "Mentions [[entities/acme]] in passing")
    _source(drain, "2026-09-03-new", "New entity needed: [[entities/unknown-co]]")
    before = entity.read_text()

    rc = D.main()

    assert rc == 0
    assert entity.read_text() == before
    assert "Reconnected **0**" in _report(drain)


def test_unreadable_sources_and_entities_are_skipped(drain):
    _entity(drain, "acme")
    (drain / "wiki" / "entities" / "b" / "broken.md").mkdir(parents=True)   # listed, unreadable
    (drain / "wiki" / "sources" / "2026-09-09-dir.md").mkdir(parents=True)
    _source(drain, "2026-09-01-report", "Updates [[entities/broken]] and updates [[entities/acme]]")

    rc = D.main()

    assert rc == 0
    assert "| [[entities/acme]] | [[sources/2026-09-01-report]] |" in _report(drain)
    assert "entities/broken" not in _report(drain)


def test_the_batch_cap_stops_within_a_source(drain, monkeypatch):
    monkeypatch.setattr(D, "BATCH", 1)
    _entity(drain, "alpha")
    beta = _entity(drain, "beta")
    _source(drain, "2026-09-01-report", "Updates [[entities/alpha]]; updates [[entities/beta]]")

    D.main()

    assert "sources/" not in beta.read_text()
    assert "Reconnected **1**" in _report(drain)


def test_the_batch_cap_stops_before_older_sources(drain, monkeypatch):
    monkeypatch.setattr(D, "BATCH", 1)
    alpha = _entity(drain, "alpha")
    beta = _entity(drain, "beta")
    _source(drain, "2026-09-02-newer", "Updates [[entities/alpha]]")
    _source(drain, "2026-09-01-older", "Updates [[entities/beta]]")

    D.main()

    assert "[[sources/2026-09-02-newer]]" in alpha.read_text()
    assert "sources/" not in beta.read_text()


def test_a_report_permission_failure_does_not_fail_the_run(drain, monkeypatch):
    _entity(drain, "acme")
    _source(drain, "2026-09-01-report", "Updates [[entities/acme]]")
    chmod = mock.create_autospec(os.chmod, side_effect=PermissionError("read-only mount"))
    monkeypatch.setattr(D.os, "chmod", chmod)

    rc = D.main()

    assert rc == 0
    chmod.assert_called_once()
    assert "Reconnected **1**" in _report(drain)
