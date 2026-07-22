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


def test_malformed_spec_warns_and_never_defers(monkeypatch, capsys):  # invariant-audit #19
    """A non-empty but unparseable spec (wrong separator / HH:MM / en-dash) used to silently return
    False for every hour — bulk drains ran at full peak price, the opposite of intent. Now it WARNs
    and still doesn't defer (fail loud, not silent)."""
    monkeypatch.setenv("CRON_DEFER_UTC_HOURS", "01:00-04:00;06:00-10:00")   # HH:MM + ';' separator
    for h in range(24):
        assert offpeak.offpeak_defer(datetime(2026, 7, 1, h, tzinfo=timezone.utc)) is False
    assert "no valid" in capsys.readouterr().err
    # a VALID spec still parses and defers (no spurious warning)
    monkeypatch.setenv("CRON_DEFER_UTC_HOURS", "1-4,6-10")
    assert offpeak.offpeak_defer(datetime(2026, 7, 1, 2, tzinfo=timezone.utc)) is True


def test_degenerate_equal_range_never_defers(monkeypatch, capsys):  # invariant-audit #351
    """`9-9` (a==b) is EMPTY in in_defer_window (neither the a<b nor a>b branch fires), so a spec of
    ONLY '9-9' would validate yet silently never defer — the silently-never-defers class this guard
    exists to catch. _spec_has_valid_window must reject it so offpeak_defer warns and defers nothing."""
    assert offpeak._spec_has_valid_window("9-9") is False
    monkeypatch.setenv("CRON_DEFER_UTC_HOURS", "9-9")
    for h in range(24):
        assert offpeak.offpeak_defer(datetime(2026, 7, 1, h, tzinfo=timezone.utc)) is False, h
    assert "no valid" in capsys.readouterr().err
    # a MIX of a degenerate part + a real window still honors the real part (no false warning)
    assert offpeak._spec_has_valid_window("9-9,1-4") is True
