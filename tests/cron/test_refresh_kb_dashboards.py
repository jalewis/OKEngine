"""Regression: the recent-ingest dashboard derives a source's ingest date with a fallback chain
(ingested -> last_updated -> updated -> published), not only `ingested` — else it renders an
all-empty board even as sources stream in, because sources carry `last_updated`/`published`,
not `ingested`."""
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("yaml")
CRON = Path(__file__).resolve().parents[2] / "scripts" / "cron"


def _load():
    sys.path.insert(0, str(CRON))
    spec = importlib.util.spec_from_file_location("refresh_kb_dashboards", CRON / "refresh_kb_dashboards.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_ingest_date_fallback_chain():
    m = _load()
    assert m._ingest_date({"ingested": "2026-06-01"}) == date(2026, 6, 1)
    assert m._ingest_date({"last_updated": "2026-06-21"}) == date(2026, 6, 21)          # no ingested
    assert m._ingest_date({"created": "2026-06-10", "last_updated": "2026-06-21"}) == date(2026, 6, 10)  # created beats last_updated
    assert m._ingest_date({"published": "2026-06-04T12:05:31+00:00"}) == date(2026, 6, 4)  # only published
    assert m._ingest_date({}) is None
    # precedence: explicit ingested wins over last_updated
    assert m._ingest_date({"ingested": "2026-06-01", "last_updated": "2026-06-21"}) == date(2026, 6, 1)
