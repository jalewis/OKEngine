"""Cockpit overlay must wrap content at any font size; dashboard cards must scale."""
import pathlib, re
CSS = (pathlib.Path(__file__).resolve().parents[1] / "okengine-cockpit/static/style.css").read_text()


def test_md_tables_scroll_not_crush():
    # SUPERSEDES the old wrap-within-overlay contract, which forced every cell to wrap and broke
    # dates (2026-07-01) at their hyphens whenever the table squeezed (operator report). The new
    # contract: data cells NEVER wrap, ONLY the last column (title/description — the long one)
    # wraps, and an over-wide table scrolls horizontally instead of crushing columns.
    tbl = re.search(r"\.md table\{[^}]*\}", CSS)
    assert tbl and "overflow-x:auto" in tbl.group(0) and "max-width:100%" in tbl.group(0)
    cells = re.search(r"\.md td,\.md th\{[^}]*\}", CSS)
    assert cells and "white-space:nowrap" in cells.group(0)
    last = re.search(r"\.md td:last-child,\.md th:last-child\{[^}]*\}", CSS)
    assert last and "white-space:normal" in last.group(0) and "overflow-wrap:anywhere" in last.group(0)


def test_md_content_wraps_long_tokens():
    assert "overflow-wrap:" in CSS


def test_dashboard_card_title_scales_with_font_control():
    m = re.search(r"\.dash-card \.dash-t\{[^}]*\}", CSS)
    assert m and "rem" in m.group(0) and "13px" not in m.group(0)


def test_table_headers_dont_wrap():
    # short column headers (Score, Conf, ...) stay on one line, not broken per-letter
    ths = re.findall(r"\.md th\{[^}]*\}", CSS)
    assert any("white-space:nowrap" in t for t in ths)


def test_predictions_claim_wraps():
    # claim cell must wrap — max-width is ignored on auto-layout table cells, so nowrap
    # rendered each claim as one long line and the ledger overflowed sideways
    m = re.search(r"\.claim\{[^}]*\}", CSS)
    assert m and "nowrap" not in m.group(0)


def test_predictions_subject_cleaned():
    JS = (pathlib.Path(__file__).resolve().parents[1] / "okengine-cockpit/static/app.js").read_text()
    assert "function subjCell" in JS and "subjCell(p.subject)" in JS


def test_num_cells_never_wrap():
    """Operator report: date cells (Resolves by / Updated / Anchored) in .ledger tables broke
    mid-token (2026-09-30) when a long first column squeezed the table. `.num` cells (right-aligned
    mono — numbers AND dates) must never wrap. The .md-table rule only covers tables whose LONG
    column is last; a ledger with the date last needs this."""
    m = re.search(r"td\.num,th\.num\{[^}]*\}", CSS)
    assert m and "white-space:nowrap" in m.group(0), "date/number cells must not wrap"
