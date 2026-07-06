"""offpeak_defer — bulk-drain deferral during a configured peak UTC window."""
import pathlib, sys
from datetime import datetime, timezone
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cron"))
import offpeak


def test_unset_env_never_defers(monkeypatch):
    monkeypatch.delenv("CRON_DEFER_UTC_HOURS", raising=False)
    assert offpeak.offpeak_defer(datetime(2026, 7, 1, 2, tzinfo=timezone.utc)) is False


def test_deepseek_peak_window(monkeypatch):
    monkeypatch.setenv("CRON_DEFER_UTC_HOURS", "1-4,6-10")
    for h in (1, 2, 3, 6, 7, 8, 9):          # peak UTC -> defer
        assert offpeak.offpeak_defer(datetime(2026, 7, 1, h, tzinfo=timezone.utc)) is True, h
    for h in (0, 4, 5, 10, 11, 17, 23):      # off-peak UTC -> run
        assert offpeak.offpeak_defer(datetime(2026, 7, 1, h, tzinfo=timezone.utc)) is False, h


def test_half_open_boundaries():
    assert offpeak.in_defer_window(3, "1-4") is True     # 03:xx peak
    assert offpeak.in_defer_window(4, "1-4") is False    # 04:00 off-peak (half-open)
    assert offpeak.in_defer_window(10, "6-10") is False  # 10:00 off-peak


def test_wraparound_window_defers_overnight():  # invariant-audit #24
    """a>b spells an OVERNIGHT window [a,24)U[0,b). Without wrap handling `20-6` is unsatisfiable
    for every hour and silently NEVER defers — bulk drains run at full peak, the opposite of intent."""
    assert offpeak.in_defer_window(22, "20-6") is True   # evening peak
    assert offpeak.in_defer_window(3, "20-6") is True    # early-morning peak
    assert offpeak.in_defer_window(0, "20-6") is True    # midnight (inside the wrap)
    assert offpeak.in_defer_window(20, "20-6") is True   # lower bound (inclusive)
    assert offpeak.in_defer_window(6, "20-6") is False   # upper bound (half-open, exclusive)
    assert offpeak.in_defer_window(10, "20-6") is False  # daytime off-peak
    # a mixed spec (wrap + normal part) still works
    assert offpeak.in_defer_window(13, "22-2,12-14") is True
