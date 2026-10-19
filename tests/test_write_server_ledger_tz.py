"""okengine#1044 [16]: the stdio write server dates its wiki/log.md ledger lines in the zone its
own process TZ names.

okengine#1042 [36] binds the gateway's TZ into every `okengine-write*` env map, because Hermes hands
a stdio server only a safe baseline plus that map. The tests for it check the map (ensure-runtime)
and the WARN (deployment `timezone` check); nothing checked the consumer: that a write server
started with that env writes the local day. A refactor of `authorization._today()` to UTC, or to a
clock helper that ignores TZ, kept every one of those tests green.

Each write runs in its own interpreter, started with an env map shaped like the one Hermes gives a
stdio writer (no OKENGINE_MCP_WRITE_DATE pin), because TZ is read when the process converts the
clock. The two zones are UTC+14 and UTC-12, written as POSIX TZ strings so no tz database is
needed: their calendar days differ at every instant, so a writer that ignores TZ writes the same
day for both and fails whatever the time of day.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parent.parent
WRITE_SERVER = REPO / "okengine-mcp" / "write_server.py"

_SCHEMA = "okf:\n  required: [type]\ntypes:\n  entity:\n    required: [type, name]\n"

# Load write_server.py by path and make one governed create, as a stdio writer would serve it.
_WRITE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("write_server", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["write_server"] = m
spec.loader.exec_module(m)
res = m._create("entities/vendor/acme", "type: entity\\nname: Acme Corp", "Acme is a vendor.")
assert res.startswith("created"), res
"""

# POSIX TZ: the sign is inverted, so `<+14>-14` is UTC+14 and `<-12>+12` is UTC-12.
ZONES = {"<+14>-14": timedelta(hours=14), "<-12>+12": timedelta(hours=-12)}


def _local_days(offset: timedelta) -> set[str]:
    return {datetime.now(timezone(offset)).date().isoformat()}


def _ledger_day(tmp_path: Path, tz: str) -> tuple[str, set[str]]:
    vault = tmp_path / tz.replace("<", "").replace(">", "").replace("+", "p").replace("-", "m")
    (vault / "wiki").mkdir(parents=True)
    (vault / "schema.yaml").write_text(_SCHEMA, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(vault), "LANG": "C.UTF-8",
           "TZ": tz, "WIKI_PATH": str(vault),
           # The image installs the okengine package and scripts/cron beside the server; a checkout
           # supplies the same modules from its source tree.
           "PYTHONPATH": os.pathsep.join(str(REPO / p) for p in ("src", "okengine-mcp", ".",
                                                                 "scripts/cron"))}
    expected = _local_days(ZONES[tz])
    proc = subprocess.run([sys.executable, "-c", _WRITE, str(WRITE_SERVER)], env=env, cwd=vault,
                          capture_output=True, text=True, timeout=120)
    expected |= _local_days(ZONES[tz])      # a local midnight during the run allows either day
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = [ln for ln in (vault / "wiki" / "log.md").read_text().splitlines()
             if "mcp-write create" in ln or "entities/vendor/acme.md v1" in ln]
    assert lines, (vault / "wiki" / "log.md").read_text()
    return lines[-1].split()[1], expected


def test_the_ledger_day_is_the_local_day_of_the_writers_tz(tmp_path):
    for tz in ZONES:
        day, expected = _ledger_day(tmp_path, tz)
        assert day in expected, f"TZ={tz}: ledger dated {day}, local day is {sorted(expected)}"


def test_writers_in_zones_a_day_apart_date_the_same_write_differently(tmp_path):
    """The UTC regression, caught at any time of day: UTC+14 and UTC-12 never share a date."""
    days = {tz: _ledger_day(tmp_path, tz)[0] for tz in ZONES}
    assert len(set(days.values())) == 2, f"both writers dated the ledger {days}: TZ is ignored"


def test_this_file_is_in_the_authorization_mutation_target():
    """These are the only tests that assert `_today()` follows TZ (every other write-path test pins
    OKENGINE_MCP_WRITE_DATE), so the target's campaign must run them or the UTC mutant survives."""
    import json
    import shlex
    targets = json.loads((REPO / "mutation" / "targets.json").read_text())
    targets = targets if isinstance(targets, list) else targets["targets"]
    cmd = next(t["test_command"] for t in targets
               if t["path"] == "src/okengine/write_services/authorization.py")
    assert "tests/test_write_server_ledger_tz.py" in shlex.split(cmd), cmd
