import importlib.util
import runpy
import sys
from pathlib import Path

import pytest
REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cron" / "repair_malformed_slugs.py"


def _load():
    spec = importlib.util.spec_from_file_location("repair_malformed_slugs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    return mod


def test_repairs_slug_and_exact_wikilinks(tmp_path):
    mod = _load()
    wiki = tmp_path / "wiki"
    old = wiki / "entities" / "a" / "this is malformed.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\ntype: entity\nname: Clean Actor\n---\nbody\n")
    ref = wiki / "briefings" / "b.md"
    ref.parent.mkdir()
    ref.write_text("[[entities/a/this is malformed]]\n")
    (ref.parent / "unrelated.md").write_text("no matching link\n")

    planned, errors = mod.repair(tmp_path, apply=False)
    assert errors == []
    assert planned == [("entities/a/this is malformed", "entities/a/clean-actor")]
    assert old.exists()

    moved, errors = mod.repair(tmp_path, apply=True)
    assert errors == []
    assert moved == planned
    assert not old.exists()
    assert (wiki / "entities" / "a" / "clean-actor.md").exists()
    assert "[[entities/a/clean-actor]]" in ref.read_text()


def test_helpers_long_slug_and_non_entities_are_ignored(tmp_path):
    mod = _load()
    assert mod.malformed(Path("has space.md"))
    assert not mod.malformed(Path("good.md"))
    long = "a" * 100
    slug = mod.bounded_slug(long)
    assert len(slug) <= mod.MAX_ENTITY_SLUG_LEN and slug != long
    assert mod.bounded_slug("Simple Name") == "simple-name"
    wiki = tmp_path / "wiki"
    (wiki / "sources").mkdir(parents=True)
    (wiki / "sources" / "bad name.md").write_text("x")
    assert mod.repair(tmp_path) == ([], [])


def test_repair_yaml_fallback_collision_and_same_target(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki" / "entities" / "a"
    wiki.mkdir(parents=True)
    bad = wiki / "bad yaml.md"
    bad.write_text("---\n[\n---\nbody")
    collision = wiki / "bad-yaml.md"
    collision.write_text("existing")
    moves, errors = mod.repair(tmp_path)
    assert moves == [] and "collision" in errors[0]

    collision.unlink()
    # A normalizer that returns the original malformed stem exercises the
    # no-op guard (important on custom normalization policies).
    monkeypatch.setattr(mod, "bounded_slug", lambda value: bad.stem)
    assert mod.repair(tmp_path) == ([], [])


def test_repair_scan_read_and_reference_write_races(tmp_path, monkeypatch):
    mod = _load()
    wiki = tmp_path / "wiki"
    old = wiki / "entities" / "a" / "bad name.md"
    old.parent.mkdir(parents=True)
    old.write_text("---\nname: Good\n---\n")
    ref = wiki / "briefings" / "ref.md"
    ref.parent.mkdir()
    ref.write_text("[[entities/a/bad name]]")
    vanished = wiki / "entities" / "a" / "gone name.md"
    vanished.write_text("---\nname: Gone\n---\n")
    original_read = Path.read_text
    original_write = Path.write_text

    def flaky_read(path, *args, **kwargs):
        if path == vanished:
            raise OSError("gone")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    monkeypatch.setattr(
        Path, "write_text",
        lambda path, *a, **k: (
            (_ for _ in ()).throw(OSError("readonly"))
            if path == ref else original_write(path, *a, **k)
        ),
    )
    moves, errors = mod.repair(tmp_path, apply=True)
    assert ("entities/a/bad name", "entities/a/good") in moves
    assert any("could not rewrite" in x for x in errors)


def test_main_dry_apply_error_and_entrypoint(tmp_path, monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "repair", lambda *_a: ([("old", "new")], []))
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path)])
    assert mod.main() == 0
    assert "WOULD MOVE" in capsys.readouterr().out
    monkeypatch.setattr(mod, "repair", lambda *_a: ([("old", "new")], ["bad"]))
    monkeypatch.setattr(sys, "argv", ["repair", "--vault", str(tmp_path), "--apply"])
    assert mod.main() == 1
    captured = capsys.readouterr()
    assert "MOVE old -> new" in captured.out and "ERROR bad" in captured.err

    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--vault", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert exc.value.code == 0
