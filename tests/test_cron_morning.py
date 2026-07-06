"""@morning schedule sentinel — one deployment knob for when daily briefs run (#177).

Brief lanes ship `@morning[:MM]`; it expands at deploy to `MM <BRIEF_HOUR> * * *`
so every deployment clusters its reader-facing briefs in the operator's morning
without forking any schedule. Mirrors the @jitter sentinel."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cron_jitter", REPO / "scripts" / "cron_jitter.py")
cj = importlib.util.module_from_spec(spec); sys.modules["cron_jitter"] = cj; spec.loader.exec_module(cj)


def test_morning_expands_to_the_brief_hour():
    assert cj.expand_morning_one("@morning", 7) == "0 7 * * *"
    assert cj.expand_morning_one("@morning:30", 7) == "30 7 * * *"
    assert cj.expand_morning_one("@morning:15", 6) == "15 6 * * *"
    assert cj.expand_morning_one("@morning:45", 8) == "45 8 * * *"


def test_non_morning_expr_untouched():
    assert cj.expand_morning_one("0 12 * * *", 7) is None
    assert cj.expand_morning_one("@jitter:daily", 7) is None
    assert not cj.is_morning_sentinel("30 7 * * *")
    assert cj.is_morning_sentinel("@morning:30")


def test_expand_brief_jobs_in_place_and_counts():
    jobs = [
        {"name": "daily-brief", "schedule": {"kind": "cron", "expr": "@morning:30"}},
        {"name": "messaging", "schedule": {"kind": "cron", "expr": "@morning:15"}},
        {"name": "index", "schedule": {"kind": "cron", "expr": "0 1 * * *"}},   # untouched
    ]
    n = cj.expand_brief_jobs(jobs, 7)
    assert n == 2
    assert jobs[0]["schedule"]["expr"] == "30 7 * * *"
    assert jobs[1]["schedule"]["expr"] == "15 7 * * *"
    assert jobs[2]["schedule"]["expr"] == "0 1 * * *"


def test_engine_daily_brief_ships_the_sentinel():
    """The engine's own daily-brief stub must use @morning (not a hardcoded hour),
    so a fresh deployment gets morning briefs by default."""
    import json
    crons = json.loads((REPO / "config" / "engine-crons.json").read_text())
    brief = next(j for j in crons if j["name"] == "daily-brief")
    assert brief["schedule"]["expr"] == "@morning:30", brief["schedule"]
