"""okengine#405: contract for the shared deployment checks library (scripts/cron/deployment_checks.py).

The 13 daily-validator checks + the doctor-only operation-runs check were extracted here so the
in-gateway validator, `framework status`/`framework doctor`, and post_deploy_verify.sh all run the
SAME checks. These tests pin the shared DRIVER (registry, selection, isolation, configure) — the
individual check behaviors keep their existing coverage in test_deployment_validate.py.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"


def _load():
    spec = importlib.util.spec_from_file_location("deployment_checks", MOD)
    m = importlib.util.module_from_spec(spec)
    sys.modules["deployment_checks"] = m
    spec.loader.exec_module(m)
    return m


def test_registry_covers_the_thirteen_validator_checks_plus_operations():
    C = _load()
    # every daily-validator check is registered, in order; operations is the doctor-only extra
    assert C.VALIDATE_CHECKS == [
        "pins", "schema", "subdomains", "crons", "timezone", "partition-dups", "rules",
        "extensions", "ownership", "runtime-ownership", "auth", "write-path", "provenance"]
    assert "operations" in C.CHECKS and "operations" not in C.VALIDATE_CHECKS
    assert set(C.VALIDATE_CHECKS) | {"operations"} == set(C.CHECKS)


def test_run_default_is_the_validator_set_and_omits_operations(tmp_path):
    C = _load()
    (tmp_path / "wiki").mkdir()
    C.configure(tmp_path, data=tmp_path / "nodata", hermes=tmp_path / "nohermes")
    findings = C.run()                      # default = VALIDATE_CHECKS
    areas = {a for _, a, _ in findings}
    assert "operations" not in areas        # doctor-only, never in the daily lane
    assert isinstance(findings, list)


def test_run_selects_only_requested_checks(tmp_path):
    C = _load()
    (tmp_path / "wiki").mkdir()
    (tmp_path / "schema.yaml").write_text("types: {actor: {}}\ntype_aliases: {actor: actor}\n")
    C.configure(tmp_path, data=tmp_path / "nodata", hermes=tmp_path / "nohermes")
    findings = C.run(["schema"])
    assert {a for _, a, _ in findings} <= {"schema"}
    # the representative schema check still fires post-extraction (type_alias shadow FAIL)
    assert any(l == "FAIL" and "SHADOWS" in m for l, _, m in findings)


def test_run_isolates_a_crashing_check_as_a_fail(tmp_path):
    C = _load()
    (tmp_path / "wiki").mkdir()
    C.configure(tmp_path)
    C.CHECKS["boom"] = lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))
    try:
        findings = C.run(["boom"])
    finally:
        del C.CHECKS["boom"]
    assert any(l == "FAIL" and a == "validator" and "boom crashed" in m for l, a, m in findings)


def test_reset_clears_prior_findings(tmp_path):
    C = _load()
    (tmp_path / "wiki").mkdir()
    C.configure(tmp_path, data=tmp_path / "nodata", hermes=tmp_path / "nohermes")
    C.run(["provenance"])
    assert C.F                               # provenance WARN accumulated
    C.reset()
    assert C.F == []


@pytest.mark.parametrize("environment", [
    ["OKENGINE_PACK=okpack-test"],
    {"OKENGINE_PACK": "okpack-test"},
])
def test_host_provenance_check_reads_gateway_compose_environment(
        tmp_path, monkeypatch, environment):
    C = _load()
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "docker-compose.yml").write_text(
        pytest.importorskip("yaml").safe_dump({
            "services": {"gateway": {"environment": environment}},
        }))
    C.configure(tmp_path)
    assert C.run(["provenance"]) == []


def test_provenance_check_warns_when_env_and_compose_both_omit_pack(tmp_path, monkeypatch):
    C = _load()
    monkeypatch.delenv("OKENGINE_PACK", raising=False)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "docker-compose.yml").write_text("services: {gateway: {environment: []}}\n")
    C.configure(tmp_path)
    findings = C.run(["provenance"])
    assert any(level == "WARN" and area == "provenance"
               for level, area, _message in findings)


def test_configure_rebinds_the_paths_the_checks_read(tmp_path):
    C = _load()
    v = tmp_path / "vault"; (v / "wiki").mkdir(parents=True)
    C.configure(v, data=tmp_path / "d", hermes=tmp_path / "h")
    assert C.VAULT == v and C.DATA == tmp_path / "d" and C.HERMES == tmp_path / "h"


def test_operation_runs_flags_stuck_and_reports_failed(tmp_path):
    C = _load()
    (tmp_path / "wiki").mkdir()
    runs = tmp_path / ".okengine" / "operations" / "runs" / "actor-review"
    runs.mkdir(parents=True)
    (runs / "r1.json").write_text(json.dumps(
        {"operation": "actor-review", "run_id": "r1", "status": "running", "pid": 999999}))
    (runs / "r2.json").write_text(json.dumps(
        {"operation": "actor-review", "run_id": "r2", "status": "failed"}))
    (runs / "r3.json").write_text(json.dumps(
        {"operation": "actor-review", "run_id": "r3", "status": "succeeded"}))
    C.configure(tmp_path, data=tmp_path / "nodata", hermes=tmp_path / "nohermes")
    findings = C.run(["operations"])
    assert any(l == "WARN" and a == "operations" and "stuck" in m and "r1" in m
               for l, a, m in findings)
    assert any(l == "INFO" and a == "operations" and "r2" in m for l, a, m in findings)


def test_operation_runs_silent_when_no_receipts(tmp_path):
    C = _load()
    (tmp_path / "wiki").mkdir()
    C.configure(tmp_path, data=tmp_path / "nodata", hermes=tmp_path / "nohermes")
    assert not [f for f in C.run(["operations"]) if f[1] == "operations"]
