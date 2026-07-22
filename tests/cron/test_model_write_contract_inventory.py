"""Fail closed when a core model-writing cron lacks a bounded contract."""
import json
from pathlib import Path

from scripts.cron.output_contract import validate


ROOT = Path(__file__).resolve().parents[2]


def test_core_model_writers_have_valid_contracts_and_adversarial_fixtures():
    jobs = json.loads((ROOT / "config/engine-crons.json").read_text(encoding="utf-8"))
    assert not [job["name"] for job in jobs if job.get("output_contract_exempt")]
    writers = [job for job in jobs if not job.get("no_agent") and any(
        tool == "okengine-write" or tool.startswith("okengine-write-")
        for tool in (job.get("enabled_toolsets") or []))]
    assert writers
    for job in writers:
        assert not validate(job.get("output_contract"), job["name"]), job["name"]
        fixtures = job.get("adversarial_fixtures")
        assert isinstance(fixtures, list) and fixtures, job["name"]
        assert all((ROOT / fixture).is_file() for fixture in fixtures), job["name"]


def test_deterministic_core_jobs_never_wake_an_agent():
    jobs = {job["name"]: job for job in json.loads(
        (ROOT / "config/engine-crons.json").read_text(encoding="utf-8"))}
    for name in {"corpus-indexer", "index-rebuild-daily", "lint-watcher",
                 "source-portfolio-refresh"}:
        assert jobs[name].get("no_agent") is True
