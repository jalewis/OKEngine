"""A prompt names only the write tools its own lane is given (okengine#852).

Hermes publishes a write server's tools as `mcp__<server>__<tool>`, and <server> depends on how the
lane is bound: contracted lanes get a per-lane writer whose name #840 bounds (often to a hash), the
rest keep the generic `okengine-write`. Prompts and selectors that HARDCODED the name went stale
twice (#607, then #840), and nothing noticed because models resolve tools from the tool list, not
from the prose. The name is therefore derived where the binding is decided
(`bind_contract_writers`): prompts write `mcp__OKENGINE_WRITER__<tool>`, selectors read
OKENGINE_WRITE_TOOL_PREFIX from their job env, and the composed fleet is validated so a pack prompt
that names a writer its lane cannot reach fails regen instead of shipping.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("cron_pack_split_852", REPO / "scripts" / "cron_pack_split.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _contracted(name, prompt, **over):
    job = {"name": name, "prompt": prompt, "output_contract": {"api": 1},
           "enabled_toolsets": ["okengine-write", "okengine"]}
    job.update(over)
    return job


def test_placeholder_expands_to_the_bounded_per_lane_writer(m):
    name = "okpack-threat-actors-weekly-brief"           # long enough that #840 hashes it
    (job,) = m.bind_contract_writers([_contracted(name, "use mcp__OKENGINE_WRITER__create_entity")])
    server = m.contract_writer_name(name)
    assert server != m.unbounded_writer_name(name), "fixture must exercise a bounded name"
    prefix = m.write_tool_prefix(server)
    assert job["prompt"] == f"use {prefix}create_entity"
    assert job["env"][m.WRITE_TOOL_PREFIX_ENV] == prefix
    assert m.validate_prompt_writer_bindings([job]) == []


def test_placeholder_expands_to_the_generic_writer_for_an_uncontracted_lane(m):
    (job,) = m.bind_contract_writers([{"name": "plain", "prompt": "mcp__OKENGINE_WRITER__patch_entity",
                                       "enabled_toolsets": ["okengine-write"], "env": {"KEEP": "1"}}])
    assert job["prompt"] == "mcp__okengine_write__patch_entity"
    assert job["env"] == {"KEEP": "1", m.WRITE_TOOL_PREFIX_ENV: "mcp__okengine_write__"}


def test_script_lanes_and_writerless_lanes_are_left_alone(m):
    script = {"name": "s", "no_agent": True, "enabled_toolsets": ["okengine-write"], "prompt": "x"}
    reader = {"name": "r", "prompt": "read only", "enabled_toolsets": ["okengine"]}
    assert m.bind_contract_writers([script, reader]) == [script, reader]


def test_a_stale_hardcoded_writer_fails_the_fleet_validation(m):
    """The live finding: a pack's weekly brief named its pre-#840 writer, which its lane no longer
    binds. This must fail regen, not ship."""
    name = "okpack-threat-actors-weekly-brief"
    stale = m.write_tool_prefix(m.unbounded_writer_name(name)) + "create_entity"
    (job,) = m.bind_contract_writers([_contracted(name, f"write via {stale}")])
    errors = m.validate_prompt_writer_bindings([job])
    assert len(errors) == 1 and stale in errors[0] and "mcp__OKENGINE_WRITER__create_entity" in errors[0]


def test_the_generic_writer_named_by_a_contracted_lane_fails_too(m):
    (job,) = m.bind_contract_writers([_contracted("broken-wikilinks-drain",
                                                  "call mcp__okengine_write__create_entity")])
    assert m.validate_prompt_writer_bindings([job]), "the lane binds only its per-lane writer"


def test_an_unresolvable_placeholder_is_reported(m):
    job = {"name": "two", "prompt": "mcp__OKENGINE_WRITER__create_entity",
           "enabled_toolsets": ["okengine-write", "okengine-write-extra"]}
    (bound,) = m.bind_contract_writers([job])
    assert "cannot be resolved" in m.validate_prompt_writer_bindings([bound])[0]


def test_non_prompt_jobs_are_not_judged(m):
    assert m.validate_prompt_writer_bindings([{"name": "s", "no_agent": True, "prompt": "mcp__okengine_write_x__y"},
                                              {"name": "p", "prompt": None}]) == []


def test_a_live_dump_restores_the_placeholder_and_drops_the_env(m):
    name = "okpack-threat-actors-weekly-brief"
    (live,) = m.bind_contract_writers([_contracted(name, "mcp__OKENGINE_WRITER__create_entity",
                                                   env={"KEEP": "1"})])
    restored = m._unpublish_writer(live)
    assert restored["prompt"] == "mcp__OKENGINE_WRITER__create_entity"
    assert restored["env"] == {"KEEP": "1"}
    bare = m._unpublish_writer({**live, "env": {m.WRITE_TOOL_PREFIX_ENV: "mcp__okengine_write__"}})
    assert "env" not in bare
    untouched = {"name": "x", "prompt": "p"}
    assert m._unpublish_writer(untouched) is untouched


def test_bind_is_idempotent_so_a_regen_of_a_regen_is_stable(m):
    once = m.bind_contract_writers([_contracted("okpack-threat-actors-weekly-brief",
                                                "mcp__OKENGINE_WRITER__create_entity")])
    assert m.bind_contract_writers(once) == once


# ---- the standing detector: no hardcoded write-server name anywhere the engine ships --------

_HARDCODED = re.compile(r"mcp__okengine_write[A-Za-z0-9_]*__[a-z_]+")


def _explicit_writers() -> set[str]:
    """Write servers the engine names DELIBERATELY in a lane's toolsets (e.g. the scoring-only
    `okengine-write-source-quality`). Such a lane can bind two writers, so its prompt must name the
    narrow one literally; that is a choice, not a derived name that can go stale."""
    import json
    jobs = json.loads((REPO / "config" / "engine-crons.json").read_text(encoding="utf-8"))
    jobs = jobs["jobs"] if isinstance(jobs, dict) else jobs
    return {re.sub(r"[^A-Za-z0-9_]", "_", t) for j in jobs for t in (j.get("enabled_toolsets") or [])
            if str(t).startswith("okengine-write-")}


def test_no_engine_prompt_or_selector_hardcodes_a_write_server_name():
    """Graduation for #852: the class is watched continuously. Engine crons, extension prompts and
    selector scripts name write tools only through the placeholder, the published env prefix, or a
    server the engine declares explicitly in a lane's toolsets."""
    explicit = _explicit_writers()
    assert "okengine_write_source_quality" in explicit, "fixture: the scoring-only server is declared"
    files = subprocess.run(["git", "ls-files", "config/engine-crons.json", "extensions", "scripts/cron",
                            "prompts", "templates"],
                           cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    offenders = []
    for rel in files:
        if not rel.endswith((".md", ".json", ".py", ".yaml")):
            continue
        for n, line in enumerate((REPO / rel).read_text(encoding="utf-8").splitlines(), 1):
            for hit in _HARDCODED.findall(line):
                # the selector fallback when run outside cron-plus (no job env) is the one allowed form
                if "OKENGINE_WRITE_TOOL_PREFIX" in line and hit.startswith("mcp__okengine_write__"):
                    continue
                if hit.split("__")[1] in explicit:
                    continue
                offenders.append(f"{rel}:{n}: {hit}")
    assert not offenders, "hardcoded write-server names (use the placeholder / env prefix):\n" + "\n".join(offenders)


def test_a_dump_without_the_resolver_libs_still_unpublishes_the_writer(m, monkeypatch):
    original_import = __import__

    def importing(name, *args, **kwargs):
        if name == "cron_jitter":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", importing)
    live = [{"name": "x", "prompt": "mcp__okengine_write__create_entity",
             "env": {m.WRITE_TOOL_PREFIX_ENV: "mcp__okengine_write__"}}]
    (restored,) = m._restore_source_reprs(live)
    assert restored == {"name": "x", "prompt": "mcp__OKENGINE_WRITER__create_entity"}


def test_unpublish_tolerates_a_lane_without_a_prompt(m):
    assert m._unpublish_writer({"name": "x", "env": {m.WRITE_TOOL_PREFIX_ENV: "mcp__okengine_write__"}}) == {"name": "x"}
