from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "select_schema_classify.py"


def _load(vault: Path, monkeypatch, schema="types: {}\n"):
    (vault / "schema.yaml").write_text(schema)
    monkeypatch.setenv("WIKI_PATH", str(vault))
    monkeypatch.setenv("HERMES_HOME", str(vault / ".hermes"))
    monkeypatch.setenv("SCHEMA_CLASSIFY_MIN_AGE", "7")
    spec = importlib.util.spec_from_file_location(f"schema_classify_{id(vault)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_loaders_helpers_and_read_edges(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, """
types: {actor: {}, tool: {}}
classify_hints:
  actor: [APT, group]
  bad: scalar
classify_catchall: [organization]
""")
    assert m.CANONICAL == ["actor", "tool"]
    assert m._CATCHALL == {"organization"}
    assert m._HINTS == [({"apt", "group"}, "actor")]
    assert m._parse_date(date(2026, 1, 1)) == date(2026, 1, 1)
    assert m._parse_date(datetime(2026, 1, 2)) == date(2026, 1, 2)
    assert m._parse_date("created 2026-01-03") == date(2026, 1, 3)
    assert m._parse_date("2026-99-99") is None
    assert m._parse_date("") is None
    assert m._parse_date(3) is None
    assert m._tags({"tags": [" APT ", 3]}) == {"apt", "3"}
    assert m._tags({"tags": "[apt, tool]"}) == {"apt", "tool"}
    assert m._tags({}) == set()
    assert m.hint_for({"apt"}) == "actor" and m.hint_for({"none"}) == ""
    assert m._excerpt("# H\n[[entities/a|Actor]] **does** work", 20) == "H Actor does work"

    page = tmp_path / "page.md"
    page.write_text("plain")
    assert m.read_fm_body(page) == ({}, "plain")
    page.write_text("---\n[\n---\nbody")
    assert m.read_fm_body(page) == ({}, "body")
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    assert m.read_fm_body(page) == ({}, "")


def test_select_filters_archives_types_indicators_and_young_pages(tmp_path, monkeypatch):
    m = _load(tmp_path, monkeypatch, """
types: {actor: {}}
classify_catchall: [organization]
""")
    assert m.select(date(2026, 1, 10)) == []
    root = tmp_path / "wiki" / "entities"
    root.mkdir(parents=True)
    pages = {
        "_skip.md": "---\ntype: entity\n---\n",
        "typed.md": "---\ntype: actor\n---\n",
        "indicator.md": "---\ntype: entity\ncategory: x\n---\n",
        "young.md": "---\ntype: entity\ncreated: 2026-01-09\n---\n",
        "bare.md": "---\ntype: entity\ncreated: 2020-01-01\ntags: [none]\n---\n# Bare\n",
        "org.md": "---\ntype: organization\ntags: [x]\n---\nOrganization",
    }
    for name, value in pages.items():
        (root / name).write_text(value)
    archive = root / "_archive"
    archive.mkdir()
    (archive / "old.md").write_text("---\ntype: entity\n---\n")
    selected = m.select(date(2026, 1, 10))
    assert [x["slug"] for x in selected] == ["bare", "org"]
    assert selected[0]["rel"] == "wiki/entities/bare.md"
    assert m.main() == 0


def test_main_no_work_batch_empty_canonical_and_entrypoint(tmp_path, monkeypatch, capsys):
    m = _load(tmp_path, monkeypatch)
    assert m.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out

    page = tmp_path / "wiki" / "entities" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: entity\ncreated: 2020-01-01\n---\nBody")
    assert m.main() == 0
    out = capsys.readouterr().out
    assert "No canonical types declared" in out and '"wakeAgent": true' in out
    assert "hint=(none)" in out and "tags: (none)" in out

    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
