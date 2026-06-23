"""Unit tests for scripts/cron_jitter.py — per-install schedule jitter."""
import importlib.util
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MOD = REPO / "scripts" / "cron_jitter.py"


def _load():
    sys.modules.pop("cron_jitter", None)
    spec = importlib.util.spec_from_file_location("cron_jitter", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_jitter"] = m
    spec.loader.exec_module(m)
    return m


def test_expand_one_bases():
    m = _load()
    assert m.expand_one("@jitter:2h", 7) == "7 */2 * * *"
    assert m.expand_one("@jitter:hourly", 0) == "0 * * * *"
    assert m.expand_one("@jitter:daily", 59) == "59 13 * * *"
    assert m.expand_one("@jitter:weekly", 5) == "5 13 * * 1"
    assert m.expand_one("@jitter:6h", 61) == "1 */6 * * *"  # minute wraps mod 60


def test_non_sentinel_is_left_alone():
    m = _load()
    assert m.expand_one("0 */2 * * *", 9) is None
    assert not m.is_sentinel("0 */2 * * *")
    assert m.is_sentinel("@jitter:daily")


def test_expand_jobs_assigns_random_minute_per_job():
    m = _load()
    jobs = [
        {"name": "a", "schedule": {"kind": "cron", "expr": "@jitter:2h"}},
        {"name": "b", "schedule": {"kind": "cron", "expr": "@jitter:daily"}},
        {"name": "c", "schedule": {"kind": "cron", "expr": "5 1 * * *"}},  # concrete, untouched
    ]
    n = m.expand_jobs(jobs, random.Random(1234))
    assert n == 2
    assert jobs[0]["schedule"]["expr"].endswith("*/2 * * *")
    assert jobs[1]["schedule"]["expr"].endswith("13 * * *")
    assert jobs[2]["schedule"]["expr"] == "5 1 * * *"           # not a sentinel — unchanged
    for j in jobs[:2]:
        assert not m.is_sentinel(j["schedule"]["expr"])         # fully expanded


def test_expand_jobs_never_picks_minute_zero():
    """okengine#103: the jittered minute must never be 0 — a :00 schedule is the herd-prone
    case the jitter exists to avoid, and the validator rejects it. Sweep many seeds."""
    m = _load()
    for seed in range(300):
        jobs = [{"name": "x", "schedule": {"kind": "cron", "expr": "@jitter:daily"}},
                {"name": "y", "schedule": {"kind": "cron", "expr": "@jitter:hourly"}}]
        m.expand_jobs(jobs, random.Random(seed))
        for j in jobs:
            minute = int(j["schedule"]["expr"].split()[0])
            assert 1 <= minute <= 59, f"seed {seed} -> herd-prone/invalid minute in {j['schedule']['expr']!r}"


def test_expand_file_roundtrip(tmp_path):
    m = _load()
    p = tmp_path / "domain-crons.json"
    p.write_text(json.dumps([
        {"name": "f", "enabled": True, "schedule": {"kind": "cron", "expr": "@jitter:2h"}},
    ]))
    n = m.expand_file(p, random.Random(7))
    assert n == 1
    out = json.loads(p.read_text())
    assert not m.is_sentinel(out[0]["schedule"]["expr"])
    # idempotent: a second pass finds nothing to expand
    assert m.expand_file(p) == 0


def test_expand_file_missing_is_noop(tmp_path):
    m = _load()
    assert m.expand_file(tmp_path / "nope.json") == 0
