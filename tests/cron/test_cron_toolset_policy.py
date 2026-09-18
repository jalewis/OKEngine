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
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]

# composites that resolve to (nearly) every native tool — never grant these to a lane
_COMPOSITES = {"hermes-cron", "hermes-cli", "hermes-api-server", "hermes-acp",
               "hermes-gateway", "coding", "all", "*"}

_TERMINAL_OK = set()
_WEB_OK = {"okengine.predictions:prediction-falsification-search"}

_MCP_NAME_SURFACES = ("config", "docs", "extensions", "scripts", "templates")
_TEXT_SUFFIXES = {".json", ".md", ".py", ".sh", ".yaml", ".yml"}


def _hermes_mcp_tool_name(server_name, tool_name):
    """Pin Hermes's public MCP naming contract without importing a sibling checkout."""
    sanitize = lambda value: re.sub(r"[^A-Za-z0-9_]", "_", value)
    return f"mcp__{sanitize(server_name)}__{sanitize(tool_name)}"


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
        if ts is None:
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
    for name, toolsets in _extension_agent_ops():
        toolsets = set(toolsets or [])
        if "okengine-write" in toolsets and "file" in toolsets:
            problems.append(f"{name}: extension model writer uses mutable file toolset")
    assert not problems, "\n".join(problems)


def test_every_contracted_lane_has_reader_and_writer_mcp_servers():
    """Positive invariant: canonical names are useless if MCP was never offered."""
    problems = []
    for job in _engine_agent_lanes():
        if not isinstance(job.get("output_contract"), dict):
            continue
        toolsets = set(job.get("enabled_toolsets") or [])
        writers = {tool for tool in toolsets if tool == "okengine-write"
                   or tool.startswith("okengine-write-")}
        if "okengine" not in toolsets or not writers:
            problems.append(f"{job['name']}: MCP reader/writer surface incomplete: {sorted(toolsets)}")
    assert not problems, "\n".join(problems)


def test_wiki_health_audit_is_deterministic_and_has_no_model_tools():
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text())
    job = next(lane for lane in jobs if lane["name"] == "wiki-health-audit")
    assert job["no_agent"] is True
    assert job["script"] == "wiki_health_audit.py"
    assert "enabled_toolsets" not in job
    assert "skills" not in job and "skill" not in job
    assert "output_contract" not in job


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


def test_prompts_use_hermes_canonical_mcp_tool_names():
    """Hermes publishes mcp__<server>__<tool>; the old spelling cannot resolve."""
    assert _hermes_mcp_tool_name("okengine", "get_page") == "mcp__okengine__get_page"
    assert (_hermes_mcp_tool_name("okengine-write", "create_entity")
            == "mcp__okengine_write__create_entity")
    assert (_hermes_mcp_tool_name("okengine-write-source-quality", "score_source")
            == "mcp__okengine_write_source_quality__score_source")

    obsolete = []
    scanned = 0
    pattern = re.compile(r"\bmcp_okengine(?:_|\b)")
    for root_name in _MCP_NAME_SURFACES:
        for path in (REPO / root_name).rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            scanned += 1
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    obsolete.append(f"{path.relative_to(REPO)}:{line_number}: {line.strip()}")
    # An empty scan is UNDETECTABLE, not a pass (okengine#559). This check found a real leak only
    # because config/cron-plus-jobs.json -- a GENERATED, gitignored artifact -- happened to exist in
    # one developer checkout. CI checks out clean, that file is absent, and the scan sails past with
    # nothing to look at. Assert the surfaces actually held files, so a renamed directory or a
    # narrowed suffix list fails loudly instead of quietly measuring nothing.
    assert scanned > 50, (
        f"MCP-name scan examined only {scanned} file(s) across {_MCP_NAME_SURFACES} -- the surfaces "
        "moved or the suffix list narrowed, so this guard is measuring nothing. UNDETECTABLE, "
        "not a pass.")
    assert not obsolete, ("obsolete MCP tool spelling; use Hermes's "
                          "mcp__<server>__<tool> contract:\n" + "\n".join(obsolete))
