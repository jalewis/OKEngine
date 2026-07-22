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


def test_sidecar_with_image_yields_trigger_job():
    m = _mod()
    rec = _record("demo.img")
    rec["manifest"]["trust"] = "sidecar"
    rec["manifest"]["operation"]["entrypoint"] = {"image": {
        "registry": "reg.example.com/demo.img", "tag": "0.1.0", "digest": "sha256:abc"}}
    job, errors, _ = m.synthesize_job(rec)
    assert errors == []
    assert job["name"] == "demo.img"
    assert job["script"] == "/opt/data/scripts/demo.img/trigger.sh"   # generated wrapper (#135)
    assert job["no_agent"] is True


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
