"""Regression: search-layer health must be visible, and 'unmeasured' must never read as healthy.

okengine#410 fixed search saturation with a 2-slot admission gate and recommended "expose
timeout/saturation distinctly in fleet health". That half never shipped, so the conditions that
would justify revisiting the search layer — rising p95, routine saturation — were unobservable
(okengine#568). A cap you cannot see hitting is indistinguishable from a cap you never reach.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "fleet_health.py"
pytestmark = pytest.mark.skipif(not MOD.is_file(), reason="fleet_health absent")
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _load():
    sys.path.insert(0, str(MOD.parent))
    spec = importlib.util.spec_from_file_location("fleet_health", MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_health"] = mod
    spec.loader.exec_module(mod)
    return mod


def _snap(tmp_path, monkeypatch, *, search=None, maintenance=None, **fields):
    mod = _load()
    path = tmp_path / "search-telemetry.json"
    body = {"updated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "concurrency_limit": 2, "saturated": 0,
            "search": {"calls": 10, "ok": 10, "timeouts": 0, "p95_ms": 900},
            "maintenance": {"calls": 2, "ok": 2, "timeouts": 0, "p95_ms": 7600}}
    if search is not None:
        body["search"].update(search)
    if maintenance is not None:
        body["maintenance"].update(maintenance)
    body.update(fields)
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(mod, "QMD_STATS_PATH", path)
    return mod


def test_absent_telemetry_is_unknown_never_ok(tmp_path, monkeypatch):
    """Reporting an unmeasured search layer as green recreates the blind spot this removes."""
    mod = _load()
    monkeypatch.setattr(mod, "QMD_STATS_PATH", tmp_path / "nope.json")
    state, detail = mod.qmd_search_health(NOW)
    assert state == "unknown"
    assert "UNMEASURED, not healthy" in detail


def test_a_healthy_snapshot_reads_ok(tmp_path, monkeypatch):
    mod = _snap(tmp_path, monkeypatch)
    assert mod.qmd_search_health(NOW)[0] == "ok"


def test_any_saturation_warns(tmp_path, monkeypatch):
    """A saturated call is a lane that did not get its answer — not merely a slow one."""
    mod = _snap(tmp_path, monkeypatch, saturated=1)
    state, detail = mod.qmd_search_health(NOW)
    assert state == "warn"
    assert "REFUSED" in detail


def test_a_timeout_warns_even_with_fast_p95(tmp_path, monkeypatch):
    mod = _snap(tmp_path, monkeypatch, search={"timeouts": 1, "p95_ms": 50})
    assert mod.qmd_search_health(NOW)[0] == "warn"


def test_slow_p95_warns(tmp_path, monkeypatch):
    mod = _snap(tmp_path, monkeypatch, search={"p95_ms": 9000})
    state, detail = mod.qmd_search_health(NOW)
    assert state == "warn"
    assert "p95" in detail


def test_stale_telemetry_is_annotated_not_treated_as_a_fault(tmp_path, monkeypatch):
    """A quiet vault issues no searches. Inferring a fault from silence is the error this codebase
    keeps paying for — say the data is old, do not invent a verdict from it."""
    old = (NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mod = _snap(tmp_path, monkeypatch, updated=old)
    state, detail = mod.qmd_search_health(NOW)
    assert state == "ok"
    assert "48h old" in detail


def test_malformed_telemetry_is_unknown_not_ok(tmp_path, monkeypatch):
    mod = _load()
    path = tmp_path / "search-telemetry.json"
    path.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(mod, "QMD_STATS_PATH", path)
    assert mod.qmd_search_health(NOW)[0] == "unknown"


def test_slow_maintenance_does_not_warn_about_search(tmp_path, monkeypatch):
    """The first live reading of this metric was a false alarm: a 7.6s index refresh rendered as
    "search p95 over 5000ms" on a vault whose searches run in ~0.5s. They share the two slots, so
    saturation is one number, but their LATENCIES are different populations and pooling them
    produces a metric that lies."""
    mod = _snap(tmp_path, monkeypatch, search={"p95_ms": 500}, maintenance={"p95_ms": 20000})
    state, detail = mod.qmd_search_health(NOW)
    assert state == "ok", detail
    assert "maintenance" in detail


def test_maintenance_only_telemetry_is_not_a_fault(tmp_path, monkeypatch):
    """No searches yet means nothing searched — not that search is broken."""
    mod = _snap(tmp_path, monkeypatch, search={"calls": 0, "ok": 0, "p95_ms": None})
    state, detail = mod.qmd_search_health(NOW)
    assert state == "ok"
    assert "no searches yet" in detail


@pytest.mark.parametrize("field", ["errors", "timeouts"])
def test_failed_index_maintenance_warns_even_when_search_stays_fast(
        tmp_path, monkeypatch, field):
    mod = _snap(tmp_path, monkeypatch, search={"calls": 3, "p95_ms": 25},
                maintenance={field: 2})
    state, detail = mod.qmd_search_health(NOW)
    assert state == "warn"
    assert "index maintenance FAILED" in detail
    assert "stale last-known-good index" in detail


def test_pre_split_telemetry_is_unknown_not_ok(tmp_path, monkeypatch):
    """An older read-MCP publishes the flat shape. Reading its numbers as if they were search
    numbers is exactly the mistake being fixed, so it reports UNMEASURED instead."""
    mod = _load()
    path = tmp_path / "search-telemetry.json"
    path.write_text(json.dumps({"updated": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "calls": 4, "p95_ms": 7299, "saturated": 0}), encoding="utf-8")
    monkeypatch.setattr(mod, "QMD_STATS_PATH", path)
    state, detail = mod.qmd_search_health(NOW)
    assert state == "unknown"
    assert "predates" in detail


def test_telemetry_that_is_not_an_object_is_unknown(tmp_path, monkeypatch):
    """A JSON document that parses but is not a mapping carries no readable fields. Treating that as
    a healthy read is the same class of error as treating an absent file as healthy."""
    mod = _load()
    path = tmp_path / "search-telemetry.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    monkeypatch.setattr(mod, "QMD_STATS_PATH", path)
    status, note = mod.qmd_search_health(NOW)
    assert status == "unknown"
    assert "not an object" in note


def test_telemetry_with_an_unreadable_timestamp_still_reports_its_measurements(tmp_path, monkeypatch):
    """The stamp qualifies the reading; it is not the reading. Discarding a whole snapshot because
    its timestamp is malformed would throw away the p95 and saturation counts that are the point —
    but the caveat has to be visible, so nobody reads a possibly-ancient sample as "now"."""
    mod = _snap(tmp_path, monkeypatch, updated="not-a-timestamp")
    status, note = mod.qmd_search_health(NOW)
    assert status == "ok"
    assert "no readable timestamp" in note
