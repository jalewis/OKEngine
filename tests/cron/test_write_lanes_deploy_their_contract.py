"""A lane that declares write operations must reach the deployed jobs file (okengine#886).

The write path authenticates a no_agent lane by looking its `output_contract` up BY NAME in the
deployed cron-plus jobs.json. `cron_pack_split` deliberately omits every `enabled: false` job from
that artifact, so a lane shipped disabled can never write — not on a schedule and not when an
operator runs it by hand. `dedup-same-url` shipped that way: its first live `--apply` on one
vault was refused 43 times ("caller has no declared write capability") and merged nothing. The
lane's own tests read the contract from config/engine-crons.json directly, so they never saw it.

A lane whose scheduled run should write nothing ships ENABLED with a dry-run default instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import cron_pack_split  # noqa: E402


def _writes(job: dict) -> bool:
    return bool((job.get("output_contract") or {}).get("operations"))


def _deployed_names(jobs: list[dict]) -> set[str]:
    return {j["name"] for j in json.loads(cron_pack_split._dump_jobs(jobs))["jobs"]}


def _unreachable(jobs: list[dict]) -> list[str]:
    deployed = _deployed_names(jobs)
    return sorted(j["name"] for j in jobs if _writes(j) and j["name"] not in deployed)


def test_every_engine_write_lane_reaches_the_deployed_jobs_file():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text(encoding="utf-8"))
    assert _unreachable(jobs) == [], (
        "these lanes declare write operations but are omitted from the deployed jobs.json (they "
        "ship enabled: false), so the write path can never authenticate them. Ship them enabled "
        "with a dry-run default rather than disabled.")


def test_the_check_catches_a_ship_disabled_write_lane():
    """NEGATIVE: a check that matched nothing would pass vacuously."""
    base = {"no_agent": True, "script": "x.py", "schedule": {"kind": "cron", "expr": "0 5 * * 0"},
            "output_contract": {"api": 1, "allowed_namespaces": ["sources"],
                                "allowed_types": ["*"], "operations": ["update"]}}
    lanes = [dict(base, name="writes-enabled"), dict(base, name="writes-disabled", enabled=False),
             dict(base, name="reads-disabled", enabled=False, output_contract={})]
    assert _unreachable(lanes) == ["writes-disabled"]
