"""Unit tests for tz_lib — deployment-timezone-aware date stamping (okengine#301)."""
import importlib.util
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

MOD = Path(__file__).resolve().parents[2] / "scripts" / "cron" / "tz_lib.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="tz_lib absent")


def _load():
    spec = importlib.util.spec_from_file_location("tz_lib", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TZ = _load()


def test_unset_defaults_to_utc(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    assert TZ.deployment_tz() is timezone.utc


def test_literal_utc_is_utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    assert TZ.deployment_tz() is timezone.utc


def test_real_zone_resolves(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    assert TZ.deployment_tz() == ZoneInfo("America/New_York")


def test_bad_zone_falls_back_to_utc_never_raises(monkeypatch):
    monkeypatch.setenv("TZ", "Not/ARealZone")
    assert TZ.deployment_tz() is timezone.utc  # must not raise


def test_today_and_now_agree_on_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    now = TZ.deployment_now()
    assert now.tzinfo == ZoneInfo("America/New_York")
    assert TZ.deployment_today() == now.date()
