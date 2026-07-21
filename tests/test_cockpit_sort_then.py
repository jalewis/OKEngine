"""Cockpit _ds_sorted secondary sort (`then:`) — the tie-break behind the actor "Recently active"
board.

Sorting the roster by `last_seen` alone buckets 50-70 actors onto each coarse annual-report date
(YYYY-01-01), so a single-field sort leaves the top of the board in arbitrary glob order. `then:
recent_reports` breaks those ties by activity (same direction), so the most-mentioned actor of a
given date leads. Backward-compat: no `then:` must preserve the prior stable-within-tie order.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("yaml")

APP = Path(__file__).resolve().parent.parent / "okengine-cockpit" / "app.py"


def _mod(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path))
    sys.path.insert(0, str(APP.parent))
    sys.modules.pop("cockpit_app", None)
    spec = importlib.util.spec_from_file_location("cockpit_app", APP)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cockpit_app"] = m
    spec.loader.exec_module(m)
    return m


def test_then_breaks_date_ties_by_activity(monkeypatch, tmp_path):
    m = _mod(monkeypatch, tmp_path)
    # three actors share 2026-01-01 (a coarse annual-report date), one is newer, one older.
    rows = [
        {"title": "low-2026", "last_seen": "2026-01-01", "recent_reports": 2},
        {"title": "high-2026", "last_seen": "2026-01-01", "recent_reports": 30},
        {"title": "mid-2026", "last_seen": "2026-01-01", "recent_reports": 9},
        {"title": "newest", "last_seen": "2026-07-08", "recent_reports": 1},
        {"title": "old", "last_seen": "2024-01-01", "recent_reports": 99},
    ]
    out = [r["title"] for r in m._ds_sorted(
        rows, {"field": "last_seen", "desc": True, "then": "recent_reports"})]
    # primary: newest date first; the 99-volume 'old' actor stays LAST despite huge volume (recency wins)
    assert out[0] == "newest" and out[-1] == "old", out
    # within the 2026-01-01 tie: activity desc
    assert out[1:4] == ["high-2026", "mid-2026", "low-2026"], out


def test_no_then_preserves_stable_order(monkeypatch, tmp_path):
    m = _mod(monkeypatch, tmp_path)
    rows = [{"title": f"a{i}", "last_seen": "2026-01-01", "recent_reports": i} for i in range(5)]
    # no `then` -> ties keep input order (stable), same as before this feature existed
    out = [r["title"] for r in m._ds_sorted(rows, {"field": "last_seen", "desc": True})]
    assert out == ["a0", "a1", "a2", "a3", "a4"], out


def test_then_missing_secondary_sinks_within_tie(monkeypatch, tmp_path):
    m = _mod(monkeypatch, tmp_path)
    rows = [
        {"title": "has", "last_seen": "2026-01-01", "recent_reports": 5},
        {"title": "missing", "last_seen": "2026-01-01"},          # no recent_reports -> -inf, sinks
    ]
    out = [r["title"] for r in m._ds_sorted(
        rows, {"field": "last_seen", "desc": True, "then": "recent_reports"})]
    assert out == ["has", "missing"], out


def test_then_breaks_batch_tie_by_rfc3339_timestamp(monkeypatch, tmp_path):
    m = _mod(monkeypatch, tmp_path)
    rows = [
        {"title": "older", "last_updated": "2026-07-17T16:07:39Z",
         "as_of": "2026-07-16T04:54:19Z"},
        {"title": "newer", "last_updated": "2026-07-17T16:07:39Z",
         "as_of": "2026-07-17T05:08:40Z"},
    ]
    out = [r["title"] for r in m._ds_sorted(
        rows, {"field": "last_updated", "desc": True, "then": "as_of"})]
    assert out == ["newer", "older"], out
