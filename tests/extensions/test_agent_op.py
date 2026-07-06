"""Agent operations: an operation with a ``prompt`` synthesizes an AGENT cron job
(``no_agent: False``, the okengine toolsets), with the entrypoint — if any — as the
wake-gate selector. Without a prompt an operation stays a deterministic ``no_agent``
script. This is what lets predictions (wake-gated grading lanes) be an extension.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _record(ext_id, op):
    m = {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
         "requires": {"engine": ">=0.4.0"},
         "capabilities": {"read": ["wiki/**"], "write": ["predictions/**"]},
         "operation": op}
    return {"id": ext_id, "tier": "engine", "dir": "/x", "manifest": m}


def test_prompt_makes_an_agent_job_with_wake_gate():
    c = _load("extension_compose", COMPOSE)
    rec = _record("okengine.predictions", {
        "schedule": {"kind": "cron", "expr": "23 6 * * *"},
        "entrypoint": "select_for_grading.py",        # wake-gate
        "prompt": "Grade the open predictions whose resolves_by has passed.",
    })
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    j = jobs[0]
    assert j["no_agent"] is False
    assert j["prompt"].startswith("Grade the open predictions")
    assert j["script"] == "/opt/data/scripts/okengine.predictions/select_for_grading.py"
    assert j["enabled_toolsets"] == ["okengine", "okengine-write"]   # default


def test_agent_op_without_entrypoint_has_no_wake_gate():
    c = _load("extension_compose", COMPOSE)
    rec = _record("okengine.brief", {
        "schedule": {"kind": "cron", "expr": "0 8 * * *"},
        "prompt": "Write the daily brief.",
    })
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors, errors
    j = jobs[0]
    assert j["no_agent"] is False and "script" not in j        # agent, no gate


def test_custom_toolsets_respected():
    c = _load("extension_compose", COMPOSE)
    rec = _record("okengine.predictions", {
        "schedule": {"kind": "cron", "expr": "23 6 * * *"},
        "prompt": "Grade.", "toolsets": ["okengine", "okengine-write", "hermes-cron"],
    })
    jobs, _, _ = c.synthesize_ops(rec)
    assert jobs[0]["enabled_toolsets"] == ["okengine", "okengine-write", "hermes-cron"]


def test_no_prompt_no_entrypoint_is_an_error():
    c = _load("extension_compose", COMPOSE)
    rec = _record("x.y", {"schedule": {"kind": "cron", "expr": "0 4 * * *"}})
    _, errors, _ = c.synthesize_ops(rec)
    assert any("no_agent operation needs an entrypoint" in e for e in errors), errors


def test_no_prompt_stays_no_agent_backcompat():
    c = _load("extension_compose", COMPOSE)
    rec = _record("okengine.contradictions", {
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "run.py"})
    jobs, errors, _ = c.synthesize_ops(rec)
    assert not errors and jobs[0]["no_agent"] is True and jobs[0]["prompt"] is None


def test_mixed_agent_and_no_agent_multi_op():
    c = _load("extension_compose", COMPOSE)
    ext_id = "okengine.predictions"
    m = {"id": ext_id, "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
         "requires": {"engine": ">=0.4.0"},
         "capabilities": {"read": ["wiki/**"], "write": ["predictions/**"]},
         "operations": {
             "candidate-watch": {"schedule": {"kind": "cron", "expr": "17 6 * * *"},
                                 "entrypoint": "select_candidates.py",
                                 "prompt": "File prediction candidates."},
             "regrade-sweep": {"schedule": {"kind": "cron", "expr": "0 3 * * *"},
                               "entrypoint": "reindex.py"},   # no prompt -> no_agent
         }}
    jobs, errors, _ = c.synthesize_ops({"id": ext_id, "tier": "engine", "dir": "/x", "manifest": m})
    assert not errors, errors
    by = {j["name"]: j for j in jobs}
    assert by[f"{ext_id}:candidate-watch"]["no_agent"] is False
    assert by[f"{ext_id}:regrade-sweep"]["no_agent"] is True


# --- manifest validation ----------------------------------------------------

def test_manifest_accepts_agent_op_without_entrypoint():
    mod = _load("extension_manifest", MANIFEST)
    rec = _record("okengine.brief", {"schedule": {"kind": "cron", "expr": "0 8 * * *"},
                                      "prompt": "Write the brief."})
    errors, _ = mod.validate_manifest(rec["manifest"])
    assert not errors, errors


def test_manifest_rejects_op_with_neither_entrypoint_nor_prompt():
    mod = _load("extension_manifest", MANIFEST)
    rec = _record("x.y", {"schedule": {"kind": "cron", "expr": "0 4 * * *"}})
    errors, _ = mod.validate_manifest(rec["manifest"])
    assert any("entrypoint script, or a 'prompt'" in e for e in errors), errors


def test_manifest_rejects_bad_toolsets():
    mod = _load("extension_manifest", MANIFEST)
    rec = _record("x.y", {"schedule": {"kind": "cron", "expr": "0 4 * * *"},
                          "prompt": "x", "toolsets": "okengine"})   # str, not list
    errors, _ = mod.validate_manifest(rec["manifest"])
    assert any("toolsets must be a list" in e for e in errors), errors
