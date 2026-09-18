"""Producer -> consumer contract for observability-validation (okengine#757).

observability-validation never passed on any deployment: every hand-written fixture in its unit
tests described artifacts in shapes the real producers do not write (percentile keys the read-MCP
omits until a bucket has samples, a verdict line a silent no_agent run never logs, a snapshot that
never moves). This drives the REAL writers — the read-MCP telemetry publisher, deployment_validate's
report, fleet_health — and asserts the REAL validator accepts what they produce, and still rejects a
genuine disagreement.
"""
import contextlib
import importlib.util
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# server.py imports `mcp` at module level; same pattern as the other read-MCP tests. CI installs it.
pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Deployment:
    """A throwaway deployment: the read-MCP writer plus the gateway-side lanes, all on tmp paths."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.monkeypatch = tmp_path, monkeypatch
        self.vault, self.data = tmp_path / "vault", tmp_path / "data"
        self.telemetry = self.data / "qmd" / "search-telemetry.json"
        self.logs = self.data / "logs" / "cron-plus"
        self.jobs = self.data / "cron-plus" / "jobs.json"
        for directory in (self.vault / "wiki", self.logs, self.jobs.parent, self.data / "metrics"):
            directory.mkdir(parents=True, exist_ok=True)
        self.jobs.write_text(json.dumps({"jobs": [{
            "name": "deployment-validate", "enabled": True,
            "schedule": {"kind": "cron", "expr": "10 12 * * *"}, "no_agent": True}]}))
        for key, value in {
            "WIKI_PATH": self.vault, "HERMES_HOME": self.data, "CRON_JOBS": self.jobs,
            "CRON_LOGS": self.logs, "OKENGINE_QMD_STATS": self.telemetry,
            "OKENGINE_MEMORY_EVENTS": tmp_path / "memory.events",
            "OKENGINE_QUALITY_ADJUDICATION": tmp_path / "adjudication.json",
        }.items():
            monkeypatch.setenv(key, str(value))
        self.server = _load("okengine_server_observability", REPO / "okengine-mcp" / "server.py")
        monkeypatch.setattr(self.server, "_QMD_STATS_PATH", self.telemetry)
        monkeypatch.setattr(self.server, "_QMD_STATS", {
            "search": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
            "maintenance": {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []},
            "saturated": {"search": 0, "maintenance": 0},
        })

    def publish(self, at: float, **calls):
        """The read-MCP recording calls and republishing, with its clock pinned to `at`."""
        for spec, count in calls.items():
            kind, outcome = spec.split("_", 1)
            for index in range(count):
                self.server._record_qmd(kind, outcome, None if outcome == "saturated" else 100 + index)
        stamp = datetime.fromtimestamp(at, tz=timezone.utc)
        self.monkeypatch.setattr(self.server, "datetime", type(
            "PinnedDatetime", (datetime,), {"now": classmethod(lambda cls, tz=None: stamp)}))
        self.server._publish_qmd_stats()

    def restart_read_mcp(self):
        for kind in ("search", "maintenance"):
            self.server._QMD_STATS[kind] = {"ok": 0, "timeouts": 0, "errors": 0, "latency_ms": []}
        self.server._QMD_STATS["saturated"] = {"search": 0, "maintenance": 0}

    def deployment_validate(self, findings):
        """A no_agent deployment-validate run: the real report writer, and the log cron-plus keeps.

        On exit 0 the run is silent and its stdout is discarded; on exit 1 the runner embeds the
        stdout in its error line (both shapes captured from live gateways).
        """
        started = time.time() - 30
        stamp = datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        validate = _load("deployment_validate_observability",
                         REPO / "scripts" / "cron" / "deployment_validate.py")
        self.monkeypatch.setattr(validate.C, "VAULT", self.vault)
        self.monkeypatch.setattr(validate.C, "run", lambda names=None: list(findings))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = validate.main()
        log = self.logs / f"deployment-validate-{stamp}.log"
        if code == 0:
            log.write_text(
                "INFO cron.scheduler: Job '70c0b69cc497' (no_agent): wakeAgent=false gate — silent run\n"
                "INFO cron-plus.runner: agent returned [SILENT] — skipping delivery\n")
        else:
            log.write_text(
                "ERROR cron-plus.runner: agent run failed: Script exited with code "
                f"{code}\nstdout:\n{stdout.getvalue()}")

    def fleet_health(self):
        fleet = _load("fleet_health_observability", REPO / "scripts" / "cron" / "fleet_health.py")
        assert fleet.main() == 0
        return json.loads((self.vault / "wiki" / "dashboards" / ".fleet-lanes.json").read_text())

    def validate(self):
        validator = _load("observability_validate_contract",
                          REPO / "scripts" / "cron" / "observability_validate.py")
        wiki = self.vault / "wiki"
        (self.data / "metrics" / "okengine.prom").write_text(
            "okengine_health_export_timestamp_seconds 100\nokengine_health_monitor_stale 0\n")
        for name, value in {
            "VAULT": self.vault, "DATA": self.data, "WIKI": wiki,
            "FLEET": wiki / "dashboards" / "fleet-health.md",
            "LANES": wiki / "dashboards" / ".fleet-lanes.json", "SEARCH": self.telemetry,
            "DEPLOYMENT": wiki / "operational" / "deployment-validation.md",
            "METRICS": self.data / "metrics" / "okengine.prom",
            "OUTPUT": wiki / "operational" / "observability-validation.md",
        }.items():
            self.monkeypatch.setattr(validator, name, value)
        return validator.validate()


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    return _Deployment(tmp_path, monkeypatch)


def test_a_freshly_restarted_read_mcp_with_no_samples_validates(deployment):
    deployment.publish(time.time() - 60)
    deployment.deployment_validate([])
    deployment.fleet_health()
    assert deployment.validate() == []


def test_live_traffic_that_keeps_counting_after_fleet_health_validates(deployment):
    """The #757 failure on every live pack: the artifact moved on after the snapshot."""
    deployment.publish(time.time() - 60, search_ok=13, maintenance_ok=72)
    deployment.deployment_validate([("WARN", "pins", "advisory")])
    lanes = deployment.fleet_health()
    assert "deployment-validate" in lanes["ok"]
    deployment.publish(time.time() + 120, search_ok=2, maintenance_ok=7, maintenance_saturated=1)
    assert deployment.validate() == []


def test_an_unchanged_artifact_that_disagrees_with_its_snapshot_is_still_caught(deployment):
    deployment.publish(time.time() - 60, search_ok=4, maintenance_errors=2)
    deployment.deployment_validate([])
    deployment.fleet_health()
    artifact = json.loads(deployment.telemetry.read_text())
    artifact["search"]["calls"] += 1
    deployment.telemetry.write_text(json.dumps(artifact))
    assert deployment.validate() == ["fleet search snapshot disagrees with the qmd artifact it read"]


def test_a_read_mcp_restart_after_the_snapshot_is_reported(deployment):
    deployment.publish(time.time() - 60, search_ok=5, maintenance_ok=5)
    deployment.deployment_validate([])
    deployment.fleet_health()
    deployment.restart_read_mcp()
    deployment.publish(time.time() + 120, maintenance_ok=1)
    assert deployment.validate() == [
        "qmd artifact counters went backwards since the fleet snapshot (read-MCP restart, or "
        "fleet-health read a different file)"]


def test_a_failing_deployment_validate_is_visible_and_consistent(deployment):
    deployment.publish(time.time() - 60, search_ok=1, maintenance_ok=1)
    deployment.deployment_validate([("FAIL", "projection", "count drift")])
    lanes = deployment.fleet_health()
    assert "deployment-validate" in lanes["errored"]
    assert deployment.validate() == []
