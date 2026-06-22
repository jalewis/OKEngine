"""Regression: disabled cron jobs are not written to the deployed cron-plus-jobs.json
(#27) — so cron-plus never validates their never-fires sentinel expr and never logs
'invalid cron expr' noise. The in-memory merge keeps them (split/compose stay lossless);
only the deploy serializer (_dump_jobs) drops them."""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron_pack_split.py"


def _mod():
    spec = importlib.util.spec_from_file_location("cron_pack_split", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cron_pack_split"] = m
    spec.loader.exec_module(m)
    return m


def test_dump_jobs_drops_disabled():
    m = _mod()
    text = m._dump_jobs([
        {"name": "on", "enabled": True, "schedule": {"kind": "cron", "expr": "0 * * * *"}},
        {"name": "off", "enabled": False, "schedule": {"kind": "cron", "expr": "0 0 30 2 *"}},
        {"name": "default-on", "schedule": {"kind": "cron", "expr": "0 * * * *"}},  # no enabled field
    ])
    names = {j["name"] for j in json.loads(text)["jobs"]}
    assert names == {"on", "default-on"}, names


def test_skeleton_crons_ship_enabled_and_jittered():
    """Useful-by-default (replaces the old safe-default invariant): the skeleton
    ships its domain crons ENABLED with a `@jitter:*` schedule sentinel — expanded
    to a random concrete minute at framework init/pull. They are NOT disabled
    never-fires placeholders, and a committed round schedule must never appear."""
    sk = REPO / "templates" / "pack" / "skeleton" / "crons" / "domain-crons.json"
    jobs = json.loads(sk.read_text())
    assert jobs
    assert all(j.get("enabled") is True for j in jobs), "skeleton crons must ship enabled"
    assert all((j.get("schedule") or {}).get("expr", "").startswith("@jitter:")
               for j in jobs), "skeleton crons must use a @jitter:* sentinel"
    assert all("0 0 30 2 *" not in json.dumps(j) for j in jobs), "no never-fires placeholder"
