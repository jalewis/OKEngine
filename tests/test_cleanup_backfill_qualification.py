import importlib.util
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cleanup_backfill_qualification",
    REPO / "scripts" / "cleanup_backfill_qualification.py",
)
C = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = C
SPEC.loader.exec_module(C)


def test_candidates_are_scoped_to_qualification_artifacts(tmp_path):
    pack = tmp_path / "okcti-test"
    predictions = pack / "wiki/predictions"
    sources = pack / "wiki/sources/2026/07/27"
    predictions.mkdir(parents=True)
    sources.mkdir(parents=True)
    fake = predictions / "qwen-qualification-g9.md"
    fake.write_text("---\ntype: prediction\n---\nQwen qualification G9\n")
    generated = sources / "qwen-final-control-g9.md"
    generated.write_text("---\nqualification_fixture: true\n---\n")
    real = predictions / "real-forecast.md"
    real.write_text(
        "---\ntype: prediction\n---\nA genuine forecast referencing "
        "[[predictions/qwen-qualification-g9]].\n"
    )

    assert C.candidates(pack, "g9") == [fake, generated]
    assert real not in C.candidates(pack)


def test_future_matrix_marks_and_cleans_all_fixtures_on_exit():
    prepare = (REPO / "scripts/prepare_backfill_qualification.py").read_text()
    matrix = (REPO / "scripts/run_backfill_qualification_matrix.sh").read_text()
    assert "qualification_fixture: true" in prepare
    assert "cleanup_backfill_qualification.py" in matrix
    assert "PSB_TARGET" in matrix


def test_candidates_generation_filter_and_main_apply(tmp_path, monkeypatch, capsys):
    root = tmp_path / "packs"
    pack = root / "okcti-test"
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text("name: test\n", encoding="utf-8")
    fixture = pack / "raw/qualification/qwen-final-g20.md"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("qualification_fixture: true\n")
    other = pack / "wiki/sources/qwen-final-g21.md"
    other.parent.mkdir(parents=True)
    other.write_text("qualification.invalid\n")
    directory = pack / "wiki/concepts/qwen-final-dir"
    directory.mkdir(parents=True)

    assert C.candidates(pack, "g20") == [fixture]
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cleanup_backfill_qualification",
            "--pack-root",
            str(root),
            "--pack",
            "okcti-test",
            "--generation",
            "g20",
        ],
    )
    assert C.main() == 0
    assert fixture.exists()
    assert "WOULD REMOVE" in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cleanup_backfill_qualification",
            "--pack-root",
            str(root),
            "--pack",
            "okcti-test",
            "--generation",
            "g20",
            "--apply",
        ],
    )
    assert C.main() == 0
    assert not fixture.exists()
    assert other.exists()
    assert "REMOVE" in capsys.readouterr().out


def test_main_refuses_a_directory_without_pack_manifest(tmp_path, monkeypatch):
    root = tmp_path / "packs"
    (root / "not-a-pack").mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", ["cleanup", "--pack-root", str(root),
                                      "--pack", "not-a-pack"])
    with pytest.raises(SystemExit, match="no pack.yaml"):
        C.main()


def test_cleanup_rejects_missing_pack_and_skips_read_errors(tmp_path, monkeypatch):
    root = tmp_path / "packs"
    root.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cleanup_backfill_qualification",
            "--pack-root",
            str(root),
            "--pack",
            "okcti-test",
        ],
    )
    with pytest.raises(SystemExit, match="invalid pack path"):
        C.main()

    pack = root / "okcti-test"
    page = pack / "wiki/sources/qwen-final-g20.md"
    page.parent.mkdir(parents=True)
    page.write_text("qualification.invalid")
    original = Path.read_text

    def fail_selected(path, *args, **kwargs):
        if path == page:
            raise OSError("unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_selected)
    assert C.candidates(pack) == []
