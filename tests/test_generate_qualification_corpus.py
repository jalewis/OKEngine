import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_corpus", ROOT / "scripts/generate_qualification_corpus.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_generator_is_deterministic_typed_and_sharded(tmp_path):
    first = MODULE.generate(tmp_path / "one", 100)
    second = MODULE.generate(tmp_path / "two", 100)
    assert first == second
    assert first["pages"] == 100
    assert set(first["types"]) == set(MODULE.TYPES)
    assert sum(first["types"].values()) == 100
    assert len(list((tmp_path / "one/wiki").rglob("*.md"))) == 100
    assert list((tmp_path / "one/wiki/entities").glob("*/*.md"))
    assert (tmp_path / "one/raw").is_dir()


def test_generator_replaces_owned_wiki_without_stale_pages(tmp_path):
    stale = tmp_path / "wiki/stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    MODULE.generate(tmp_path, 10)
    assert not stale.exists()
    assert len(list((tmp_path / "wiki").rglob("*.md"))) == 10


def test_manifest_matches_returned_identity(tmp_path):
    report = MODULE.generate(tmp_path, 25)
    stored = json.loads((tmp_path / ".okengine/qualification-corpus.json").read_text())
    assert stored == report and len(report["sha256"]) == 64


def test_too_small_corpus_is_refused(tmp_path):
    try:
        MODULE.generate(tmp_path, 4)
    except ValueError as exc:
        assert "at least 5" in str(exc)
    else:
        raise AssertionError("under-representative corpus accepted")


def test_generator_cli_success_and_error(tmp_path, capsys):
    assert MODULE.main([str(tmp_path / "ok"), "--pages", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["pages"] == 5
    try:
        MODULE.main([str(tmp_path / "bad"), "--pages", "1"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("argparse error did not exit")
    assert "at least 5" in capsys.readouterr().err
