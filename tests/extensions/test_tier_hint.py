"""Extension op `tier:` hint — okengine#129 down payment.

An operation can declare `tier: <stage>` so kickstart slots its job into that stage's
dependency order instead of guessing a wall-clock time. The composer stamps it on the job;
kickstart's by_tier() picks it up. (Full cross-extension DAG scheduling stays #129.)
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO / "scripts" / "extension_compose.py"
MANIFEST = REPO / "scripts" / "extension_manifest.py"
KICKSTART = REPO / "scripts" / "kickstart.sh"

pytestmark = pytest.mark.skipif(not COMPOSE.is_file(), reason="extension modules absent")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _rec(op):
    op = dict(op)
    if op.get("prompt") or op.get("prompt_file"):
        op.update({"output_contract": {"api": 1, "allowed_namespaces": ["x"],
                   "allowed_types": ["x"], "operations": ["create"],
                   "required_fields": ["type"], "required_relationships": [],
                   "body": {"required": True, "min_non_whitespace": 1},
                   "unknown_fields": "reject", "unresolved_links": "reject",
                   "placeholder_links": "reject", "completion": "per-selected-item"},
                   "adversarial_fixtures": ["tests/extensions/test_tier_hint.py"]})
    m = {"id": "okengine.scorer", "kind": "operation", "version": "0.1.0", "trust": "in-gateway",
         "requires": {"engine": ">=0.4.0"},
         "capabilities": {"read": ["wiki/**"], "write": ["x/**"]}, "operation": op}
    return {"id": "okengine.scorer", "tier": "engine", "dir": "/x", "manifest": m}


def test_tier_is_stamped_on_the_job():
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(_rec({
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "score.py", "tier": "score"}))
    assert not errors, errors
    assert jobs[0]["tier"] == "score"


def test_model_override_stamped_on_job():
    """Per-op model override (cron scheduler honors job['model']): a low-stakes lane can run
    on a free/cheap model while paid work uses the config default."""
    c = _load("extension_compose", COMPOSE)
    jobs, errors, _ = c.synthesize_ops(_rec({
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "s.py",
        "prompt": "x", "model": "vendor/small-model:free"}))
    assert not errors, errors
    assert jobs[0]["model"] == "vendor/small-model:free"


def test_no_model_means_no_field():
    c = _load("extension_compose", COMPOSE)
    jobs, _, _ = c.synthesize_ops(_rec({
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "s.py"}))
    assert "model" not in jobs[0]


def test_manifest_rejects_non_string_model():
    mod = _load("extension_manifest", MANIFEST)
    errors, _ = mod.validate_manifest(_rec({
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "s.py", "model": 5})["manifest"])
    assert any("model must be a non-empty model id" in e for e in errors), errors


def test_no_tier_means_no_field():
    c = _load("extension_compose", COMPOSE)
    jobs, _, _ = c.synthesize_ops(_rec({
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "score.py"}))
    assert "tier" not in jobs[0]


def test_manifest_rejects_non_string_tier():
    mod = _load("extension_manifest", MANIFEST)
    errors, _ = mod.validate_manifest(_rec({
        "schedule": {"kind": "cron", "expr": "0 4 * * *"}, "entrypoint": "s.py", "tier": 3})["manifest"])
    assert any("tier must be a non-empty stage name" in e for e in errors), errors


def test_kickstart_has_by_tier_slotting():
    body = KICKSTART.read_text(encoding="utf-8")
    assert "def by_tier(" in body
    assert "by_tier(label)" in body          # tier-tagged jobs merged into the stage
