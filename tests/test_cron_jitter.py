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


def test_expand_jobs_is_stable_under_a_fixed_seed():  # invariant-audit #47
    """The deploy re-expands ENGINE @jitter sentinels on EVERY run; deploy-cron-plus-jobs.sh now
    seeds the RNG from the pack identity so the minute is STABLE across redeploys (no silent
    skip/double-run). Lock the property the deploy relies on: identical jobs + identical seed ->
    identical expansion; a different seed generally differs (per-install spread preserved)."""
    m = _load()
    def _run(seed):
        jobs = [{"name": "a", "schedule": {"kind": "cron", "expr": "@jitter:daily"}},
                {"name": "b", "schedule": {"kind": "cron", "expr": "@jitter:6h"}}]
        m.expand_jobs(jobs, random.Random(seed))
        return [j["schedule"]["expr"] for j in jobs]
    assert _run(4242) == _run(4242)                    # deterministic per seed -> redeploy-stable
    assert any(_run(s) != _run(0) for s in range(1, 12))   # some other install seed -> different spread


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


def test_unsupported_jitter_base_fails_loud():  # invariant-audit #14
    """An unsupported @jitter base never expands; shipping the raw sentinel makes cron-plus error
    every tick and the lane silently never fires. Fail loud at the deploy/pull gate instead."""
    import pytest
    m = _load()
    for bad in ("@jitter:3h", "@jitter:8h", "@jitter:30m"):
        with pytest.raises(ValueError, match="unsupported @jitter base"):
            m.expand_jobs([{"schedule": {"expr": bad}}])


def test_supported_jitter_bases_still_expand():
    m = _load()
    for base in ("hourly", "2h", "4h", "6h", "12h", "daily", "weekly"):
        j = [{"schedule": {"expr": f"@jitter:{base}"}}]
        assert m.expand_jobs(j, random.Random(1)) == 1
        assert "@jitter" not in j[0]["schedule"]["expr"]


def test_validator_jitter_bases_match_expander():  # anti-drift guard
    """The skeleton validator (templates/pack/skeleton/validate.py) rejects unsupported @jitter
    bases inline; that set MUST equal the engine expander's supported bases (cron_jitter's
    _SENTINEL_RE), or an author-facing FAIL and the deploy-time expander disagree on what's valid."""
    import re
    m = _load()
    expander = set(re.search(r"\(([^)]+)\)", m._SENTINEL_RE.pattern).group(1).split("|"))
    vtext = (REPO / "templates" / "pack" / "skeleton" / "validate.py").read_text()
    for base in expander:
        assert f'"{base}"' in vtext, f"validate.py no longer accepts the expander base '{base}'"


def test_skeleton_validator_accepts_bare_string_schedule(tmp_path):
    """The standalone pack gate must accept the same schedule shapes as framework validate."""
    crons = tmp_path / "crons"
    crons.mkdir()
    (crons / "domain-crons.json").write_text(json.dumps([
        {"name": "string", "enabled": True, "schedule": "17 5 * * *"},
        {"name": "sentinel", "enabled": True, "schedule": "@jitter:daily"},
    ]))
    spec = importlib.util.spec_from_file_location(
        "skeleton_validate", REPO / "templates" / "pack" / "skeleton" / "validate.py"
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    validator.ROOT = tmp_path
    validator.fails.clear()

    validator.check_crons_jittered()

    assert validator.fails == []


def test_expand_jobs_handles_all_three_schedule_shapes():
    """invariant-audit #2: framework validate accepts a dict schedule, a BARE STRING schedule, and a
    top-level `expr`. The expander must handle all three — before the fix a string schedule raised
    AttributeError (aborting the whole cron deploy under `set -euo pipefail`) and a top-level `expr`
    sentinel was silently never expanded (cron-plus can't parse the raw sentinel → the lane dies)."""
    m = _load()
    jobs = [
        {"name": "dict",     "schedule": {"kind": "cron", "expr": "@jitter:2h"}},
        {"name": "string",   "schedule": "0 13 * * SUN"},        # bare string, concrete -> untouched
        {"name": "str-sent", "schedule": "@jitter:daily"},       # bare string sentinel -> expands
        {"name": "toplvl",   "expr": "@jitter:6h"},              # top-level expr sentinel -> expands
    ]
    n = m.expand_jobs(jobs, random.Random(7))
    assert n == 3                                                # dict + str-sent + toplvl
    assert jobs[0]["schedule"]["expr"].endswith("*/2 * * *")
    assert jobs[1]["schedule"] == "0 13 * * SUN"                 # concrete string preserved (shape kept)
    assert jobs[2]["schedule"].endswith("13 * * *") and not m.is_sentinel(jobs[2]["schedule"])
    assert jobs[3]["expr"].endswith("*/6 * * *") and not m.is_sentinel(jobs[3]["expr"])


def test_expand_brief_jobs_handles_all_three_schedule_shapes():
    """invariant-audit #2: the same three-shape tolerance for `@morning[:MM]` brief expansion."""
    m = _load()
    jobs = [
        {"name": "dict",     "schedule": {"expr": "@morning"}},
        {"name": "string",   "schedule": "@morning:30"},
        {"name": "toplvl",   "expr": "@morning"},
        {"name": "concrete", "schedule": "0 9 * * *"},           # not a sentinel -> untouched, no crash
    ]
    n = m.expand_brief_jobs(jobs, 7)
    assert n == 3
    assert jobs[0]["schedule"]["expr"] == "0 7 * * *"
    assert jobs[1]["schedule"] == "30 7 * * *"                   # bare-string shape preserved
    assert jobs[2]["expr"] == "0 7 * * *"                        # top-level shape preserved
    assert jobs[3]["schedule"] == "0 9 * * *"
