"""#271: the safe type drain covers every governed namespace."""
from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path

import pytest

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


def test_parsing_and_remap_defensive_paths(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    assert module.split_fm_body("plain") is None
    assert module.current_type("plain") == ""
    assert module.current_type("---\ntype: [broken\n---\nbody") == ""
    assert module.current_type("---\n- scalar\n---\nbody") == ""
    assert module.target_type("Alias", set(), {"alias": "canonical"}) == "canonical"
    assert module.target_type("Canonical", {"canonical"}, {}) == "canonical"
    assert module.target_type("other", {"canonical"}, {}) is None

    assert module.remap("plain", {"actor"}, {}) == (None, "", "")
    assert module.remap("---\nname: x\n---\nbody", {"actor"}, {}) == (None, "", "")
    assert module.remap("---\ntype: [broken\n---\nbody", {"actor"}, {}) == (None, "", "")
    assert module.remap("---\n- type\n---\nbody", {"actor"}, {}) == (None, "", "")
    assert module.remap("---\ntype: actor\n---\nbody", {"actor"}, {}) == (None, "actor", "")


def test_remap_safety_gate_refusal_paths(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    text = "---\ntype: old\n---\nbody"

    monkeypatch.setattr(module.yaml, "safe_load", lambda _text: ["not", "mapping"])
    assert module.remap(text, {"actor"}, {"old": "actor"}) == (None, "", "")

    calls = iter([{"type": "old"}, module.yaml.YAMLError("bad rewrite")])
    monkeypatch.setattr(module.yaml, "safe_load",
                        lambda _text: (_value if not isinstance(
                            (_value := next(calls)), Exception)
                            else (_ for _ in ()).throw(_value)))
    assert module.remap(text, {"actor"}, {"old": "actor"}) == (None, "old", "")

    calls = iter([{"type": "old"}, {"type": "wrong"}])
    monkeypatch.setattr(module.yaml, "safe_load", lambda _text: next(calls))
    assert module.remap(text, {"actor"}, {"old": "actor"}) == (None, "old", "")

    original_split = module.split_fm_body
    calls = 0
    def split_then_refuse(value):
        nonlocal calls
        calls += 1
        return original_split(value) if calls == 1 else None
    monkeypatch.setattr(module, "split_fm_body", split_then_refuse)
    parsed = iter([{"type": "old"}, {"type": "actor"}])
    monkeypatch.setattr(module.yaml, "safe_load", lambda _text: next(parsed))
    assert module.remap(text, {"actor"}, {"old": "actor"}) == (None, "old", "")


@pytest.mark.parametrize("content", [
    "[not, a, mapping]\n",
    "types: {old: 3}\n",
    "types: {}\nextra: {}\n",
])
def test_invalid_explicit_maps_are_rejected(monkeypatch, tmp_path, content):
    mapping = tmp_path / "map.yaml"
    mapping.write_text(content, encoding="utf-8")
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py", "--map", str(mapping)])
    assert module.main() == 2


def test_missing_map_and_missing_wiki_paths(monkeypatch, tmp_path, capsys):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py", "--map", str(tmp_path / "missing")])
    assert module.main() == 2
    assert "cannot load type map" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py"])
    assert module.main() == 0
    assert '"wakeAgent": false' in capsys.readouterr().out


def test_dry_run_skip_filters_and_permission_report(monkeypatch, tmp_path, capsys):
    wiki = tmp_path / "wiki" / "entities"
    wiki.mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types: {actor: {required: [type]}}\ntype_aliases: {old: actor}\n")
    target = wiki / "target.md"
    target.write_text("---\ntype: old\n---\nbody\n")
    for name in ("_hidden.md", "INDEX.md", "copy.bak.md"):
        (wiki / name).write_text("---\ntype: old\n---\nbody\n")
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py", "--dry-run"])
    assert module.main() == 0
    assert "type: old" in target.read_text()
    assert "Would drain 1 page(s)" in capsys.readouterr().out

    original = Path.write_text
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", denied)
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py"])
    assert module.main() == 0
    assert "Permission-skipped: 1" in capsys.readouterr().out


def test_schema_type_drain_entrypoint_missing_wiki(tmp_path, monkeypatch):
    script = REPO / "scripts" / "cron" / "schema_type_drain.py"
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setattr(sys, "argv", [str(script)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script), run_name="__main__")
    assert exc.value.code == 0


def test_schema_type_drain_tolerates_page_disappearing_during_scan(tmp_path, monkeypatch):
    page = tmp_path / "wiki" / "entities" / "gone.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: old\n---\nbody\n")
    module = _load(monkeypatch, tmp_path)
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("vanished"))
            if self == page else original(self, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(sys, "argv", ["schema_type_drain.py"])
    assert module.main() == 0
