"""The review queue is a worklist, so the newest row goes on top (okengine#521).

Appending buried each new flag under the entire backlog — on one live vault, under 3,221 older
rows. Three pieces have to agree: the write path prepends, the one-time reorder puts existing
vaults in that order, and the cockpit worklist sorts newest-first WITHIN its reason bands.

The band detail is the trap. `reverse=True` on the whole sort key would have inverted the
reason priority too, sending `grounding` (the most urgent) to the bottom — which is why the
sort inverts a single component instead.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent
REVERSE = REPO / "scripts" / "reverse_review_queue.py"


def _load(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HEADER = ("---\ntitle: Review Queue\n---\n\n"
          "# Review Queue\n\nAgent-flagged pages awaiting human review "
          "(highlight, not a gate — the writes already landed).\n\n")


# --------------------------------------------------------------- the reorder


def test_reverses_existing_rows(tmp_path):
    m = _load("revq", REVERSE)
    text = HEADER + "- 2026-07-01 **a.md** — x\n- 2026-07-02 **b.md** — y\n"
    new, moved = m.reorder(text)
    assert moved == 2
    rows = [ln for ln in new.splitlines() if ln.startswith("- ")]
    assert rows[0].startswith("- 2026-07-02")


def test_preamble_is_untouched(tmp_path):
    """A list item above the frontmatter would stop this being a parseable OKF page."""
    m = _load("revq", REVERSE)
    text = HEADER + "- 2026-07-01 **a.md** — x\n- 2026-07-02 **b.md** — y\n"
    new, _ = m.reorder(text)
    assert new.startswith(HEADER), "frontmatter and heading must stay at the top"


def test_is_idempotent(tmp_path):
    """Safe to re-run, and a no-op on a vault created after the write-path change."""
    m = _load("revq", REVERSE)
    text = HEADER + "- 2026-07-02 **b.md** — y\n- 2026-07-01 **a.md** — x\n"
    new, moved = m.reorder(text)
    assert moved == 0 and new == text


def test_trailing_content_is_preserved_in_place(tmp_path):
    m = _load("revq", REVERSE)
    text = HEADER + "- 2026-07-01 **a.md** — x\n- 2026-07-02 **b.md** — y\n\n## Notes\n\nkeep me\n"
    new, _ = m.reorder(text)
    assert new.endswith("## Notes\n\nkeep me\n")
    assert new.count("keep me") == 1


def test_interleaved_non_row_lines_keep_their_position(tmp_path):
    """A live queue carries a stray `[END LOG]` marker and lines of model prose between real
    rows. Reversing the contiguous block would drag the marker to the top and scatter the
    prose; only dated rows may move."""
    m = _load("revq", REVERSE)
    text = (HEADER
            + "- 2026-07-01 **a.md** — x\n"
            + "- All 20 sources have: quality_score\n"
            + "- 2026-07-02 **b.md** — y\n"
            + "[END LOG]\n"
            + "- 2026-07-03 **c.md** — z\n")
    new, moved = m.reorder(text)
    assert moved == 3, "only the three DATED rows are permuted"
    out = new.splitlines()
    assert out[-2] == "[END LOG]", "the marker keeps its index"
    assert out[-4] == "- All 20 sources have: quality_score", "prose keeps its index"
    dated = [ln for ln in out if m._ROW.match(ln + "\n")]
    assert dated[0].startswith("- 2026-07-03") and dated[-1].startswith("- 2026-07-01")


def test_a_queue_with_no_rows_is_left_alone(tmp_path):
    m = _load("revq", REVERSE)
    new, moved = m.reorder(HEADER)
    assert moved == 0 and new == HEADER


def test_dry_run_writes_nothing(tmp_path):
    m = _load("revq", REVERSE)
    q = tmp_path / "wiki" / "_review-queue.md"
    q.parent.mkdir(parents=True)
    text = HEADER + "- 2026-07-01 **a.md** — x\n- 2026-07-02 **b.md** — y\n"
    q.write_text(text, encoding="utf-8")
    assert m.main(["--vault", str(tmp_path)]) == 0
    assert q.read_text(encoding="utf-8") == text
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert q.read_text(encoding="utf-8") != text


def test_split_rows_without_rows_and_cli_noop_paths(tmp_path, capsys):
    m = _load("revq_edges", REVERSE)
    assert m.split_rows("heading\n") == (["heading\n"], [], [])
    assert m.main(["--vault", str(tmp_path)]) == 2
    q = tmp_path / "wiki" / "_review-queue.md"
    q.parent.mkdir(parents=True)
    q.write_text(HEADER + "- 2026-07-02 **b**\n- prose\n- 2026-07-01 **a**\n")
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0
    assert "pinned" in capsys.readouterr().out
    assert m.main(["--vault", str(tmp_path), "--apply"]) == 0


# ------------------------------------------------------- the write-path prepend


def _write_server():
    pytest.importorskip("mcp")
    path = REPO / "okengine-mcp" / "write_server.py"
    return _load("write_server_q", path)


def test_write_path_inserts_below_the_preamble():
    ws = _write_server()
    out = ws._prepend_queue_row(HEADER + "- 2026-07-01 **a.md** — x\n",
                                "- 2026-07-02 **b.md** — y\n")
    assert out.startswith(HEADER)
    rows = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert rows[0].startswith("- 2026-07-02"), "newest row on top"
    assert rows[1].startswith("- 2026-07-01"), "existing rows keep their order"


def test_write_path_first_row_lands_after_the_header():
    ws = _write_server()
    out = ws._prepend_queue_row(ws.QUEUE_HEADER, "- 2026-07-02 **b.md** — y\n")
    assert out.startswith(ws.QUEUE_HEADER)
    assert out.rstrip().endswith("- 2026-07-02 **b.md** — y")
    assert out.count("# Review Queue") == 1


# ------------------------------------------------------------ the cockpit sort


def test_cockpit_orders_newest_first_without_inverting_priority():
    """The trap: `reverse=True` on the whole key would send `grounding` to the bottom."""
    pytest.importorskip("fastapi")
    app = _load("cockpit_q", REPO / "okengine-cockpit" / "app.py")
    priority = {"grounding": 0, "agent-draft": 3}
    rows = [
        {"subject": "old-grounding", "updated": "2026-01-01", "reasons": [{"code": "grounding"}]},
        {"subject": "new-grounding", "updated": "2026-07-01", "reasons": [{"code": "grounding"}]},
        {"subject": "new-draft", "updated": "2026-07-02", "reasons": [{"code": "agent-draft"}]},
    ]
    rows.sort(key=lambda row: (min((priority.get(str(r.get("code")), 9)
                                   for r in row["reasons"]), default=9),
                               app._desc(row["updated"]), row["subject"]))
    assert [r["subject"] for r in rows] == ["new-grounding", "old-grounding", "new-draft"], (
        "grounding must still outrank agent-draft, but newest first inside the band")
