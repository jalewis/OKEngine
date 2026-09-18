"""Freeing a canonical seat held by a redundant tombstone (okengine#520 follow-up).

`okf_migrate` refuses to move a live page onto an occupied path, so a retired duplicate sitting
in the survivor's canonical seat holds that survivor off-shard forever. Freeing it means
removing a page, which the mover must never do — so it lives here, in the dedup half.

The tests that matter are the REFUSALS. This is the only tool in the set that deletes, so
"redundant" has to be proven every time: the occupant's `superseded_by` must designate the very
page whose seat it holds.

That designation is matched as a PATH as well as an id, and the path form is the important one:
`dedup_sources_by_url` writes `survivor_rel` and `write_server._tombstone` sanitises the value
with `_safe()`, so a path is what the engine actually stores. 15,978 of 16,061 values on one
live vault are path-shaped. The first version of this compared only against the mover's `id`,
matched the 83-value minority, and misfiled two genuinely redundant seats as unresolvable —
which is why both forms are pinned below.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "scripts" / "clear_redundant_tombstones.py"


def _load():
    if str(TOOL.parent) not in sys.path:
        sys.path.insert(0, str(TOOL.parent))
    spec = importlib.util.spec_from_file_location("clear_tomb", TOOL)
    m = importlib.util.module_from_spec(spec)
    sys.modules["clear_tomb"] = m
    spec.loader.exec_module(m)
    m.okf_migrate._SCHEMA_CACHE.clear()
    return m


def _vault(tmp_path: Path) -> Path:
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    (tmp_path / "schema.yaml").write_text(
        "types:\n  source: {required: [type]}\n"
        "partitioning:\n  namespaces:\n"
        "    sources: {strategy: by-date, date_field: published}\n", encoding="utf-8")
    return tmp_path


def _page(vault, rel, *, pid, published="2026-05-04", status=None, superseded_by=None):
    p = vault / "wiki" / (rel + ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: source", f"id: {pid}", f"published: {published}"]
    if status:
        lines.append(f"status: {status}")
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")
    lines += ["---", "Body.", ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _pair(vault, *, superseded_by="sources:survivor", status="tombstoned", mover_id="sources:survivor"):
    """A live page off-shard whose canonical seat (sources/2026/05/story) is occupied."""
    _page(vault, "sources/2026/07/06/market/story", pid=mover_id)
    return _page(vault, "sources/2026/05/story", pid="sources:dup",
                 status=status, superseded_by=superseded_by)


def test_a_redundant_seat_is_freed_and_archived(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v)
    archive = tmp_path / "arch"
    assert m.main(["--vault", str(v), "--apply", "--archive", str(archive)]) == 0
    assert not seat.exists(), "the seat must be freed for the survivor"
    copies = list(archive.glob("*.md"))
    assert len(copies) == 1, "removal must be reversible — archive before unlink"
    assert "sources:dup" in copies[0].read_text(encoding="utf-8")


def test_removal_is_logged(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    _pair(v)
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    log = (v / "wiki" / "log.md").read_text(encoding="utf-8")
    assert "clear-redundant-tombstone" in log and "sources/2026/05/story" in log


def test_dry_run_removes_nothing(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v)
    assert m.main(["--vault", str(v)]) == 0
    assert seat.exists()


# ------------------------------------------------------------------ refusals


def test_a_tombstone_pointing_elsewhere_is_kept(tmp_path):
    """That redirect is still load-bearing for some other page."""
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v, superseded_by="sources:some-third-page")
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert seat.exists()


def test_a_path_designating_the_mover_is_redundant(tmp_path):
    """The engine's OWN convention: `superseded_by` holds the survivor's path, not its id.
    Treating a path as unresolvable left two genuinely free seats blocked."""
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v, superseded_by="sources/2026/07/06/market/story")
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert not seat.exists(), "a path pointing at the mover proves the redirect is superfluous"


def test_a_path_designating_a_different_page_is_refused(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v, superseded_by="sources/frontier/2026/some-other-story")
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert seat.exists()


def test_a_live_occupant_is_never_removed(tmp_path):
    """Two live pages wanting one seat is a genuine duplicate slug, not a redundancy."""
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v, status=None, superseded_by=None)
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert seat.exists()


def test_empty_never_matches_empty(tmp_path):
    """A mover with no id and a tombstone with no pointer must not compare equal."""
    m = _load()
    v = _vault(tmp_path)
    p = _page(v, "sources/2026/07/06/market/story", pid="sources:x")
    p.write_text(p.read_text().replace("id: sources:x\n", ""), encoding="utf-8")
    seat = _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned")
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert seat.exists()


def test_both_designation_forms_are_accepted(tmp_path):
    """The corpus carries id-shaped and path-shaped values and neither is schema-defined, so
    the tool tolerates what is there rather than asserting a convention."""
    m = _load()
    for form in ("sources:survivor", "sources/2026/07/06/market/story"):
        v = _vault(tmp_path / form.replace("/", "_").replace(":", "_"))
        seat = _pair(v, superseded_by=form)
        assert m.main(["--vault", str(v), "--apply",
                       "--archive", str(v / "arch")]) == 0
        assert not seat.exists(), f"{form!r} designates the mover and must free the seat"


def test_a_tombstoned_mover_does_not_claim_a_seat(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    _page(v, "sources/2026/07/06/market/story", pid="sources:survivor", status="tombstoned")
    seat = _page(v, "sources/2026/05/story", pid="sources:dup", status="tombstoned",
                 superseded_by="sources:survivor")
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert seat.exists()


def test_plan_reports_why_each_was_skipped(tmp_path):
    m = _load()
    v = _vault(tmp_path)
    _pair(v, superseded_by="sources:elsewhere")
    _removable, skipped = m.plan(v, "sources")
    assert skipped["points_elsewhere"] == 1


def test_failed_recheck_is_counted(tmp_path, monkeypatch):
    m = _load()
    v = _vault(tmp_path)
    monkeypatch.setattr(m.okf_migrate, "build_map", lambda *_: ([], [("mover", "seat")]))
    monkeypatch.setattr(m.okf_migrate, "classify_collisions", lambda *_: {
        "blocked_by_tombstone": [], "live_conflict": [],
        "redundant_tombstone": [("mover", "seat")],
    })
    monkeypatch.setattr(m, "_fm", lambda *_: {})
    removable, skipped = m.plan(v, "sources")
    assert removable == [] and skipped["failed_recheck"] == 1


def test_cli_refusals_long_listing_and_io_errors(tmp_path, monkeypatch, capsys):
    m = _load()
    assert m.main(["--vault", str(tmp_path)]) == 2
    v = _vault(tmp_path / "v")
    seats = [(f"sources/seat-{i}", f"sources/mover-{i}") for i in range(11)]
    monkeypatch.setattr(m, "plan", lambda *_: (seats, {
        "points_elsewhere": 0, "live_conflict": 0, "failed_recheck": 0}))
    assert m.main(["--vault", str(v)]) == 0
    assert "and 1 more" in capsys.readouterr().out
    monkeypatch.setattr(m.shutil, "copy2", lambda *_: (_ for _ in ()).throw(OSError("no")))
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0


def test_log_failure_does_not_undo_a_safe_removal(tmp_path, monkeypatch):
    m = _load()
    v = _vault(tmp_path)
    seat = _pair(v)
    real_open = Path.open

    def open_or_fail(path, *args, **kwargs):
        if path.name == "log.md":
            raise OSError("read only")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_or_fail)
    assert m.main(["--vault", str(v), "--apply", "--archive", str(tmp_path / "a")]) == 0
    assert not seat.exists()
