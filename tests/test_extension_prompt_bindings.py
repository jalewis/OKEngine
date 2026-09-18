"""Extension prompts name the write server their composed operation actually binds (okengine#607).

`bind_contract_writers` routes a CONTRACTED model lane — one with an `output_contract`, not
`no_agent` — to its own per-lane server `okengine-write-<slug>`, and leaves everything else on the
generic `okengine-write`. Hermes then publishes each as `mcp__<server>__<tool>` with non-word
characters mapped to `_`. An extension operation composes to a job named `<ext_id>:<op>`, so
`okengine.predictions`' `grade` binds `okengine-write-okengine-predictions-grade`.

Every one of the 21 write-tool mentions across the first-party extension prompts named the GENERIC
server instead. The lanes worked — the model resolves tools from the tool list, not from prose —
which is exactly why nothing surfaced it, and why it has to be checked statically.

The check composes the real extensions through the real code (`extension_discovery.discover` ->
`extension_compose.compose` -> `cron_pack_split.bind_contract_writers`) rather than re-deriving the
naming rule. A mirrored rule can agree with itself while disagreeing with the engine; the pack
repos have to mirror it because they cannot import the engine, but this repo has no such excuse.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import cron_pack_split  # noqa: E402
import extension_compose  # noqa: E402
import extension_discovery  # noqa: E402

# mcp__<server>__<tool>, restricted to the write servers — the read server (`okengine`) has no
# per-lane variants, so it cannot be mis-bound this way.
TOOL_RE = re.compile(r"mcp__(okengine_write[a-z0-9_]*?)__([a-z_]+)")


def hermes_name(server: str) -> str:
    """Hermes's MCP naming contract: non-word characters become `_`."""
    return re.sub(r"[^A-Za-z0-9_]", "_", server)


def composed_jobs() -> list[dict]:
    """Every first-party extension, composed and writer-bound the way a deploy does it.

    This walks `extensions/` on disk and parses every manifest, which costs ~0.25s — well over
    the 0.100s unit budget. Its callers are marked `integration` explicitly: the layer inference
    reads each TEST's own source, not its helpers', so a test whose only I/O happens one call
    away infers as `unit` and then fails the budget on main, where unit-suite runs
    automatically. Marking it here is the honest classification, not a workaround — composing
    from the real filesystem is what makes this check worth having.
    """
    extensions, errors = extension_discovery.discover(None)
    assert not errors, f"extension discovery failed: {errors}"
    resolved, resolve_errors = extension_discovery.resolve_enabled(
        sorted(e["id"] for e in extensions), extensions)
    assert not resolve_errors, f"extension resolution failed: {resolve_errors}"
    jobs, compose_errors, _ = extension_compose.compose(resolved)
    assert not compose_errors, f"extension composition failed: {compose_errors}"
    return cron_pack_split.bind_contract_writers(jobs)


def mismatches(jobs: list[dict]) -> tuple[list[str], int]:
    """(complaints, how many tool mentions were examined)."""
    out, seen = [], 0
    for job in jobs:
        prompt = job.get("prompt") or ""
        if not prompt:
            continue
        writers = {hermes_name(t) for t in (job.get("enabled_toolsets") or [])
                   if t == "okengine-write" or t.startswith("okengine-write-")}
        for named, tool in TOOL_RE.findall(prompt):
            seen += 1
            if named not in writers:
                out.append(f"{job['name']}: prompt names mcp__{named}__{tool} but the lane binds "
                           f"{sorted('mcp__' + w + '__' + tool for w in writers) or '[no writer]'}")
    return out, seen


@pytest.mark.integration
def test_every_extension_prompt_names_a_writer_its_lane_binds():
    """The gate. A prompt naming a server the job does not bind is a prompt lying about what it
    will call — harmless at runtime today, and undetectable by any runtime signal."""
    jobs = composed_jobs()
    complaints, seen = mismatches(jobs)
    assert seen > 0, ("no write-tool mentions found in any composed extension prompt — the "
                      "prompts moved or composition returned nothing. UNDETECTABLE, not a pass.")
    assert not complaints, "\n".join(complaints)


@pytest.mark.integration
def test_composition_produced_prompt_bearing_jobs_to_check():
    """Guards the guard: `compose()` returning an empty or promptless list would make the check
    above pass over nothing, which is the shape of every gate that quietly stops working."""
    jobs = composed_jobs()
    assert len(jobs) > 20, f"only {len(jobs)} extension job(s) composed"
    assert sum(1 for j in jobs if j.get("prompt")) > 5, "almost no composed job carries a prompt"


def test_a_contracted_lane_binds_its_own_writer_and_others_stay_generic():
    """The rule this check depends on, asserted against the engine's own function rather than
    restated — if `bind_contract_writers` changes, this fails here instead of silently
    invalidating every assertion above."""
    contracted = {"name": "demo:op", "output_contract": {"api": 1},
                  "enabled_toolsets": ["okengine-write", "okengine"]}
    plain = {"name": "demo:other", "enabled_toolsets": ["okengine-write"]}
    script = {"name": "demo:script", "no_agent": True, "output_contract": {"api": 1},
              "enabled_toolsets": ["okengine-write"]}
    bound = {j["name"]: j for j in cron_pack_split.bind_contract_writers([contracted, plain, script])}
    assert "okengine-write-demo-op" in bound["demo:op"]["enabled_toolsets"]
    assert bound["demo:other"]["enabled_toolsets"] == ["okengine-write"]
    assert bound["demo:script"]["enabled_toolsets"] == ["okengine-write"]


def test_the_check_catches_a_prompt_that_names_the_wrong_writer():
    """A red-by-construction case, so a refactor that made `mismatches` always return [] would
    fail here rather than turn the gate green."""
    job = {"name": "demo:op", "enabled_toolsets": ["okengine-write-demo-op"],
           "prompt": "write it via mcp__okengine_write__create_entity"}
    complaints, seen = mismatches([job])
    assert seen == 1 and len(complaints) == 1
    assert "mcp__okengine_write_demo_op__create_entity" in complaints[0]


def test_a_prompt_naming_the_bound_writer_is_accepted():
    job = {"name": "demo:op", "enabled_toolsets": ["okengine-write-demo-op"],
           "prompt": "write it via mcp__okengine_write_demo_op__create_entity"}
    assert mismatches([job]) == ([], 1)


def test_the_generic_writer_is_correct_on_an_uncontracted_lane():
    """Over-specific is as wrong as under-specific; a rule that only checked one direction would
    wave through a prompt naming a per-lane server the job never binds."""
    ok = {"name": "demo:other", "enabled_toolsets": ["okengine-write"],
          "prompt": "mcp__okengine_write__create_entity"}
    bad = {"name": "demo:other", "enabled_toolsets": ["okengine-write"],
           "prompt": "mcp__okengine_write_demo_other__create_entity"}
    assert mismatches([ok]) == ([], 1)
    assert len(mismatches([bad])[0]) == 1


def test_hermes_name_maps_both_separators():
    """`-` in a server name and `.` in an extension id both become `_` in the published tool
    name. A prompt written against the un-sanitised spelling names a tool that cannot resolve."""
    assert hermes_name("okengine-write-okengine-predictions-grade") == \
        "okengine_write_okengine_predictions_grade"
    assert hermes_name("okengine.predictions:grade") == "okengine_predictions_grade"
