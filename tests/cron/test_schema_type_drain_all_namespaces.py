"""#271: the safe type drain covers every governed namespace."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(monkeypatch, vault):
    monkeypatch.setenv("WIKI_PATH", str(vault))
    sys.modules.pop("schema_type_drain", None)
    spec = importlib.util.spec_from_file_location(
        "schema_type_drain", REPO / "scripts" / "cron" / "schema_type_drain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_alias_and_explicit_map_drain_sources_and_entities(monkeypatch, tmp_path):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n  source: {required: [type]}\n"
        "type_aliases: {threat_actor: actor}\n", encoding="utf-8")
    actor = tmp_path / "wiki" / "entities" / "a.md"
    source = tmp_path / "wiki" / "sources" / "s.md"
    actor.write_text("---\ntype: threat_actor\nid: x:a\n---\nbody\n", encoding="utf-8")
    source.write_text("---\ntype: collapsed source metadata\nid: x:s\n---\nbody\n", encoding="utf-8")
    mapping = tmp_path / "map.yaml"
    mapping.write_text("'collapsed source metadata': source\n", encoding="utf-8")
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py", "--map", str(mapping)])
    assert module.main() == 0
    assert "type: actor" in actor.read_text() and "type: source" in source.read_text()
    assert actor.read_text().endswith("body\n") and source.read_text().endswith("body\n")


def test_page_specific_map_does_not_retype_other_pages(monkeypatch, tmp_path):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types:\n  actor: {required: [type]}\n", encoding="utf-8")
    target = tmp_path / "wiki" / "entities" / "one.md"
    other = tmp_path / "wiki" / "entities" / "two.md"
    target.write_text("---\ntype: ambiguous\nid: one\n---\none\n", encoding="utf-8")
    other.write_text("---\ntype: ambiguous\nid: two\n---\ntwo\n", encoding="utf-8")
    mapping = tmp_path / "map.yaml"
    mapping.write_text("paths:\n  entities/one.md: actor\n", encoding="utf-8")
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py", "--map", str(mapping)])

    assert module.main() == 0
    assert "type: actor" in target.read_text()
    assert "type: ambiguous" in other.read_text()


def test_page_specific_map_replaces_multiline_corrupt_type_scalar(monkeypatch, tmp_path):
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types:\n  concept: {required: [type]}\n", encoding="utf-8")
    page = tmp_path / "wiki" / "concepts" / "broken.md"
    page.write_text(
        "---\nid: broken\ntype: incident-prediction-target-page-lane source-reporting\n"
        "  continuation-that-was-folded-into-type\nstatus: draft\n---\nbody\n",
        encoding="utf-8",
    )
    mapping = tmp_path / "map.yaml"
    mapping.write_text("paths:\n  concepts/broken.md: concept\n", encoding="utf-8")
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py", "--map", str(mapping)])

    assert module.main() == 0
    rewritten = page.read_text()
    assert "type: concept\nstatus: draft" in rewritten
    assert "continuation-that-was-folded-into-type" not in rewritten
    assert rewritten.endswith("body\n")


def test_corpus_audit_ignores_engine_structural_bundle(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "corpus_audit_271", REPO / "scripts" / "cron" / "corpus_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {source: {required: [type]}}\n")
    (tmp_path / "wiki" / "BUNDLE.md").write_text("---\ntype: bundle\n---\nstructural\n")
    assert module.audit(tmp_path)["off_taxonomy"] == {}
