"""Cross-file contract: health_export must parse the fleet-lane counts that fleet_health WRITES.

fleet_health.py emits the counts as PLAIN text ('🔴 stale: 0'). health_export shipped a bold-only
regex ('stale: \\*\\*(\\d+)\\*\\*') that matched nothing, so every fleet Prometheus metric read 0
regardless of real lane state (a dead-monitor-reads-healthy fail). This pins the producer/consumer
format so the two halves cannot silently drift again.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HE = REPO / "scripts" / "cron" / "health_export.py"
FH = REPO / "scripts" / "cron" / "fleet_health.py"

pytestmark = pytest.mark.skipif(not HE.is_file(), reason="script absent")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


he = _load(HE, "health_export")

# the exact plain summary line fleet_health.py emits (matches the live dashboard)
PLAIN = "- 🟢 ok: 88  ·  🔴 stale: 0  ·  🔴 errored: 1  ·  🔴 off-model: 2  ·  🟡 never-run: 0\n"


def test_parse_plain_counts():
    # regression: the shipped bold-only regex parsed all of these as None -> exported 0
    assert he._parse_fleet(PLAIN) == {"stale": 0, "errored": 1, "off-model": 2, "ok": 88}


def test_old_bold_only_regex_missed_the_plain_line():
    assert re.search(r"stale: \*\*(\d+)\*\*", PLAIN) is None   # why the metrics read 0


def test_parse_tolerates_bold_too():
    bold = "🟢 ok: **88**  ·  🔴 stale: **3**  ·  🔴 errored: **1**  ·  🔴 off-model: **0**\n"
    assert he._parse_fleet(bold) == {"stale": 3, "errored": 1, "off-model": 0, "ok": 88}


def test_producer_still_emits_plain_counts():
    """If fleet_health switches the counts to bold, `label: {counts[...]}` stops being a substring
    and this fails — forcing both sides back into sync instead of silently zeroing the metrics."""
    if not FH.is_file():
        pytest.skip("fleet_health.py absent")
    src = FH.read_text(encoding="utf-8")
    for frag in ("ok: {counts['ok']}", "stale: {counts['stale']}",
                 "errored: {counts['errored']}", "off-model: {counts['off-model']}"):
        assert frag in src, f"producer format drifted: {frag!r} not in fleet_health.py"


def test_fleet_health_emits_lane_identity_sidecar(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs.json"
    logs = tmp_path / "logs"
    logs.mkdir()
    wiki = tmp_path / "wiki"
    jobs.write_text(json.dumps({"jobs": [
        {"name": "lane-error", "enabled": True, "schedule": {"expr": "0 * * * *"}},
        {"name": "lane-ok", "enabled": True, "schedule": {"expr": "0 * * * *"}},
    ]}))
    (logs / "lane-error-1.log").write_text("ERROR failed\n")
    (logs / "lane-ok-1.log").write_text("completed\n")

    fh = _load(FH, "fleet_health_sidecar")
    monkeypatch.setattr(fh, "WIKI", wiki)
    monkeypatch.setattr(fh, "JOBS", jobs)
    monkeypatch.setattr(fh, "LOGS", logs)
    monkeypatch.setattr(fh, "_interval_s", lambda _expr: None)

    assert fh.main() == 0
    sidecar = json.loads((wiki / "dashboards" / ".fleet-lanes.json").read_text())
    assert sidecar["errored"] == ["lane-error"]
    assert sidecar["ok"] == ["lane-ok"]
