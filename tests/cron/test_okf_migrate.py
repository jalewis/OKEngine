"""okf_migrate (okengine#165): the mover follows the governing schema's partitioning —
flat AND wrongly-nested pages re-nest to the canonical key; duplicate-slug collisions
are held back for the dedup pass; link rewrite covers nested old paths."""
import importlib
import pathlib
import sys

import pytest

yaml = pytest.importorskip("yaml")
REPO = pathlib.Path(__file__).resolve().parents[2]


def _mod():
    sys.path.insert(0, str(REPO / "scripts" / "cron"))
    import okf_migrate
    return importlib.reload(okf_migrate)


def _vault(tmp_path):
    (tmp_path / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\n"
        "types:\n  vendor: {}\n  malware: {}\n")
    w = tmp_path / "wiki" / "entities"
    # flat page -> by-letter
    (w / "flatcorp.md").parent.mkdir(parents=True, exist_ok=True)
    (w / "flatcorp.md").write_text("---\ntype: vendor\n---\n")
    # legacy type-dir page -> re-nest to by-letter
    (w / "vendor" / "a").mkdir(parents=True)
    (w / "vendor" / "a" / "acme.md").write_text("---\ntype: vendor\n---\n")
    # already canonical -> untouched
    (w / "g").mkdir()
    (w / "g" / "goodco.md").write_text("---\ntype: vendor\n---\n")
    # duplicate slug: legacy AND canonical both exist -> collision, held back
    (w / "malware" / "d").mkdir(parents=True)
    (w / "malware" / "d" / "dupbot.md").write_text("---\ntype: malware\n---\nlegacy copy\n")
    (w / "d").mkdir()
    (w / "d" / "dupbot.md").write_text("---\ntype: malware\n---\ncanonical copy\n")
    # a page linking the legacy paths
    (tmp_path / "wiki" / "concepts").mkdir()
    (tmp_path / "wiki" / "concepts" / "note.md").write_text(
        "---\ntype: concept\n---\nsee [[entities/vendor/a/acme|Acme]] and "
        "[[entities/flatcorp]] and [[entities/malware/d/dupbot]]\n")
    return tmp_path


def test_build_map_renests_and_holds_collisions(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    move_map, collisions = m.build_map(root, "entities")
    assert move_map["entities/flatcorp"] == "entities/f/flatcorp"
    assert move_map["entities/vendor/a/acme"] == "entities/a/acme"
    assert "entities/g/goodco" not in move_map              # canonical already
    assert ("entities/malware/d/dupbot", "entities/d/dupbot") in collisions
    assert "entities/malware/d/dupbot" not in move_map      # held back, not clobbered


def test_apply_moves_and_rewrites_nested_links(tmp_path):
    m = _mod()
    root = _vault(tmp_path)
    rc = m.main(["--namespace", "entities", "--apply", "--root", str(root)])
    assert rc == 0
    assert (root / "wiki" / "entities" / "a" / "acme.md").is_file()
    assert not (root / "wiki" / "entities" / "vendor" / "a" / "acme.md").exists()
    assert (root / "wiki" / "entities" / "f" / "flatcorp.md").is_file()
    note = (root / "wiki" / "concepts" / "note.md").read_text()
    assert "[[entities/a/acme|Acme]]" in note               # alias preserved
    assert "[[entities/f/flatcorp]]" in note
    assert "[[entities/malware/d/dupbot]]" in note          # collision link untouched
    # both dupbot copies still exist — nothing clobbered
    assert (root / "wiki" / "entities" / "malware" / "d" / "dupbot.md").is_file()
    assert (root / "wiki" / "entities" / "d" / "dupbot.md").read_text().endswith("canonical copy\n")


def test_two_sources_one_destination_both_held(tmp_path):
    m = _mod()
    root = tmp_path
    (root / "schema.yaml").write_text(
        "partitioning:\n  namespaces:\n    entities: {strategy: by-letter}\ntypes:\n  vendor: {}\n")
    w = root / "wiki" / "entities"
    (w / "vendor" / "x").mkdir(parents=True)
    (w / "vendor" / "x" / "xcorp.md").write_text("---\ntype: vendor\n---\nA\n")
    (w / "xcorp.md").write_text("---\ntype: vendor\n---\nB\n")
    move_map, collisions = m.build_map(root, "entities")
    assert "entities/vendor/x/xcorp" not in move_map
    assert "entities/xcorp" not in move_map
    assert len([c for c in collisions if c[1] == "entities/x/xcorp"]) == 2
