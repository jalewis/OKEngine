"""Regression: the generic DOCX/PPTX stage-1 extractor.

Splits into two halves: the module must import and behave gracefully WITHOUT the
optional office libs (no hard dep — the core contract of issue #5), and when the
libs ARE present it must actually extract body + table + notes text and honour the
idempotency / empty-output rules. The lib-dependent half importorskips."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ED = REPO / "scripts" / "extract-docs.py"


def _load():
    spec = importlib.util.spec_from_file_location("extract_docs", ED)
    m = importlib.util.module_from_spec(spec)
    sys.modules["extract_docs"] = m
    spec.loader.exec_module(m)
    return m


# --- no-hard-dependency contract (always runs) ---------------------------------

def test_module_imports_without_office_libs():
    # Importing must never require python-docx/pptx — it's an optional dep.
    assert _load() is not None


_ALL_EXTS = {".docx", ".pptx", ".xlsx", ".rtf", ".doc"}


def test_build_backends_reports_missing_without_crashing():
    m = _load()
    backends, missing = m._build_backends()
    assert isinstance(backends, dict) and isinstance(missing, list)
    # Each present backend is a known format; every format is present XOR missing.
    assert set(backends).issubset(_ALL_EXTS)
    assert len(backends) + len(missing) == len(_ALL_EXTS)


def test_noop_exit_zero_when_no_backends(tmp_path, monkeypatch):
    """With neither office lib available, a populated raw root is a clean no-op."""
    m = _load()
    (tmp_path / "a.docx").write_bytes(b"not really a docx")
    monkeypatch.setattr(m, "_build_backends", lambda: ({}, ["python-docx (.docx)",
                                                             "python-pptx (.pptx)"]))
    assert m.main([str(tmp_path)]) == 0
    assert not (tmp_path / "a.docx.txt").exists()   # nothing written


def test_errors_on_missing_raw_root():
    m = _load()
    assert m.main(["/nonexistent/raw/root/xyz"]) == 1


# --- real extraction (requires the libs) ---------------------------------------

def test_docx_extracts_paragraphs_and_tables(tmp_path):
    docx = pytest.importorskip("docx")
    m = _load()
    d = docx.Document()
    d.add_paragraph("First real paragraph of the document body.")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Cell-A"
    t.rows[0].cells[1].text = "Cell-B"
    src = tmp_path / "report.docx"
    d.save(str(src))

    assert m.main([str(tmp_path)]) == 0
    companion = tmp_path / "report.docx.txt"
    assert companion.is_file()
    text = companion.read_text()
    assert "First real paragraph" in text
    assert "Cell-A | Cell-B" in text          # table cells joined

    # Idempotent: a second run skips (companion newer than source).
    mtime = companion.stat().st_mtime
    assert m.main([str(tmp_path)]) == 0
    assert companion.stat().st_mtime == mtime


def test_pptx_extracts_shape_and_notes(tmp_path):
    pptx = pytest.importorskip("pptx")
    m = _load()
    pr = pptx.Presentation()
    slide = pr.slides.add_slide(pr.slide_layouts[5])  # title-only layout has a title ph
    slide.shapes.title.text = "Slide Title Text"
    slide.notes_slide.notes_text_frame.text = "Speaker note body."
    src = tmp_path / "deck.pptx"
    pr.save(str(src))

    assert m.main([str(tmp_path)]) == 0
    text = (tmp_path / "deck.pptx.txt").read_text()
    assert "Slide Title Text" in text
    assert "[notes] Speaker note body." in text


def test_force_reextracts(tmp_path):
    docx = pytest.importorskip("docx")
    m = _load()
    d = docx.Document()
    d.add_paragraph("Body text here for the force test.")
    src = tmp_path / "f.docx"
    d.save(str(src))
    assert m.main([str(tmp_path)]) == 0
    companion = tmp_path / "f.docx.txt"
    # Make the companion look newer, then --force must still rewrite it.
    os.utime(companion, (companion.stat().st_atime, companion.stat().st_mtime + 100))
    before = companion.stat().st_mtime
    assert m.main(["--force", str(tmp_path)]) == 0
    assert companion.stat().st_mtime != before


# --- residual formats: XLSX / RTF / DOC (#7) -----------------------------------

def test_all_formats_registered_or_gracefully_missing():
    """Every supported format is either a live backend or named in `missing`
    (graceful skip) — including .doc, which depends on a host tool (antiword)."""
    m = _load()
    backends, missing = m._build_backends()
    miss = " ".join(missing)
    for ext in (".docx", ".pptx", ".xlsx", ".rtf", ".doc"):
        assert ext in backends or ext in miss, ext


def test_xlsx_extracts_cells(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    m = _load()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Header-A", "Header-B"])
    ws.append(["v1", "v2"])
    wb.save(str(tmp_path / "book.xlsx"))
    assert m.main([str(tmp_path)]) == 0
    text = (tmp_path / "book.xlsx.txt").read_text()
    assert "# sheet: Data" in text
    assert "Header-A | Header-B" in text and "v1 | v2" in text


def test_rtf_extracts_text(tmp_path):
    pytest.importorskip("striprtf")
    m = _load()
    (tmp_path / "note.rtf").write_text(
        r"{\rtf1\ansi\deff0{\fonttbl{\f0 Times;}}\f0\fs24 Hello RTF body text here.\par}")
    assert m.main([str(tmp_path)]) == 0
    assert "Hello RTF body text here." in (tmp_path / "note.rtf.txt").read_text()
