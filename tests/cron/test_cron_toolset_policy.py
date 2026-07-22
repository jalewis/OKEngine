"""Toolset policy for agent cron lanes (okengine#54 follow-through).

An agent lane's `enabled_toolsets` is an ALLOWLIST the scheduler enforces
(per-job list wins over the platform default; MCP servers are layered on).
The okcti 2026-07-13 incident showed why breadth is a bug: a lane carrying
the kitchen-sink `hermes-cron` composite wrote a vault page flat via
write_file (against its own prompt) and minted a #54 duplicate, and lanes
whose prompts FORBID web tools were spending the shared paid web budget.

Policy, enforced here at the source (engine-crons.json + extension
manifests; the same shape flows into every deployment's jobs store):
  1. Every agent lane declares enabled_toolsets explicitly — no falling
     back to the platform default (full native toolset, no MCP).
  2. No composite/platform toolset (hermes-cron, hermes-cli, coding, …) —
     breadth must be spelled out.
  3. `terminal` only where the lane's job genuinely needs a shell.
  4. `web` only where the lane's job is to consult the outside world.
"""
import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]

# composites that resolve to (nearly) every native tool — never grant these to a lane
_COMPOSITES = {"hermes-cron", "hermes-cli", "hermes-api-server", "hermes-acp",
               "hermes-gateway", "coding", "all", "*"}

_TERMINAL_OK = {"wiki-health-audit"}                      # runs wiki_schema_audit.py in a shell
_WEB_OK = {"okengine.predictions:prediction-falsification-search"}


def _engine_agent_lanes():
    d = json.loads((REPO / "config" / "engine-crons.json").read_text())
    jobs = d["jobs"] if isinstance(d, dict) and "jobs" in d else d
    return [j for j in jobs if not j.get("no_agent")]


def _extension_agent_ops():
    """(lane-name, toolsets) for every agent operation in extensions/*/extension.yaml
    — both the single-operation form (`operation:`) and the map form (`operations:`)."""
    out = []
    for f in sorted(REPO.glob("extensions/*/extension.yaml")):
        m = yaml.safe_load(f.read_text())
        ext = m.get("id", f.parent.name)
        ops = []
        if isinstance(m.get("operation"), dict):
            ops.append((ext, m["operation"]))
        for key, op in (m.get("operations") or {}).items():
            if isinstance(op, dict):
                ops.append((f"{ext}:{key}", op))
        for name, op in ops:
            if op.get("no_agent"):
                continue
            out.append((name, op.get("toolsets")))
    return out


def test_engine_agent_lanes_declare_explicit_narrow_toolsets():
    problems = []
    for j in _engine_agent_lanes():
        name, ts = j.get("name"), j.get("enabled_toolsets")
        if not ts:
            problems.append(f"{name}: no enabled_toolsets (falls back to the broad platform default)")
            continue
        for t in ts:
            if t in _COMPOSITES:
                problems.append(f"{name}: composite toolset '{t}' — spell the breadth out")
        if "terminal" in ts and name not in _TERMINAL_OK:
            problems.append(f"{name}: grants 'terminal' but is not on the shell allowlist")
        if "web" in ts and name not in _WEB_OK:
            problems.append(f"{name}: grants 'web' but is not on the web allowlist")
    assert not problems, "\n".join(problems)


def test_model_write_lanes_never_receive_native_file_mutation_tools():
    problems = []
    for job in _engine_agent_lanes():
        if "okengine-write" not in (job.get("enabled_toolsets") or []):
            continue
        toolsets = set(job["enabled_toolsets"])
        if "file" in toolsets:
            problems.append(f"{job['name']}: model writer uses mutable file toolset")
    assert not problems, "\n".join(problems)


def test_wiki_health_audit_uses_portable_schema_audit_command():
    job = next(lane for lane in _engine_agent_lanes() if lane["name"] == "wiki-health-audit")
    assert job["prompt"].startswith("Read /opt/vault/CLAUDE.md and follow")
    assert "$WIKI_PATH/CLAUDE.md" not in job["prompt"]
    assert "run `python3 /opt/data/scripts/wiki_schema_audit.py`" in job["prompt"]


def test_extension_agent_ops_declare_narrow_toolsets():
    problems = []
    for name, ts in _extension_agent_ops():
        if not ts:
            continue   # extension synthesis fills the narrow _DEFAULT_TOOLSETS
        for t in ts:
            if t in _COMPOSITES:
                problems.append(f"{name}: composite toolset '{t}' — spell the breadth out")
        if "terminal" in ts and name not in _TERMINAL_OK:
            problems.append(f"{name}: grants 'terminal' but is not on the shell allowlist")
        if "web" in ts and name not in _WEB_OK:
            problems.append(f"{name}: grants 'web' but is not on the web allowlist")
    assert not problems, "\n".join(problems)


def test_extension_default_toolsets_stay_narrow():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "extension_compose", REPO / "scripts" / "extension_compose.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert not set(mod._DEFAULT_TOOLSETS) & _COMPOSITES, \
        "extension _DEFAULT_TOOLSETS must not include a composite toolset"
    assert "terminal" not in mod._DEFAULT_TOOLSETS and "web" not in mod._DEFAULT_TOOLSETS
