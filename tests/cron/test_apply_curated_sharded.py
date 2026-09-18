"""Regression: apply_curated_entity_fields must resolve pages in their by-letter shard.

resolve_page() probed only the flat `<namespace>/<slug>.md`, so on the entities/concepts
namespaces (which shard by leading letter) it returned None and curated fields were silently
never enforced. This pins resolution through the shared shard-aware okf_migrate.find_page.
"""
import importlib.util
import os
import runpy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "apply_curated_entity_fields.py"

pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="script absent")


def _load(vault: Path):
    os.environ["WIKI_PATH"] = str(vault)
    spec = importlib.util.spec_from_file_location("apply_curated_entity_fields", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["apply_curated_entity_fields"] = m
    spec.loader.exec_module(m)
    return m


def test_resolves_sharded_entity(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "a").mkdir(parents=True)
    page = wiki / "entities" / "a" / "acme.md"
    page.write_text("---\ntype: entity\n---\n# Acme\n", encoding="utf-8")
    m = _load(tmp_path)
    assert m.resolve_page("acme", ["entities"]) == page      # was None (flat-only probe)


def test_resolves_flat_entity_too(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "entities").mkdir(parents=True)
    page = wiki / "entities" / "flatco.md"
    page.write_text("---\ntype: entity\n---\n# Flatco\n", encoding="utf-8")
    m = _load(tmp_path)
    assert m.resolve_page("flatco", ["entities"]) == page


def test_missing_slug_returns_none(tmp_path):
    (tmp_path / "wiki" / "entities").mkdir(parents=True)
    m = _load(tmp_path)
    assert m.resolve_page("nope", ["entities"]) is None


def test_enforce_overwrites_fields_and_preserves_body(tmp_path):
    m = _load(tmp_path)
    text = (
        "---\ntype: actor\norigin: Unknown\naliases:\n  - Old\nother: keep\n---\n"
        "# Body\n\nByte-identical.\n"
    )
    new, changed = m.enforce(text, {
        "_comment": "operator truth",
        "origin": "Russia",
        "aliases": ["[[entities/a/apt29]]", "Cozy Bear"],
        "verified": True,
    })
    assert changed == ["origin", "aliases", "verified"]
    assert new.endswith("# Body\n\nByte-identical.\n")
    assert "# operator truth" in new
    assert "origin: Russia" in new
    assert '  - "[[entities/a/apt29]]"' in new
    assert "verified: true" in new
    assert "other: keep" in new
    assert m.enforce(new, {"origin": "Russia", "aliases": [
        "[[entities/a/apt29]]", "Cozy Bear"], "verified": True}) == (None, [])


def test_main_applies_overlay_and_rejects_invalid_json(tmp_path, monkeypatch, capsys):
    wiki = tmp_path / "wiki" / "entities" / "a"
    wiki.mkdir(parents=True)
    page = wiki / "acme.md"
    page.write_text("---\ntype: actor\norigin: Unknown\n---\n# Acme\n", encoding="utf-8")
    overlay = tmp_path / "curated.json"
    overlay.write_text('{"acme": {"origin": "US"}, "bad": "not-an-object"}')
    m = _load(tmp_path)
    monkeypatch.setattr(m, "OVERLAY_PATH", overlay)
    monkeypatch.setattr(m, "curated_namespaces", lambda: ["entities"])

    assert m.main() == 0
    assert "origin: US" in page.read_text()
    assert "overlay value not an object" in capsys.readouterr().out

    overlay.write_text("{bad json")
    assert m.main() == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_namespaces_resolution_and_field_rendering(tmp_path, monkeypatch):
    m = _load(tmp_path)
    monkeypatch.setenv("CURATED_NAMESPACE", " concepts ")
    assert m.curated_namespaces() == ["concepts"]
    monkeypatch.delenv("CURATED_NAMESPACE")
    monkeypatch.setattr(m.schema_lib, "governing_schema", lambda _vault: {})
    monkeypatch.setattr(m.schema_lib, "knowledge_namespaces", lambda _schema: set())
    assert m.curated_namespaces() == [""]

    direct = tmp_path / "wiki" / "entities" / "acme.md"
    direct.parent.mkdir(parents=True)
    direct.write_text("---\ntype: actor\n---\n")
    assert m.resolve_page("entities/acme", ["other"]) == direct
    root = tmp_path / "wiki" / "root.md"
    root.write_text("---\ntype: note\n---\n")
    assert m.resolve_page("root", [""]) == root

    assert m.render_field("items", []) == ["items: []"]
    assert m.render_field("items", [" plain ", "a:b", 'a"b']) == [
        "items:", '  - " plain "', '  - "a:b"', '  - a"b']
    assert m.render_field("enabled", False) == ["enabled: false"]
    assert m.render_field("link", "[[x]]") == ['link: "[[x]]"']
    assert m.render_field("tag", "a#b") == ['tag: "a#b"']


def test_enforce_rejects_bad_documents_and_handles_missing_anchor(tmp_path):
    m = _load(tmp_path)
    assert m.split_fm_body("plain") is None
    assert not m.parses_with_type("[broken")
    assert not m.parses_with_type("- scalar\n")
    assert m.enforce("plain", {"x": 1}) == (None, [])
    assert m.enforce("---\ntype: [broken\n---\nbody", {"x": 1}) == (None, [])
    # A typeless mapping receives the block but is still rejected by the safety gate.
    assert m.enforce("---\nname: x\n---\nbody", {"x": 1}) == (None, [])

    text = "---\ntype: note\nitems:\n  - old\n\nkeep: yes\n---\nbody"
    new, changed = m.enforce(text, {"items": ["new"]})
    assert changed == ["items"]
    assert "  - old" not in new and "keep: yes" in new
    # A removed block followed immediately by prose (not an indented continuation)
    # must reset block skipping and retain that prose.
    assert "prose" in m._strip_keys("items:\n  - old\nprose", {"items"})

    original_split = m.split_fm_body
    calls = {"count": 0}
    def unsafe_second(value):
        calls["count"] += 1
        return None if calls["count"] == 2 else original_split(value)
    m.split_fm_body = unsafe_second
    assert m.enforce("---\ntype: note\n---\nbody", {"x": 1}) == (None, [])
    m.split_fm_body = original_split


def test_main_missing_overlay_missing_page_read_error_and_permission(
        tmp_path, monkeypatch, capsys):
    m = _load(tmp_path)
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(m, "OVERLAY_PATH", missing)
    assert m.main() == 0
    assert "nothing to enforce" in capsys.readouterr().out

    overlay = tmp_path / "overlay.json"
    overlay.write_text('{"missing": {"x": 1}}')
    monkeypatch.setattr(m, "OVERLAY_PATH", overlay)
    monkeypatch.setattr(m, "curated_namespaces", lambda: ["entities"])
    assert m.main() == 0
    assert "page not found" in capsys.readouterr().out

    page = tmp_path / "wiki" / "entities" / "x.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: actor\nx: old\n---\nbody")
    overlay.write_text('{"x": {"x": "new"}}')
    monkeypatch.setattr(m, "resolve_page", lambda *_args: page)
    original_read = Path.read_text
    monkeypatch.setattr(Path, "read_text",
                        lambda path, *a, **k: (_ for _ in ()).throw(OSError())
                        if path == page else original_read(path, *a, **k))
    assert m.main() == 0
    monkeypatch.setattr(Path, "read_text", original_read)

    original_write = Path.write_text
    monkeypatch.setattr(Path, "write_text",
                        lambda path, *a, **k: (_ for _ in ()).throw(PermissionError())
                        if path == page else original_write(path, *a, **k))
    assert m.main() == 0
    assert "Permission-skipped: 1" in capsys.readouterr().out

    # Already-correct content exercises the main-loop no-change path.
    monkeypatch.setattr(Path, "write_text", original_write)
    page.write_text("---\ntype: actor\nx: new\n---\nbody")
    assert m.main() == 0


def test_resolve_checks_empty_and_multiple_namespaces(tmp_path):
    m = _load(tmp_path)
    (tmp_path / "wiki").mkdir(exist_ok=True)
    assert m.resolve_page("missing", ["entities", "", "concepts"]) is None


def test_empty_namespace_resolves_page_created_during_scan(tmp_path, monkeypatch):
    m = _load(tmp_path)
    page = tmp_path / "wiki/race.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: note\n---\n")
    original = Path.exists
    calls = {page: 0}
    def appears(path):
        if path == page:
            calls[path] += 1
            return calls[path] > 1
        return original(path)
    monkeypatch.setattr(Path, "exists", appears)
    assert m.resolve_page("race", [""]) == page


def test_apply_curated_entrypoint_missing_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_PATH", str(tmp_path))
    monkeypatch.setenv("CURATED_FIELDS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(sys, "argv", [str(MOD)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(MOD), run_name="__main__")
    assert exc.value.code == 0
