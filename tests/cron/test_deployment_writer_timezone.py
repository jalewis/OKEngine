"""okengine#1042 [36]: the `timezone` check WARNs when a write server dates its ledger in a zone
other than the gateway's.

Hermes never passes the gateway's TZ to a stdio writer, so a writer's ledger day comes from its own
`env:` map. ensure-runtime binds it; this check catches a config.yaml it has not reconciled."""
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent.parent
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"
_ARGS = ["/opt/hermes/okengine-mcp/write_server.py"]


def _run(tmp_path, servers, tz, config=True):
    spec = importlib.util.spec_from_file_location("deployment_checks_1042_tz", MOD)
    C = importlib.util.module_from_spec(spec)
    sys.modules["deployment_checks_1042_tz"] = C
    spec.loader.exec_module(C)
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    if config is True:
        (data / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}))
    elif config is not None:
        (data / "config.yaml").write_text(config)
    C.configure(vault, data=data, hermes=tmp_path / "hermes", env={} if tz is None else {"TZ": tz})
    C.check_timezone()
    return [(level, msg) for level, area, msg in C.F if area == "timezone"]


def _writers(**envs):
    servers = {name: {"command": "python", "args": list(_ARGS), "env": env}
               for name, env in envs.items()}
    servers["okengine"] = {"url": "http://okengine-mcp:8730/mcp"}       # a reader is not judged
    return servers


def test_writers_bound_to_the_gateway_zone_pass(tmp_path):
    servers = _writers(**{"okengine-write": {"TZ": "America/New_York"},
                          "okengine-write-chat": {"TZ": "America/New_York"}})
    assert _run(tmp_path, servers, "America/New_York") == []


def test_a_writer_without_the_gateway_zone_warns(tmp_path):
    """The negative fixture: the live shape, a writer with no TZ under a New York gateway."""
    servers = _writers(**{"okengine-write": {"TZ": "America/New_York"},
                          "okengine-write-entity-backfill": {"OKENGINE_WRITE_ACTOR": "cron:x"},
                          "okengine-write-chat": {"TZ": "Europe/Berlin"}})
    findings = _run(tmp_path, servers, "America/New_York")
    assert len(findings) == 1 and findings[0][0] == "WARN", findings
    msg = findings[0][1]
    assert "2 write server(s)" in msg
    assert "okengine-write-chat (Europe/Berlin)" in msg and "okengine-write-entity-backfill (UTC)" in msg
    assert "ensure-runtime" in msg


@pytest.mark.parametrize("tz, env", [(None, {}), ("UTC", {}), ("", {"TZ": "utc"}), (None, {"TZ": "UTC"})])
def test_utc_spellings_agree(tmp_path, tz, env):
    assert _run(tmp_path, _writers(**{"okengine-write": env}), tz) == []


def test_more_than_four_drifted_writers_are_elided(tmp_path):
    servers = _writers(**{f"okengine-write-{i}": {} for i in range(6)})
    (finding,) = _run(tmp_path, servers, "America/New_York")
    assert "6 write server(s)" in finding[1] and " …" in finding[1]


@pytest.mark.parametrize("config", [None, "- not a mapping\n"])
def test_no_readable_config_is_not_judged(tmp_path, config):
    assert _run(tmp_path, {}, "America/New_York", config=config) == []


def test_an_invalid_zone_fails_before_judging_writers(tmp_path):
    findings = _run(tmp_path, _writers(**{"okengine-write": {}}), "Mars/Olympus")
    assert [level for level, _ in findings] == ["FAIL"]


def test_a_writer_ensure_runtime_does_not_bind_is_not_judged(tmp_path):
    """Review F5: the check and ensure-runtime select the same writers, so the WARN's advice
    (re-run ensure-runtime) can always clear it."""
    servers = _writers(**{"custom-writer": {}, "okengine-writer": {},
                          "okengine-write-x": {"TZ": "America/New_York"}})
    assert _run(tmp_path, servers, "America/New_York") == []


# --- the zone compose gives the gateway: `TZ=${TZ:-UTC}` (review F4) ---------------------------

def test_host_env_takes_tz_as_the_gateway_gets_it(tmp_path):
    """Host-side checks (framework doctor, post_deploy_verify) judge the gateway's TZ, which the
    service's `environment:` interpolates from the shell before the .env."""
    spec = importlib.util.spec_from_file_location("deployment_checks_1042_host", MOD)
    C = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(C)
    (tmp_path / ".env").write_text("TZ=America/New_York\nOTHER=1\n")
    assert C.host_env(tmp_path, {})["TZ"] == "America/New_York"
    env = C.host_env(tmp_path, {"TZ": "Europe/Paris"})
    assert env["TZ"] == "Europe/Paris" and env["OTHER"] == "1"
    assert C.host_env(tmp_path / "nowhere", {})["TZ"] == "UTC"


def test_this_file_is_in_the_deployment_checks_mutation_target():
    """okengine#1044 [15]: these are the only tests that assert `_check_writer_timezones`. The
    target's other test files execute it without asserting on it, so with this file outside the
    target's test_command every mutant of the check survived a full-command campaign."""
    import json
    import shlex
    targets = json.loads((REPO / "mutation" / "targets.json").read_text())
    targets = targets if isinstance(targets, list) else targets["targets"]
    cmd = next(t["test_command"] for t in targets if t["path"] == "scripts/cron/deployment_checks.py")
    assert "tests/cron/test_deployment_writer_timezone.py" in shlex.split(cmd), cmd
