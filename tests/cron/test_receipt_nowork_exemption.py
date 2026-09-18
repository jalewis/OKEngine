"""The wakeAgent=false no-work exemption must not be defeated by a stale manifest (#479).

`_verify_receipt` exempts a clean no-work run from owing a completion receipt.
The original test was ``not Path(manifest).is_file()`` -- but selection manifests
are never removed on the no-work path, so a file left by an ANY earlier run made
the exemption unreachable forever after, and every subsequent wakeAgent=false run
of an enforce-mode `per-selected-item` lane was failed with
"missing okengine-receipt JSON block" despite no agent having run.

The guard lives inside `run-receipts.patch` (it patches cron-plus's runner.py, which
has no checked-in copy here), so the function is extracted from the patch and
exercised directly -- this tests the shipped bytes, not a paraphrase.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PATCH = REPO / "patches" / "cron-plus" / "run-receipts.patch"


def _added_lines() -> list[str]:
    """The lines this patch ADDS to runner.py, with the diff marker stripped."""
    return [ln[1:] for ln in PATCH.read_text().splitlines()
            if ln.startswith("+") and not ln.startswith("+++")]


def _selected_this_run():
    """exec the patch's `_selected_this_run` in an isolated namespace."""
    added = _added_lines()
    start = next(i for i, ln in enumerate(added) if ln.startswith("def _selected_this_run("))
    end = next(i for i, ln in enumerate(added[start + 1:], start + 1)
               if ln.startswith("def "))
    ns: dict = {"Path": Path, "datetime": datetime, "Optional": None}
    exec("\n".join(added[start:end]), ns)
    return ns["_selected_this_run"]


def _manifest(tmp_path: Path, *, age_seconds: float) -> tuple[dict, datetime]:
    """A manifest whose mtime sits `age_seconds` BEFORE the run start."""
    started_at = datetime.now(timezone.utc)
    path = tmp_path / "lane.json"
    path.write_text('{"selected": ["a", "b"]}')
    import os
    stamp = started_at.timestamp() - age_seconds
    os.utime(path, (stamp, stamp))
    return {"selection_manifest": str(path)}, started_at


def test_stale_manifest_does_not_defeat_the_exemption(tmp_path):
    """#479: a manifest from a PREVIOUS run means this run selected nothing."""
    job, started_at = _manifest(tmp_path, age_seconds=86_400)     # a day old
    assert Path(job["selection_manifest"]).is_file(), "precondition: file present"
    assert _selected_this_run()(job, started_at) is False


def test_manifest_written_by_this_run_still_owes_a_receipt(tmp_path):
    """The other direction: real selected work must NOT be exempted."""
    job, started_at = _manifest(tmp_path, age_seconds=-5)         # written after start
    assert _selected_this_run()(job, started_at) is True


def test_manifest_at_exactly_the_run_start_counts_as_selected(tmp_path):
    job, started_at = _manifest(tmp_path, age_seconds=0)
    assert _selected_this_run()(job, started_at) is True


def test_fresh_empty_manifest_is_no_work(tmp_path):
    """Selectors refresh an explicit empty manifest before returning wake=false."""
    job, started_at = _manifest(tmp_path, age_seconds=-5)
    Path(job["selection_manifest"]).write_text('{"selected": []}')
    assert _selected_this_run()(job, started_at) is False


def test_fresh_malformed_manifest_fails_closed(tmp_path):
    job, started_at = _manifest(tmp_path, age_seconds=-5)
    path = Path(job["selection_manifest"])
    path.write_text('{"selected":')
    # Some CI filesystems expose coarse mtime resolution. Preserve the test's
    # explicit "written by this run" precondition after replacing the content.
    import os
    stamp = started_at.timestamp() + 5
    os.utime(path, (stamp, stamp))
    assert _selected_this_run()(job, started_at) is True


@pytest.mark.parametrize("job", [{}, {"selection_manifest": ""},
                                 {"selection_manifest": "/nonexistent/lane.json"}])
def test_absent_manifest_is_no_work(job):
    assert _selected_this_run()(job, datetime.now(timezone.utc)) is False


def test_unreadable_manifest_fails_closed_into_verification(tmp_path, monkeypatch):
    """An OSError must not silently exempt a lane from its receipt."""
    job, started_at = _manifest(tmp_path, age_seconds=86_400)

    def boom(self, *a, **kw):
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "stat", boom)
    assert _selected_this_run()(job, started_at) is True


def test_guard_no_longer_keys_on_mere_file_existence():
    """Pin the defect itself: existence alone must not gate the exemption."""
    added = "\n".join(_added_lines())
    assert "not _selected_this_run(job, started_at)" in added, \
        "the no-work exemption must test what THIS run selected"
    assert "not Path(manifest).is_file()" not in added, \
        "#479 regression: exemption gated on file existence, which stale manifests defeat"


def test_started_at_is_threaded_into_verification():
    """The run-start timestamp must reach _verify_receipt from main()."""
    added = "\n".join(_added_lines())
    assert "def _verify_receipt(job: dict, response: str, started_at: datetime)" in added
    assert "_verify_receipt(job, response, started_at)" in added
