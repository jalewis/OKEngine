from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from datetime import datetime as _dt0, timezone as _tz0

_EPOCH = _dt0(2026, 8, 17, 12, tzinfo=_tz0.utc)

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "projection_service_test", REPO / "okengine-projection/service.py")
S = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = S
SPEC.loader.exec_module(S)


def test_projection_schema_recreates_health_view_for_column_layout_upgrades():
    """PostgreSQL rejects CREATE OR REPLACE when an existing view's columns move."""
    sql = (REPO / "db/projection-schema.sql").read_text(encoding="utf-8")
    drop = sql.index("DROP VIEW IF EXISTS v_projection_health")
    create = sql.index("CREATE VIEW v_projection_health")
    assert drop < create
    assert "DROP TABLE" not in sql


def test_reproducibility_canary_fails_closed():
    with pytest.raises(RuntimeError, match="comparable=0"):
        S.require_reproducible(0, [], [])
    with pytest.raises(RuntimeError, match="2 row and 1 link"):
        S.require_reproducible(3, [1, 2], [1])
    S.require_reproducible(3, [], [])


def test_every_basic_health_alarm_can_fire():
    healthy = {"epoch": 2, "age_seconds": 10}
    latest = {"epoch": 2, "ok": True, "error": None, "finished_at": _EPOCH}
    with pytest.raises(RuntimeError, match="never completed"):
        S.require_basic_health(None, 20, latest, 1, 1)
    with pytest.raises(RuntimeError, match="is 21s old"):
        S.require_basic_health({**healthy, "age_seconds": 21}, 20, latest, 1, 1)
    with pytest.raises(RuntimeError, match="last projection epoch 3 failed"):
        S.require_basic_health(healthy, 20,
                               {"epoch": 3, "ok": False, "error": "boom",
                                "finished_at": _EPOCH}, 1, 1)
    with pytest.raises(RuntimeError, match="2 projected vs 3 eligible"):
        S.require_basic_health(healthy, 20, latest, 2, 3)
    S.require_basic_health(healthy, 20, latest, 3, 3)


def test_an_epoch_still_running_is_not_a_failed_epoch():
    """`ok` is NOT NULL DEFAULT false, so a row reads `ok=false` from the moment an epoch STARTS.

    Observed live seconds after a roll: `last projection epoch 159 failed: no error recorded` —
    on an epoch the database recorded as ok=true once it finished. The message said "no error
    recorded" out loud and nobody heard it: a run that failed has an error.
    """
    healthy = {"epoch": 2, "age_seconds": 10}
    in_flight = {"epoch": 3, "ok": False, "error": None, "finished_at": None}
    S.require_basic_health(healthy, 20, in_flight, 3, 3)


def test_a_finished_epoch_that_failed_without_an_error_still_alarms():
    """The other side: finished, not ok, and silent about why is a real failure, and the absence
    of a message must not become the reason to ignore it."""
    healthy = {"epoch": 2, "age_seconds": 10}
    dead = {"epoch": 3, "ok": False, "error": None, "finished_at": _EPOCH}
    with pytest.raises(RuntimeError, match="no error recorded"):
        S.require_basic_health(healthy, 20, dead, 3, 3)


def test_digest_alarm_can_fire():
    with pytest.raises(RuntimeError, match="comparable=0"):
        S.require_digest_health([], 0)
    with pytest.raises(RuntimeError, match="2 of 4 comparable"):
        S.require_digest_health(["a", "b"], 4)
    S.require_digest_health([], 4)


def test_projection_profile_refuses_shipped_credentials():
    for reader in ("", " ", "okengine-reader-local", "REPLACE_WITH_A_DIFFERENT_VALUE"):
        with pytest.raises(RuntimeError, match="READER_PASSWORD"):
            S.validate_secrets("postgresql://writer:strong@postgres/db", reader)
    for writer in ("", "%20", "okengine-projection-local", "REPLACE_BEFORE_ENABLING"):
        with pytest.raises(RuntimeError, match="WRITER_PASSWORD"):
            S.validate_secrets(f"postgresql://writer:{writer}@postgres/db", "different")
    with pytest.raises(RuntimeError, match="WRITER_PASSWORD"):
        S.validate_secrets("postgresql://writer:REPLACE%5FBEFORE%5FENABLING@postgres/db", "different")
    with pytest.raises(RuntimeError, match="WRITER_PASSWORD"):
        S.validate_secrets("not-a-dsn", "different")
    with pytest.raises(RuntimeError, match="WRITER_PASSWORD"):
        S.validate_secrets("postgresql://writer:strong@[invalid/db", "different")
    with pytest.raises(RuntimeError, match="distinct"):
        S.validate_secrets("postgresql://writer:same@postgres/db", "same")
    S.validate_secrets("postgresql://writer:strong@postgres/db", "different")


def test_count_drift_is_bounded_by_churn_not_by_equality():
    """`rows` is a count as of an EPOCH; `files` is the filesystem NOW.

    On a vault that ingests continuously those are never taken at the same instant, so
    strict equality reported drift for every file that merely arrived in between. Measured
    on a live 48,000-page vault: ±1 against a projection 40 minutes into a one-hour cycle,
    i.e. a FAIL on essentially every check, for a reason unrelated to correctness. A gate
    that fails routinely stops being read — which is the failure it exists to catch.
    """
    healthy = {"epoch": 2, "age_seconds": 10}
    latest = {"epoch": 2, "ok": True, "error": None}
    # one file arrived since the epoch: explained, must NOT fail
    S.require_basic_health(healthy, 20, latest, 48368, 48369, churn=1)
    # a reshelve leaves a stale row instead: the other direction, equally explained
    S.require_basic_health(healthy, 20, latest, 48369, 48368, churn=1)
    # drift beyond the churn is real divergence and still fails, and says how much
    with pytest.raises(RuntimeError, match="leaving 4 unexplained"):
        S.require_basic_health(healthy, 20, latest, 100, 105, churn=1)
    # with no churn at all the check is exactly as strict as it was before
    with pytest.raises(RuntimeError, match="2 projected vs 3 eligible"):
        S.require_basic_health(healthy, 20, latest, 2, 3, churn=0)


def test_churn_counts_only_pages_written_after_the_projection_finished():
    from datetime import datetime, timedelta, timezone
    finished = datetime(2026, 8, 16, 3, 0, tzinfo=timezone.utc)
    pages = [
        {"file_mtime": finished + timedelta(minutes=5)},   # written after -> cannot be in it
        {"file_mtime": finished - timedelta(minutes=5)},   # written before -> should be in it
        {"file_mtime": finished},                          # exactly at the boundary -> in it
        {"file_mtime": None},                              # unreadable mtime -> not counted
        {},                                                # absent key -> not counted
    ]
    assert S.churn_since(pages, finished) == 1


def test_churn_is_zero_when_the_projection_has_never_finished():
    """No epoch means no baseline: fall back to the strict comparison rather than to a
    tolerance nobody can justify."""
    assert S.churn_since([{"file_mtime": "anything"}], None) == 0


# --- a page deleted after the epoch is churn, not digest drift -----------------------------------
import hashlib as _hashlib
from datetime import datetime as _dt, timedelta as _td, timezone as _tz



def _page(tmp_path: Path, body: bytes, mtime: _dt) -> Path:
    path = tmp_path / "page.md"
    path.write_bytes(body)
    import os
    os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def test_a_page_deleted_after_the_epoch_is_vanished_not_a_mismatch(tmp_path):
    """412 of 48,968 rows on a live vault were pages a cleanup lane had removed within the hour.
    Every health check reported `digest drift ... (FileNotFoundError)` — for rows where nothing
    had been compared at all."""
    assert S.classify_sampled_page(tmp_path / "gone.md", "abc", _EPOCH) == ("vanished", None)


def test_a_present_but_unreadable_page_is_still_a_mismatch(tmp_path, monkeypatch):
    """A permissions or I/O error is not a deletion; waving it through would hide the one case
    where the file IS there and cannot be trusted."""
    path = _page(tmp_path, b"body", _EPOCH - _td(hours=1))

    def boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    verdict, detail = S.classify_sampled_page(path, "abc", _EPOCH)
    assert verdict == "mismatch" and detail == "PermissionError"


def test_a_matching_digest_on_an_older_file_is_comparable(tmp_path):
    body = b"hello"
    path = _page(tmp_path, body, _EPOCH - _td(hours=1))
    digest = _hashlib.sha256(body).hexdigest()
    assert S.classify_sampled_page(path, digest, _EPOCH) == ("comparable", None)


def test_a_differing_digest_on_an_older_file_is_a_mismatch(tmp_path):
    path = _page(tmp_path, b"changed", _EPOCH - _td(hours=1))
    assert S.classify_sampled_page(path, "not-the-digest", _EPOCH) == ("mismatch", None)


def test_a_file_written_after_the_epoch_proves_nothing_either_way(tmp_path):
    """Pre-existing skip, kept: its digest is EXPECTED to differ, so it is neither evidence of
    health nor of drift."""
    path = _page(tmp_path, b"rewritten", _EPOCH + _td(minutes=5))
    assert S.classify_sampled_page(path, "stale-digest", _EPOCH) == ("newer", None)


def test_vanished_pages_do_not_trip_the_digest_alarm():
    """The end the refactor serves: whatever the sampler saw, only real mismatches fail."""
    with pytest.raises(RuntimeError, match="comparable=0"):
        S.require_digest_health([], 0)
    with pytest.raises(RuntimeError, match="1 of 3 comparable"):
        S.require_digest_health(["real-drift"], 3)
