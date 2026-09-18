"""Final-composition budget and least-privilege contracts (okengine#505)."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "cron_pack_split_agent_bounds", ROOT / "scripts/cron_pack_split.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _tracked_agent_jobs():
    jobs = json.loads((ROOT / "config/engine-crons.json").read_text(encoding="utf-8"))
    for manifest in sorted((ROOT / "extensions").glob("*/extension.yaml")):
        document = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        operations = document.get("operations") or []
        if isinstance(operations, dict):
            operations = list(operations.values())
        if isinstance(document.get("operation"), dict):
            operations = [*operations, document["operation"]]
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict) or not (
                    operation.get("prompt") or operation.get("prompt_file")):
                continue
            jobs.append({
                "name": operation.get("name") or f"{manifest.parent.name}:{index}",
                "max_iterations": operation.get("max_iterations"),
                "enabled_toolsets": operation.get("toolsets") or [],
            })
    return jobs


def test_validator_rejects_every_unbounded_agent_shape():
    jobs = [
        {"name": "deterministic", "no_agent": True},
        {"name": "missing"},
        {"name": "boolean", "max_iterations": True},
        {"name": "zero", "max_iterations": 0},
        {"name": "one-turn", "max_iterations": 1},
        {"name": "string-no-agent", "no_agent": "true"},
        {"name": "numeric-no-agent", "no_agent": 1},
        {"name": "bounded", "max_iterations": 8},
    ]
    errors = MODULE.validate_agent_bounds(jobs)
    assert len(errors) == 6
    assert all(name in " ".join(errors) for name in (
        "missing", "boolean", "zero", "one-turn", "string-no-agent", "numeric-no-agent",
    ))


def test_tracked_agent_definitions_have_finite_lanes_and_no_broad_context():
    jobs = _tracked_agent_jobs()
    assert MODULE.validate_agent_bounds(jobs) == []
    forbidden = {"terminal", "skills", "delegation", "file"}
    excess = {
        job["name"]: sorted(forbidden & set(job.get("enabled_toolsets") or []))
        for job in jobs if not job.get("no_agent")
        and forbidden & set(job.get("enabled_toolsets") or [])
    }
    assert excess == {}


def test_multi_item_selectors_leave_a_terminal_accounting_turn():
    jobs = {
        job["name"]: job
        for job in json.loads(
            (ROOT / "config/engine-crons.json").read_text(encoding="utf-8")
        )
    }
    selectors = {
        "orphans-drain": ("select_orphans_drain.py", "ORPHAN_BATCH_SIZE", 1),
        "publisher-canonical-drain": (
            "select_publisher_canonical_drain.py", "PCD_MAX_CANDIDATES", 3
        ),
        "repair-yaml-propose": ("select_broken_yaml.py", "YAML_REPAIR_BATCH_SIZE", 1),
        "review-drain": ("select_review_drain.py", "REVIEW_DRAIN_BATCH", 1),
        "trends-refresh": ("select_trend_deltas.py", "TREND_TOP_N", 1),
    }
    for name, (filename, env_name, sections) in selectors.items():
        text = (ROOT / "scripts/cron" / filename).read_text(encoding="utf-8")
        match = re.search(rf'{env_name}",\s*"(\d+)"', text)
        assert match, f"{name}: selector default is not statically gradeable"
        if name == "publisher-canonical-drain":
            sections = text.count("[:MAX_CANDIDATES_PER_SECTION]")
            assert sections > 0
        assert int(match.group(1)) * sections <= jobs[name]["max_iterations"] - 1


def test_publisher_selector_bounds_every_rendered_label():
    path = ROOT / "scripts/cron/select_publisher_canonical_drain.py"
    spec = importlib.util.spec_from_file_location("publisher_selector_bounds", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = path.read_text(encoding="utf-8")

    assert module.MAX_LABEL_CHARS == 160
    assert text.count("[:MAX_LABEL_CHARS]") == 4
