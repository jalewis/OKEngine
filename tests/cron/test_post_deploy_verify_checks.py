"""okengine#1032 [22]: post_deploy_verify runs every registered deployment check, or says why not.

Step [9] used to run a hand-typed list of 12 of the 19 registered checks. Seven were absent and only
the in-container ones were explained, so reserved-basenames, source-identity and tool-search never
ran at deploy time, and the next check added to CHECKS would have been skipped the same way. The
step now runs ``CHECKS`` minus ``deployment_checks.POST_DEPLOY_HOST_EXCLUDED``, a reasoned map of the
checks that need the running gateway.

The step's driver, ``scripts/post_deploy_selfchecks.py`` (okengine#1043), is executed here as the
verifier executes it (``python3 <engine>/scripts/post_deploy_selfchecks.py <deployment>``), from a
stub engine whose ``deployment_checks`` records what it was asked to run, so the derivation is
proven by behavior, not by matching its text.

Each exclusion claims that the check runs in its own in-container step. okengine#1042 [22]: two of
those claims (3b, 5c) were pinned by a COMMENT header, which survives deleting the step itself.
They are now proven by running the verifier against a stub ``docker`` that makes the step's
condition true and asserting the step reports it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "post_deploy_verify.sh"
DRIVER = REPO / "scripts" / "post_deploy_selfchecks.py"
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"

needs_bash = pytest.mark.skipif(not shutil.which("bash"), reason="bash not available")


def _checks():
    spec = importlib.util.spec_from_file_location("deployment_checks_pdv", MOD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_engine(tmp_path: Path, checks: list[str], excluded: dict[str, str],
                 driver: str | None = None) -> Path:
    engine = tmp_path / "engine"
    cron = engine / "scripts" / "cron"
    cron.mkdir(parents=True)
    (cron / "deployment_checks.py").write_text(
        "import json, os\n"
        f"CHECKS = dict.fromkeys({checks!r})\n"
        f"POST_DEPLOY_HOST_EXCLUDED = {excluded!r}\n"
        "def configure(*args, **kwargs):\n"
        "    with open(os.path.join(args[0], 'configured.json'), 'w') as fh:\n"
        "        json.dump({'args': list(args), 'kwargs': kwargs}, fh)\n"
        "def host_env(dep):\n    return {'FROM_PACK_ENV': dep}\n"
        "def run(names):\n    return [('INFO', 'area of ' + name, 'ran') for name in names]\n",
        encoding="utf-8")
    (engine / "scripts" / "post_deploy_selfchecks.py").write_text(
        DRIVER.read_text(encoding="utf-8") if driver is None else driver, encoding="utf-8")
    return engine


def _run_step9(tmp_path: Path, checks: list[str], excluded: dict[str, str],
               driver: str | None = None) -> list[str]:
    """Run step [9]'s driver against a stub engine; return the check names it ran."""
    engine = _stub_engine(tmp_path, checks, excluded, driver)
    proc = subprocess.run(
        [sys.executable, str(engine / "scripts" / "post_deploy_selfchecks.py"), str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60, check=True,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
    rows = [line.split("\t") for line in proc.stdout.splitlines() if line]
    assert rows and rows[-1][0] == "END", f"the driver did not finish:\n{proc.stdout}"
    body = rows[:-1]
    assert all(row[0] == "INFO" for row in body), proc.stdout
    for row in body:
        assert row[2] == f"area of {row[1]}", "a row lost the check that produced it"
    return [row[1] for row in body]


def test_step9_runs_every_registered_check_not_excluded(tmp_path):
    C = _checks()
    ran = _run_step9(tmp_path, list(C.CHECKS), C.POST_DEPLOY_HOST_EXCLUDED)
    assert ran == [name for name in C.CHECKS if name not in C.POST_DEPLOY_HOST_EXCLUDED]
    for name in ("reserved-basenames", "source-identity", "tool-search", "write-principal"):
        assert name in ran, f"{name} is host-readable and must run at deploy time"


def test_step9_configures_the_checks_with_the_host_env(tmp_path):
    """okengine#994 [16]: the checks judge the env compose builds from the pack .env."""
    _run_step9(tmp_path, ["pins"], {})
    configured = json.loads((tmp_path / "configured.json").read_text(encoding="utf-8"))
    assert configured["args"] == [str(tmp_path)]
    assert configured["kwargs"] == {
        "data": str(tmp_path / ".hermes-data"),
        "hermes": str(tmp_path / ".no-baked-image-on-host"),
        "env": {"FROM_PACK_ENV": str(tmp_path)},
    }


def test_every_registered_check_is_run_or_excluded_with_a_reason(tmp_path):
    C = _checks()
    ran = set(_run_step9(tmp_path, list(C.CHECKS), C.POST_DEPLOY_HOST_EXCLUDED))
    for name in C.CHECKS:
        reason = C.POST_DEPLOY_HOST_EXCLUDED.get(name, "")
        assert (name in ran) != bool(reason.strip()), (
            f"{name}: run={name in ran}, exclusion reason={reason!r}; a check is run at deploy "
            "time, or excluded with a reason, never both and never neither")
    assert set(C.POST_DEPLOY_HOST_EXCLUDED) <= set(C.CHECKS), "an exclusion names no check"


def test_a_new_check_runs_at_deploy_time_by_default(tmp_path):
    """The point of deriving the list: registering a check is enough to run it after a deploy."""
    C = _checks()
    ran = _run_step9(tmp_path, [*C.CHECKS, "brand-new-check"], C.POST_DEPLOY_HOST_EXCLUDED)
    assert "brand-new-check" in ran


def test_a_hand_listed_step9_is_caught(tmp_path):
    """Negative fixture: the pre-fix hand list skips registered checks and the detector says so."""
    C = _checks()
    text = DRIVER.read_text(encoding="utf-8")
    derived = "        if name in checks.POST_DEPLOY_HOST_EXCLUDED:\n"
    assert derived in text, "fixture: the driver no longer derives its list this way"
    hand = ('        if name not in ["pins", "schema", "subdomains", "crons", "timezone", '
            '"partition-dups", "rules", "extensions", "provenance", "write-principal", '
            '"operations", "auth"]:\n')
    ran = set(_run_step9(tmp_path, list(C.CHECKS), C.POST_DEPLOY_HOST_EXCLUDED,
                         text.replace(derived, hand)))
    unaccounted = sorted(n for n in C.CHECKS if n not in ran and n not in C.POST_DEPLOY_HOST_EXCLUDED)
    assert unaccounted == ["reserved-basenames", "source-identity", "tool-search"]


# ---- each exclusion's in-container step, proven by running it (okengine#1042 [22]) -------------

def _gateway_docker(bindir: Path, body: str) -> None:
    """A docker whose `compose config --services` lists the gateway and whose execs answer `body`."""
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "docker"
    stub.write_text("#!/bin/sh\ncase \"$*\" in\n" + body +
                    '  *"config --services"*) printf "gateway\\n" ;;\nesac\nexit 0\n',
                    encoding="utf-8")
    stub.chmod(0o755)


def _verify(deployment: Path, script: Path = SCRIPT) -> str:
    (deployment / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    env = {**os.environ, "PATH": f"{deployment / 'bin'}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", str(script)], cwd=deployment, capture_output=True, text=True,
                          timeout=300, env=env)
    return proc.stdout + proc.stderr


# 3b: every baked write-path file hashes differently from its staged copy.
_DRIFTED_DIGESTS = (
    '  *"command -v sha256sum"*) echo digest-ok ;;\n'
    '  *"sha256sum /opt/hermes/"*) echo baked0000 ;;\n'
    '  *"sha256sum /opt/data/"*) echo staged1111 ;;\n'
)
# 5c: the gateway runs as 1003, the runtime dir is owned by 10000.
_DESYNCED_UID = (
    '  *HERMES_UID*) echo 1003 ;;\n'
    '  *"stat -c %u /opt/data/cron-plus"*) echo 10000 ;;\n'
)

# scheduler-dst: the in-gateway run of the shared check reports a FAIL row.
_DST_SHIFTED = (
    '  *scheduler-dst*) printf "FAIL\\tcroniter 6.0.0 mis-computes next fire across DST\\nEND\\n" ;;\n'
)

#: Where each excluded check runs instead, and the verdict that proves the step ran.
IN_CONTAINER_PROBE = {
    "write-path": (_DRIFTED_DIGESTS, "write-path lib schema_lib.py is STALE (baked vs staged)"),
    "runtime-ownership": (_DESYNCED_UID, "runtime /opt/data/cron-plus owned by uid 10000 but the "
                                         "gateway runs as 1003"),
    # These two steps call C.run in the gateway; their markers are executable code, not comments.
    "ownership": (None, 'findings = C.run(["ownership"])'),
    "chat-capability": (None, 'C.run([\\"chat-capability\\"])'),
    # The gateway's croniter fails the DST self-test (6.0.0 shifts next-fire by an hour).
    "scheduler-dst": (_DST_SHIFTED, "[scheduler-dst] croniter 6.0.0 mis-computes"),
}


def test_every_exclusion_has_an_in_container_probe():
    assert set(_checks().POST_DEPLOY_HOST_EXCLUDED) == set(IN_CONTAINER_PROBE)


@needs_bash
@pytest.mark.parametrize("name", [n for n, (body, _) in IN_CONTAINER_PROBE.items() if body])
def test_an_excluded_check_is_run_by_its_in_container_step(tmp_path, name):
    body, verdict = IN_CONTAINER_PROBE[name]
    _gateway_docker(tmp_path / "bin", body)
    out = _verify(tmp_path)
    assert f"\x1b[31mFAIL\x1b[0m  {verdict}" in out, out[-3000:]


@needs_bash
def test_a_scheduler_dst_fail_points_at_the_pinned_plugin_not_carried_patches(tmp_path):
    """Since hermes-cron-plus v0.2.0 the engine carries no cron-plus patches: install-cron-plus.sh
    restores the pinned tree, and the gateway loads the plugin only at start. The FAIL hint must
    say both, not send the operator to re-apply patches that no longer exist."""
    _gateway_docker(tmp_path / "bin", _DST_SHIFTED)
    out = _verify(tmp_path)
    marker = "\x1b[31mFAIL\x1b[0m  [scheduler-dst] croniter 6.0.0 mis-computes"
    assert marker in out, out[-3000:]
    hint = out.split(marker, 1)[1].split("\n", 2)[1]
    assert "install-cron-plus.sh" in hint and "restart" in hint, hint
    assert "carried patches" not in hint, f"stale hint: {hint}"


@pytest.mark.parametrize("name", [n for n, (body, _) in IN_CONTAINER_PROBE.items() if not body])
def test_an_in_gateway_check_step_is_present(name):
    """CANNOT DETECT: whether the step is reached or passes on a live gateway; the marker is the
    C.run call itself, so deleting the step deletes it (unlike a comment header)."""
    assert IN_CONTAINER_PROBE[name][1] in SCRIPT.read_text(encoding="utf-8")


@needs_bash
def test_the_write_path_step_covers_every_write_path_lib(tmp_path):
    """3b must hash the same libs as deployment_checks._WRITE_PATH_LIBS, plus both contracts."""
    _gateway_docker(tmp_path / "bin", _DRIFTED_DIGESTS)
    out = _verify(tmp_path)
    for lib in _checks()._WRITE_PATH_LIBS:
        assert f"write-path lib {lib} is STALE" in out, f"3b does not compare {lib}"
    for name in ("base-schema.yaml", "schema_validator.py"):
        assert f"write-path contract {name} is STALE" in out, f"3b does not compare {name}"


@needs_bash
def test_a_deleted_write_path_step_with_its_comment_kept_is_caught(tmp_path):
    """Negative fixture for okengine#1042 [22]: remove the 3b loop and keep its header. The old pin
    (comment text) stayed green; the behavioral probe must not."""
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("for lib in schema_lib.py id_lib.py id_index.py okf_migrate.py; do")
    end = text.index("done\n", start) + len("done\n")
    gutted = text[:start] + text[end:]
    assert "# 3b. write-path baked-lib drift" in gutted, "fixture: the header must survive"
    copy = tmp_path / "engine" / "scripts" / "post_deploy_verify.sh"
    copy.parent.mkdir(parents=True)
    copy.write_text(gutted, encoding="utf-8")
    deployment = tmp_path / "dep"
    deployment.mkdir()
    _gateway_docker(deployment / "bin", _DRIFTED_DIGESTS)
    assert IN_CONTAINER_PROBE["write-path"][1] not in _verify(deployment, copy)


# ---- 3b with nothing to compare (okengine#1042 [16]) --------------------------------------------

@needs_bash
def test_a_gateway_that_cannot_hash_is_undetectable_not_silent(tmp_path):
    """Exec fails (or the image has no sha256sum): both digests come back empty. That used to print
    nothing at all, in the step that step [9] skips write-path on the strength of."""
    _gateway_docker(tmp_path / "bin", "")
    out = _verify(tmp_path)
    assert "write-path baked-lib drift UNDETECTABLE" in out, out[-3000:]
    assert "write-path lib schema_lib.py is STALE" not in out


@needs_bash
def test_a_write_path_lib_absent_on_both_sides_is_undetectable(tmp_path):
    _gateway_docker(tmp_path / "bin", '  *"command -v sha256sum"*) echo digest-ok ;;\n')
    out = _verify(tmp_path)
    for lib in _checks()._WRITE_PATH_LIBS:
        assert f"cannot compare write-path lib {lib} — absent from BOTH" in out, out[-3000:]
    for name in ("base-schema.yaml", "schema_validator.py"):
        assert f"cannot compare write-path contract {name} — absent from BOTH" in out
    assert "write-path baked-lib drift UNDETECTABLE" not in out


@needs_bash
def test_matching_write_path_digests_report_nothing(tmp_path):
    _gateway_docker(tmp_path / "bin", '  *"command -v sha256sum"*) echo digest-ok ;;\n'
                                      '  *"sha256sum /opt/"*) echo same2222 ;;\n')
    out = _verify(tmp_path)
    assert "write-path lib" not in out and "write-path contract" not in out, out[-3000:]
    assert "UNDETECTABLE — cannot run sha256sum" not in out


@pytest.mark.parametrize("level", ["INFO", "PASS"])
def test_step9_reports_passing_levels(level):
    """tool-search reports a clean result as PASS; step [9] must not drop it on the floor.

    CANNOT DETECT: the rendering of the ok() line on a live run; this proves only that the case
    statement has a branch for the level.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    block = text[text.index('echo "[9] deployment self-checks'):]
    branch = next(line for line in block.splitlines() if 'ok  "[$area] $msg"' in line)
    assert level in [part.strip() for part in branch.split(")")[0].split("|")]
