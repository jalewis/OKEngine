"""cron-plus fall-back fold policy (``patches/cron-plus/dst_fold.py``; okengine#1055, #1061 [4]).

On 2026-11-01 America/New_York the 01:xx wall-clock hour happens twice: 05:xxZ (EDT), then 06:xxZ
(EST). The pinned cron-plus guard compared croniter's candidate against the claim time, so a daily
01:30 lane claimed a minute late fired again at 01:30 EST (#1055), and every hourly fixed-minute
lane lost its 01:MM EST fire (#1061 [4]). The policy decides from the candidate slot alone: an
explicit hour fires once, a star hour fires in both passes.

The acceptance set below runs the real croniter (pinned 6.2.4 in every CI lock) chained the way
cron-plus chains it: each next fire computed from the previous fire as ``last_run_at``. On the pin's
guard, cases A, B, C, E2, E3, F2 and G1 fail; every case passes with the policy.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "patches" / "cron-plus" / "dst_fold.py"
PATCH = REPO / "patches" / "cron-plus" / "dst-fold.patch"
NY = ZoneInfo("America/New_York")


def _mod():
    spec = importlib.util.spec_from_file_location("dst_fold", POLICY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def _chain(expr: str, start: str, n: int) -> list[str]:
    """``n`` successive fires, each computed from the previous one as cron-plus does."""
    m, base, out = _mod(), _utc(start), []
    for _ in range(n):
        base = m.next_fire(croniter(expr, base.astimezone(NY)), expr).astimezone(timezone.utc)
        out.append(base.strftime("%m-%dT%H:%MZ"))
    return out


ACCEPTANCE = [
    ("A hourly fixed minute keeps both 01:12", "12 * * * *", "2026-11-01T04:30", 3,
     ["11-01T05:12Z", "11-01T06:12Z", "11-01T07:12Z"]),
    ("B top of every hour from 01:00 EDT", "0 * * * *", "2026-11-01T05:00", 2,
     ["11-01T06:00Z", "11-01T07:00Z"]),
    ("C */1 hour keeps both 01:45", "45 */1 * * *", "2026-11-01T04:50", 3,
     ["11-01T05:45Z", "11-01T06:45Z", "11-01T07:45Z"]),
    ("D */30 minutes unchanged", "*/30 * * * *", "2026-11-01T04:50", 5,
     ["11-01T05:00Z", "11-01T05:30Z", "11-01T06:00Z", "11-01T06:30Z", "11-01T07:00Z"]),
    ("E1 daily 01:30 claimed on time", "30 1 * * *", "2026-11-01T05:30", 1, ["11-02T06:30Z"]),
    ("E2 daily 01:30 claimed a minute late", "30 1 * * *", "2026-11-01T05:31", 1, ["11-02T06:30Z"]),
    ("E3 daily 01:30 claimed 29 minutes late", "30 1 * * *", "2026-11-01T05:59", 1,
     ["11-02T06:30Z"]),
    ("E4 daily 01:30 from the day before", "30 1 * * *", "2026-10-31T05:30", 1, ["11-01T05:30Z"]),
    ("F1 every other hour claimed on time", "0 1-23/2 * * *", "2026-11-01T05:00", 1,
     ["11-01T08:00Z"]),
    ("F2 every other hour claimed late", "0 1-23/2 * * *", "2026-11-01T05:02", 1, ["11-01T08:00Z"]),
    ("G1 quarter-hours inside an explicit hour", "*/15 1 * * *", "2026-11-01T05:20", 3,
     ["11-01T05:30Z", "11-01T05:45Z", "11-02T06:00Z"]),
    ("G2 an 0-23 range is an explicit hour", "0 0-23 * * *", "2026-11-01T05:00", 1,
     ["11-01T07:00Z"]),
    ("H daily 07:00 across fall-back", "0 7 * * *", "2026-10-31T11:00", 1, ["11-01T12:00Z"]),
    ("S1 spring-forward daily 01:30", "30 1 * * *", "2027-03-13T06:30", 2,
     ["03-14T06:30Z", "03-15T05:30Z"]),
    ("S2 spring-forward hourly", "12 * * * *", "2027-03-14T06:00", 2,
     ["03-14T06:12Z", "03-14T07:12Z"]),
    ("S3 spring-forward daily 07:00", "0 7 * * *", "2027-03-13T12:00", 1, ["03-14T11:00Z"]),
]


@pytest.mark.parametrize("label, expr, start, n, want", ACCEPTANCE, ids=[c[0] for c in ACCEPTANCE])
def test_acceptance_set(label, expr, start, n, want):
    assert _chain(expr, start, n) == want, label


@pytest.mark.parametrize("expr, star", [
    ("12 * * * *", True), ("45 */1 * * *", True), ("0 */2 * * *", True), ("@hourly", True),
    ("@HOURLY", True), ("30 1 * * *", False), ("0 1-23/2 * * *", False), ("0 0-23 * * *", False),
    ("@daily", False), ("@midnight", False), ("", False), ("*", False), (None, False),
    ("0 *", True), ("0 1", False),   # the hour is read positionally, as the second field
])
def test_hour_field_classification(expr, star):
    assert _mod().hour_field_is_star(expr) is star, expr


@pytest.mark.parametrize("moment, repeated", [
    (_utc("2026-11-01T05:30").astimezone(NY), False),   # 01:30 EDT, first pass
    (_utc("2026-11-01T06:30").astimezone(NY), True),    # 01:30 EST, second pass
    (_utc("2026-11-01T07:30").astimezone(NY), False),   # 02:30 EST, happens once
    (_utc("2027-03-14T07:30").astimezone(NY), False),   # 03:30 EDT, after the spring gap
    (_utc("2026-11-01T06:30"), False),                  # UTC has no fold
    (datetime(2026, 11, 1, 1, 30), False),              # naive: no zone, no fold
    (datetime(2027, 3, 14, 2, 30, tzinfo=NY), False),   # spring gap, fold=0: never happens twice
    (datetime(2027, 3, 14, 2, 30, fold=1, tzinfo=NY), False),  # spring gap, fold=1
], ids=["first-pass", "second-pass", "after-fold", "spring", "utc", "naive", "gap", "gap-fold1"])
def test_repeated_wall_time(moment, repeated):
    assert _mod().is_repeated_wall_time(moment) is repeated


class _StuckInFold:
    """A croniter stand-in that only ever yields a second-pass wall time (a broken tz database)."""

    def __init__(self):
        self.calls = 0

    def get_next(self, ret_type):
        self.calls += 1
        return _utc("2026-11-01T06:30").astimezone(NY)


def test_a_runaway_skip_loop_raises_instead_of_hanging():
    m = _mod()
    it = _StuckInFold()
    with pytest.raises(RuntimeError, match="no slot outside a repeated wall-clock span"):
        m.next_fire(it, "30 1 * * *")
    assert it.calls == m.MAX_SKIPS + 1


def test_a_star_hour_returns_the_second_pass_without_skipping():
    it = _StuckInFold()
    # Compare in UTC: PEP 495 makes an inter-zone == False for any time in a repeated span.
    assert _mod().next_fire(it, "30 * * * *").astimezone(timezone.utc) == _utc("2026-11-01T06:30")
    assert it.calls == 1


def test_the_patch_replaces_the_claim_time_guard_with_the_policy():
    text = PATCH.read_text(encoding="utf-8")
    removed = [line[1:] for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert any("nxt_local.date() == base_local.date()" in line for line in removed), \
        "the pin's claim-time comparison must be removed"
    assert any("dst_fold.next_fire(it, expr)" in line for line in added)
    assert not any("base_local" in line for line in added), \
        "the new policy must not depend on the claim time"


def test_the_installer_applies_the_patch_and_ships_the_overlay():
    install = (REPO / "scripts" / "install-cron-plus.sh").read_text(encoding="utf-8")
    assert 'apply_carried_patch "$ENGINE_DIR/patches/cron-plus/dst-fold.patch"' in install
    assert 'cp "$ENGINE_DIR/patches/cron-plus/dst_fold.py" "$DEST/dst_fold.py"' in install
