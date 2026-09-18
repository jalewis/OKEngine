"""Regression tests for the extension cron composer (#113).

Guards docs/design/extension-lifecycle.md: an enabled `operation` extension
synthesizes one namespaced, deterministic job; sidecar/image and non-operation
kinds emit no job (deferred / out of scope); conflicts fail loud.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent.parent
MOD_PATH = REPO / "scripts" / "extension_compose.py"

pytestmark = pytest.mark.skipif(not MOD_PATH.is_file(),
                                reason="extension_compose.py not present")


def _mod():
    spec = importlib.util.spec_from_file_location("extension_compose", MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["extension_compose"] = m
    spec.loader.exec_module(m)
    return m


def _record(ext_id="demo.alpha", **op_over):
    operation = {"schedule": {"kind": "cron", "expr": "17 5 * * *"},
                 "entrypoint": {"script": "run.py"}, "timeout": 1800}
    operation.update(op_over)
    man = {"id": ext_id, "kind": "operation", "version": "0.1.0",
           "trust": "in-gateway", "requires": {"engine": ">=0.3.0"},
           "capabilities": {"read": ["wiki/**"], "write": ["alpha/**"]},
           "operation": operation}
    return {"id": ext_id, "tier": "pack", "dir": f"/x/{ext_id}", "manifest": man}


def _agent_record(ext_id="demo.agent"):
    contract = {
        "api": 1, "allowed_namespaces": ["alpha"], "allowed_types": ["entity"],
        "operations": ["create"], "required_fields": ["type"],
        "required_relationships": [], "body": {"required": True, "min_non_whitespace": 40},
        "unknown_fields": "reject", "unresolved_links": "reject",
        "placeholder_links": "reject", "completion": "run",
    }
    return _record(ext_id, prompt="write alpha", output_contract=contract,
                   adversarial_fixtures=["tests/extensions/test_compose.py"])


def test_agent_operation_requires_contract_and_adversarial_fixtures():
    m = _mod()
    bad = _record("demo.bad-agent", prompt="write alpha")
    job, errors, _ = m.synthesize_job(bad)
    assert job is None and any("output_contract" in error for error in errors)

    job, errors, _ = m.synthesize_job(_agent_record())
    assert errors == []
    assert job["output_contract"]["allowed_namespaces"] == ["alpha"]
    assert job["adversarial_fixtures"] == ["tests/extensions/test_compose.py"]


def test_operation_synthesizes_namespaced_job():
    m = _mod()
    job, errors, warnings = m.synthesize_job(_record("demo.alpha"))
    assert errors == []
    assert job["name"] == "demo.alpha"            # namespaced by construction
    assert job["script"] == "/opt/data/scripts/demo.alpha/run.py"   # namespaced staging path (#128)
    assert job["no_agent"] is True
    assert job["schedule"] == {"kind": "cron", "expr": "17 5 * * *"}
    assert "okengine-write" in job["enabled_toolsets"]
    assert len(job["id"]) == 12                    # deterministic id


def test_job_id_is_deterministic():
    m = _mod()
    a, _, _ = m.synthesize_job(_record("demo.alpha"))
    b, _, _ = m.synthesize_job(_record("demo.alpha"))
    assert a["id"] == b["id"]                       # reproducible from the manifest


def test_bare_string_entrypoint_supported():
    m = _mod()
    job, errors, _ = m.synthesize_job(_record("demo.alpha", entrypoint="run.py"))
    assert errors == []
    assert job["script"] == "/opt/data/scripts/demo.alpha/run.py"


def test_entrypoint_basename_only_in_staging_path():
    """A path-y entrypoint is reduced to its basename under the namespaced dir."""
    m = _mod()
    job, errors, _ = m.synthesize_job(_record("demo.alpha", entrypoint={"script": "sub/run.py"}))
    assert errors == []
    assert job["script"] == "/opt/data/scripts/demo.alpha/run.py"


def test_sidecar_without_image_is_error():
    m = _mod()
    rec = _record("demo.sidecar")
    rec["manifest"]["trust"] = "sidecar"          # but entrypoint is still a script
    job, errors, warnings = m.synthesize_job(rec)
    assert job is None
    assert any("image" in e for e in errors)


def test_sidecar_with_image_does_not_schedule_unreachable_trigger():
    m = _mod()
    rec = _record("demo.img")
    rec["manifest"]["trust"] = "sidecar"
    rec["manifest"]["operation"]["entrypoint"] = {"image": {
        "registry": "reg.example.com/demo.img", "tag": "0.1.0", "digest": "sha256:abc"}}
    job, errors, warnings = m.synthesize_job(rec)
    assert errors == []
    assert job is None
    assert any("automatic scheduling is disabled" in warning for warning in warnings)


def test_non_operation_kind_emits_no_job():
    m = _mod()
    rec = _record("demo.reader")
    rec["manifest"]["kind"] = "reader-extension"
    job, errors, warnings = m.synthesize_job(rec)
    assert job is None and errors == []
    assert warnings


def test_missing_operation_block_is_error():
    m = _mod()
    rec = _record("demo.bad")
    del rec["manifest"]["operation"]
    job, errors, _ = m.synthesize_job(rec)
    assert job is None
    assert any("operation" in e for e in errors)


def test_bad_schedule_is_error():
    m = _mod()
    job, errors, _ = m.synthesize_job(_record("demo.alpha", schedule={"kind": "interval"}))
    assert job is None
    assert any("schedule" in e for e in errors)


def test_compose_rejects_collision_with_engine_pack_job():
    m = _mod()
    resolved = {"demo.alpha": _record("demo.alpha")}
    jobs, errors, _ = m.compose(resolved, existing_names={"demo.alpha"})
    assert any("collides" in e for e in errors)


def test_compose_clean_when_no_collision():
    m = _mod()
    resolved = {"demo.alpha": _record("demo.alpha"), "demo.bravo": _record("demo.bravo")}
    jobs, errors, _ = m.compose(resolved, existing_names={"build-hot-set"})
    assert errors == []
    assert {j["name"] for j in jobs} == {"demo.alpha", "demo.bravo"}


# --- okengine#561: declared operation fields must survive composition ---------------------------
def test_declared_timeout_reaches_the_generated_job():
    """cron-plus resolves job.get("timeout", $OKENGINE_AGENT_RUN_TIMEOUT_SECONDS, 1200), so a
    timeout that never reaches the job silently becomes 1200s with nothing reporting it.

    Measured on okcti-test before the fix: 5 extension cron defs declared 300-3600s and 0 of 27
    extension-sourced jobs carried a timeout. One lane declared 600 and was killed at 1200 on
    eight consecutive runs across two days. Note this file's own fixture has declared
    `timeout: 1800` since it was written -- nothing ever asserted it arrived.
    """
    m = _mod()
    job, errors, _ = m.synthesize_job(_record())
    assert not errors, errors
    assert job["timeout"] == 1800, f"declared timeout dropped during composition: {job}"


def test_absent_timeout_leaves_the_runner_default_in_charge():
    """Omitting it must NOT stamp a value -- absent means "inherit", and inventing a number here
    would silently override the deployment-wide default."""
    m = _mod()
    rec = _record()
    del rec["manifest"]["operation"]["timeout"]
    job, errors, _ = m.synthesize_job(rec)
    assert not errors, errors
    assert "timeout" not in job


@pytest.mark.parametrize("bad", [0, -5, "abc", 1.5, True, [1800]])
def test_a_declared_but_invalid_timeout_is_an_error_not_a_silent_fallback(bad):
    """The whole defect class is a declaration nobody honours. Falling back to the default on a
    malformed value would reproduce it exactly -- fail loudly instead."""
    m = _mod()
    job, errors, _ = m.synthesize_job(_record(timeout=bad))
    assert job is None
    assert any("timeout" in e for e in errors), errors


def test_every_carried_operation_field_round_trips():
    """The general form (okengine#561): the job dict is an explicit allowlist, so ANY optional
    field can be forgotten silently. Pin the ones the composer is meant to carry, so the next
    addition that gets dropped fails here rather than in production a week later.
    """
    m = _mod()
    job, errors, _ = m.synthesize_job(_record(tier="kickstart", model="cheap-model", timeout=900))
    assert not errors, errors
    for field, expected in (("tier", "kickstart"), ("model", "cheap-model"), ("timeout", 900)):
        assert job.get(field) == expected, f"{field} did not survive composition: {job}"
