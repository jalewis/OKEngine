import importlib.util
import sys
from pathlib import Path

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
