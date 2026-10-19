"""deployment_checks ``scheduler-dst``: the gateway's croniter must pass the DST self-test.

The check runs ``scripts/cron/croniter_dst_selftest.py`` under ``HERMES/.venv/bin/python -I``, the
interpreter cron-plus runs in, never under this process's interpreter: a host-side doctor run would
otherwise judge the host's croniter (6.2.4) and report a pass for a gateway carrying 6.0.0.

The gateway interpreter is faked by a shell wrapper that runs the real self-test on a real Python
with a stub ``croniter`` first on the path, so each verdict comes from the self-test's own exit
status and output.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MOD = REPO / "scripts" / "cron" / "deployment_checks.py"


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_stub_dir = _sibling("test_croniter_dst_selftest")._stub_dir


def _load():
    spec = importlib.util.spec_from_file_location("deployment_checks_dst", MOD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["deployment_checks_dst"] = module
    spec.loader.exec_module(module)
    return module


def _gateway(tmp_path: Path, stub: Path | None, body: str | None = None) -> Path:
    """A fake /opt/hermes whose .venv/bin/python runs the self-test with ``stub`` on the path. It
    drops the leading interpreter flags (-I), as a stub path is only reachable without isolation."""
    hermes = tmp_path / "hermes"
    bindir = hermes / ".venv" / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    if body is None:
        body = ('while [ "${1#-}" != "$1" ]; do shift; done\n'
                f'PYTHONPATH="{stub}" exec "{sys.executable}" "$@"\n')
    py.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    py.chmod(0o755)
    return hermes


def _run(tmp_path: Path, hermes: Path, monkeypatch, selftest: Path | None = None):
    C = _load()
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    C.configure(tmp_path / "vault", data=tmp_path / "data", hermes=hermes, env=env)
    if selftest is not None:
        monkeypatch.setattr(C, "_DST_SELFTEST", selftest)
    return C.run(["scheduler-dst"])


def test_a_croniter_with_the_600_shift_fails(tmp_path, monkeypatch):
    found = _run(tmp_path, _gateway(tmp_path, _stub_dir(tmp_path, "shifted")), monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("FAIL", "scheduler-dst")], found
    msg = found[0][2]
    assert "2026-11-01T08:00:00-05:00" in msg, "the observed wrong fire must be named"
    assert "build-engine-image.sh" in msg, "the remedy (rebuild the image) must be named"


def test_a_correct_croniter_is_reported_as_info(tmp_path, monkeypatch):
    found = _run(tmp_path, _gateway(tmp_path, _stub_dir(tmp_path, "correct")), monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("INFO", "scheduler-dst")], found
    assert "stub-correct" in found[0][2]


def test_a_gateway_without_croniter_fails(tmp_path, monkeypatch):
    found = _run(tmp_path, _gateway(tmp_path, _stub_dir(tmp_path, "missing")), monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("FAIL", "scheduler-dst")], found
    assert "croniter is not importable" in found[0][2]


def test_no_gateway_interpreter_is_undetectable(tmp_path, monkeypatch):
    """Host side (doctor, post-deploy step 9) there is no baked venv: WARN, never a pass."""
    found = _run(tmp_path, tmp_path / "no-baked-image", monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("WARN", "scheduler-dst")], found
    assert "undetectable, not a pass" in found[0][2]


def test_a_missing_selftest_script_is_undetectable(tmp_path, monkeypatch):
    hermes = _gateway(tmp_path, _stub_dir(tmp_path, "correct"))
    found = _run(tmp_path, hermes, monkeypatch, selftest=tmp_path / "absent.py")
    assert [(lvl, area) for lvl, area, _ in found] == [("WARN", "scheduler-dst")], found
    assert "absent.py" in found[0][2]


@pytest.mark.parametrize("body, needle", [
    ("echo 'Traceback: boom' >&2\nexit 1\n", "exit 1"),
    ("exit 0\n", "exit 0"),                       # rc 0 with no PASS line is not a pass
    ("printf 'UNDETECTABLE\\tno tz database\\n'\nexit 12\n", "no tz database"),
])
def test_an_unexpected_selftest_result_is_undetectable(tmp_path, monkeypatch, body, needle):
    found = _run(tmp_path, _gateway(tmp_path, None, body), monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("WARN", "scheduler-dst")], found
    assert needle in found[0][2] and "undetectable, not a pass" in found[0][2]


def test_a_hung_selftest_is_undetectable(tmp_path, monkeypatch):
    C = _load()
    monkeypatch.setattr(C, "_DST_SELFTEST_TIMEOUT", 0.5)
    C.configure(tmp_path / "vault", data=tmp_path / "data",
                hermes=_gateway(tmp_path, None, "sleep 5\n"), env=dict(os.environ))
    found = C.run(["scheduler-dst"])
    assert [(lvl, area) for lvl, area, _ in found] == [("WARN", "scheduler-dst")], found
    assert "timed out" in found[0][2]


def test_the_check_isolates_the_gateway_interpreter(tmp_path, monkeypatch):
    """-I: the gateway's PYTHONPATH/user site must not pick the croniter cron-plus does not use."""
    hermes = _gateway(tmp_path, None,
                      'printf "PASS\\targs=%s\\n" "$*"\nexit 0\n')
    found = _run(tmp_path, hermes, monkeypatch)
    assert found[0][0] == "INFO" and "args=-I " in found[0][2], found


def test_the_check_writes_no_bytecode(tmp_path, monkeypatch):
    """-B (okengine#1064): verify runs this as root in the gateway, so an import that writes .pyc
    leaves root-owned files in the mounted plugin tree."""
    hermes = _gateway(tmp_path, None,
                      'printf "PASS\\targs=%s\\n" "$*"\nexit 0\n')
    found = _run(tmp_path, hermes, monkeypatch)
    assert found[0][0] == "INFO" and "args=-I -B " in found[0][2], found


def test_an_unrunnable_interpreter_is_undetectable(tmp_path, monkeypatch):
    """An interpreter that cannot be executed (no exec bit; holds as root too) is not a pass."""
    hermes = _gateway(tmp_path, None, "exit 0\n")
    (hermes / ".venv" / "bin" / "python").chmod(0o644)
    found = _run(tmp_path, hermes, monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("WARN", "scheduler-dst")], found
    assert "cannot run" in found[0][2]


def test_the_verdict_is_the_last_stdout_line(tmp_path, monkeypatch):
    """A croniter that prints a warning first must not hide the verdict line behind it."""
    hermes = _gateway(tmp_path, None,
                      'printf "a deprecation notice\\nFAIL\\tcroniter 6.0.0 shifts DST\\n"\nexit 10\n')
    found = _run(tmp_path, hermes, monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("FAIL", "scheduler-dst")], found
    assert found[0][2].startswith("croniter 6.0.0 shifts DST. Lanes fire an hour off")


def test_an_info_finding_carries_the_self_tests_detail_verbatim(tmp_path, monkeypatch):
    hermes = _gateway(tmp_path, None, 'printf "PASS\\tcroniter 6.2.4 computes 3 DST case(s) correctly\\n"\n')
    assert _run(tmp_path, hermes, monkeypatch) == [
        ("INFO", "scheduler-dst", "croniter 6.2.4 computes 3 DST case(s) correctly")]


def test_a_crash_reports_the_last_stderr_line(tmp_path, monkeypatch):
    hermes = _gateway(tmp_path, None,
                      'printf "Traceback (most recent call last):\\nImportError: boom\\n" >&2\nexit 1\n')
    found = _run(tmp_path, hermes, monkeypatch)
    assert found[0][0] == "WARN" and "(exit 1: ImportError: boom)" in found[0][2], found


def _plugin(tmp_path: Path, mode: str) -> None:
    """Install a fake cron-plus tree where the gateway check looks for it (DATA/plugins)."""
    src = _sibling("test_croniter_dst_selftest")._plugin_dir(tmp_path, mode)
    dest = tmp_path / "data" / "plugins" / "cron-plus"
    dest.mkdir(parents=True)
    (dest / "jobs.py").write_text((src / "jobs.py").read_text(encoding="utf-8"), encoding="utf-8")


def test_the_installed_plugin_with_the_pinned_guard_fails(tmp_path, monkeypatch):
    """okengine#1055 / #1061 [4]: croniter is right, the deployed plugin's fold policy is not."""
    _plugin(tmp_path, "pin")
    found = _run(tmp_path, _gateway(tmp_path, _stub_dir(tmp_path, "correct")), monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("FAIL", "scheduler-dst")], found
    assert "mis-schedules the fall-back hour" in found[0][2]


def test_the_installed_plugin_with_the_fold_policy_passes(tmp_path, monkeypatch):
    _plugin(tmp_path, "fixed")
    found = _run(tmp_path, _gateway(tmp_path, _stub_dir(tmp_path, "correct")), monkeypatch)
    assert [(lvl, area) for lvl, area, _ in found] == [("INFO", "scheduler-dst")], found
    assert "passes 3 fall-back case(s)" in found[0][2]
