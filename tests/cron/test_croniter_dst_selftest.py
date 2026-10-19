"""The gateway scheduler's croniter must compute next-fire across DST the way cron-plus needs.

cron-plus (pinned bdb6cf5f) computes ``next_run_at`` as
``croniter(expr, last_run.astimezone(TZ)).get_next(datetime)``. The gateway image installed
croniter 6.0.0 from Hermes's frozen ``uv.lock``, which mis-computes when the base and the next fire
sit on opposite sides of a DST transition: ``0 7 * * *`` from 2026-10-31 07:00 EDT gave 2026-11-01
08:00 EST (an hour late), and from 2027-03-13 07:00 EST gave 2027-03-14 06:00 EDT (an hour early,
then the 07:00 slot fires again). Every CI and host lock carried 6.2.4, which is correct, so no test
could see it. ``scripts/cron/croniter_dst_selftest.py`` is the behavioral self-test that the image
build, the daily validator and the post-deploy verifier run against the interpreter cron-plus uses.

The negative fixtures are stub ``croniter`` modules: one reproduces 6.0.0's arithmetic (the result
keeps the base's UTC offset), one computes correctly, one is absent. Each runs as production runs
the self-test, as a subprocess of a separate interpreter, so the croniter under test is the one on
that interpreter's path, not the one this test process imported.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SELFTEST = REPO / "scripts" / "cron" / "croniter_dst_selftest.py"

#: A croniter that answers daily ``M H * * *`` expressions. SHIFTED reproduces croniter 6.0.0: the
#: wall-clock target is right but the UTC offset of the BASE is kept, so the result lands one hour
#: late on fall-back and one hour early on spring-forward.
_STUB = '''
import os
from datetime import datetime, timedelta, timezone
MODE = {mode!r}
if MODE == "missing":
    raise ImportError("No module named 'croniter'")
if MODE == "explodes":
    pass
__version__ = "stub-" + MODE


class croniter:
    def __init__(self, expr, base):
        if MODE == "explodes":
            raise ValueError("bad cron expression")
        self.minute, self.hour = (int(x) for x in expr.split()[:2])
        self.base = base

    def get_next(self, ret_type=None):
        base = self.base
        wall = base.replace(tzinfo=None)
        cand = wall.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if cand <= wall:
            cand += timedelta(days=1)
        cand = cand.replace(tzinfo=base.tzinfo)
        shift = base.utcoffset() - cand.utcoffset() if MODE == "shifted" else timedelta(0)
        return (cand.astimezone(timezone.utc) + shift).astimezone(base.tzinfo)
'''


def _stub_dir(tmp_path: Path, mode: str) -> Path:
    d = tmp_path / f"stub-{mode}"
    d.mkdir()
    (d / "croniter.py").write_text(_STUB.format(mode=mode), encoding="utf-8")
    return d


def _run(stub: Path | None, *args: str) -> subprocess.CompletedProcess:
    """Run the self-test as a subprocess. With a stub, the stub directory is the only croniter on
    the path (PYTHONPATH, so the interpreter is NOT isolated: that is the point of the fixture)."""
    env = {k: v for k, v in os.environ.items()
           if k != "PYTHONPATH" and not k.startswith("COV_CORE_")}
    cmd = [sys.executable, str(SELFTEST), *args]
    if stub is not None:
        env["PYTHONPATH"] = str(stub)
    else:
        cmd.insert(1, "-I")                 # production invocation: isolated interpreter
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, cwd=REPO)


def _load():
    spec = importlib.util.spec_from_file_location("croniter_dst_selftest_t", SELFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_this_interpreters_croniter_passes_when_run_isolated():
    """Real invocation, as the image build and the in-gateway check run it: ``python -I <file>``.
    CI and the host lock croniter 6.2.4, which computes every case correctly. Not importorskip:
    croniter is pinned in every CI lock, so its absence is a failure, not a skip."""
    proc = _run(None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict, detail = proc.stdout.strip().split("\t", 1)
    assert verdict == "PASS"
    assert "croniter" in detail and "3 DST case(s)" in detail


def test_the_croniter_600_arithmetic_fails(tmp_path):
    proc = _run(_stub_dir(tmp_path, "shifted"))
    assert proc.returncode == 10, proc.stdout + proc.stderr
    verdict, detail = proc.stdout.strip().split("\t", 1)
    assert verdict == "FAIL"
    # The message names the observed and the expected fire so an operator can see the shift.
    assert "2026-11-01T08:00:00-05:00" in detail and "2026-11-01T07:00:00-05:00" in detail
    assert "2027-03-14T06:00:00-04:00" in detail and "2027-03-14T07:00:00-04:00" in detail
    assert "stub-shifted" in detail, "the failing croniter's version must be reported"


def test_a_correct_stub_passes(tmp_path):
    """The stub's correct mode passes, so the FAIL above is caused by the shift alone."""
    proc = _run(_stub_dir(tmp_path, "correct"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("PASS\t")


def test_a_missing_croniter_is_its_own_failure(tmp_path):
    proc = _run(_stub_dir(tmp_path, "missing"))
    assert proc.returncode == 11, proc.stdout + proc.stderr
    assert proc.stdout.startswith("MISSING\t")
    assert "cron-plus cannot schedule" in proc.stdout


def test_a_croniter_that_raises_fails(tmp_path):
    """A croniter that cannot compute the cases is a broken scheduler, not an undetectable one."""
    proc = _run(_stub_dir(tmp_path, "explodes"))
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert "raised ValueError: bad cron expression" in proc.stdout


def test_no_timezone_database_is_undetectable(monkeypatch, capsys):
    """Without tz data the DST cases cannot be built: undetectable, never a pass."""
    m = _load()

    def no_zone(name):
        raise m.ZoneInfoNotFoundError(f"No time zone found with key {name}")

    monkeypatch.setattr(m, "ZoneInfo", no_zone)
    assert m.main([]) == 12
    out = capsys.readouterr().out
    assert out.startswith("UNDETECTABLE\t") and "America/New_York" in out


def test_unknown_arguments_are_refused():
    proc = _run(None, "--okengine-unknown-argument-probe")
    assert proc.returncode == 2


def test_the_cases_straddle_both_transitions():
    """Pin the acceptance set: each case's base and expected fire carry different UTC offsets,
    i.e. each one crosses a transition (a case that does not cross one proves nothing)."""
    m = _load()
    zone = m.ZoneInfo(m.ZONE)
    seen = set()
    for expr, base, want, label in m.CASES:
        b, w = m.datetime(*base, tzinfo=zone), m.datetime(*want, tzinfo=zone)
        assert b.utcoffset() != w.utcoffset(), label
        seen.add("fall" if b.utcoffset() > w.utcoffset() else "spring")
    assert seen == {"fall", "spring"}


def test_version_prefers_the_module_then_the_distribution(monkeypatch):
    m = _load()

    class Declared:
        __version__ = "9.9.9"

    assert m._croniter_version(Declared) == "9.9.9"
    monkeypatch.setattr(m, "version", lambda name: "6.2.4" if name == "croniter" else None)
    assert m._croniter_version(object()) == "6.2.4"

    def no_dist(name):
        raise m.PackageNotFoundError(name)

    monkeypatch.setattr(m, "version", no_dist)
    assert m._croniter_version(object()) == "unknown"


# ---- the same verdicts in-process (coverage of the decision logic; the subprocess runs above are
# the production-shaped evidence) ----------------------------------------------------------------

def _stub_module(mode: str) -> types.ModuleType | None:
    if mode == "missing":
        return None                         # sys.modules[name] = None makes `import` raise
    module = types.ModuleType("croniter")
    exec(compile(_STUB.format(mode=mode), "croniter_stub", "exec"), module.__dict__)  # noqa: S102
    return module


@pytest.mark.parametrize("mode, code, verdict", [
    ("correct", 0, "PASS"), ("shifted", 10, "FAIL"), ("explodes", 10, "FAIL"),
    ("missing", 11, "MISSING"),
])
def test_main_maps_each_croniter_behavior_to_its_verdict(monkeypatch, capsys, mode, code, verdict):
    m = _load()
    monkeypatch.setitem(sys.modules, "croniter", _stub_module(mode))
    assert m.main([]) == code
    out = capsys.readouterr().out
    assert out.startswith(verdict + "\t") and out.count("\n") == 1, out


def test_evaluate_reports_only_the_wrong_cases():
    m = _load()
    zone = m.ZoneInfo(m.ZONE)
    assert m.evaluate(_stub_module("correct").croniter, zone) == []
    wrong = m.evaluate(_stub_module("shifted").croniter, zone)
    assert [w.split(": '")[0] for w in wrong] == [label for *_, label in m.CASES]


def test_every_raising_case_is_reported():
    """A case that raises does not stop the remaining cases from being judged."""
    m = _load()
    wrong = m.evaluate(_stub_module("explodes").croniter, m.ZoneInfo(m.ZONE))
    assert len(wrong) == len(m.CASES) and all("raised ValueError" in w for w in wrong), wrong


def test_the_acceptance_set_is_the_audited_reproduction():
    """The cases are the v0.15.14 audit's live reproduction (claims at the exact slot minute on the
    day before each transition), plus the date-skipping 23:45 lane. Changing one is a decision."""
    assert _load().CASES == (
        ("0 7 * * *", (2026, 10, 31, 7, 0), (2026, 11, 1, 7, 0), "daily 07:00 across fall-back"),
        ("45 23 * * *", (2026, 10, 31, 23, 45), (2026, 11, 1, 23, 45),
         "daily 23:45 across fall-back"),
        ("0 7 * * *", (2027, 3, 13, 7, 0), (2027, 3, 14, 7, 0), "daily 07:00 across spring-forward"),
    )


def test_help_describes_the_self_test(capsys):
    m = _load()
    with pytest.raises(SystemExit) as exc:
        m.main(["--help"])
    assert exc.value.code == 0
    assert m.__doc__.splitlines()[0] in " ".join(capsys.readouterr().out.split())


# ---- --cron-plus: the deployed plugin's compute_next_run on the fall-back hour (okengine#1055,
# okengine#1061 [4]) ---------------------------------------------------------------------------

#: A cron-plus tree whose jobs.compute_next_run answers from a table. "fixed" gives the expected
#: fires (only when CRON_TZ is the case zone, as the real plugin needs); "pin" gives what the pinned
#: bdb6cf5f guard computed on the audit's reproduction; "raises" and "none" are a broken scheduler.
_PLUGIN = '''
import os
MODE = {mode!r}
if MODE == "unimportable":
    raise ImportError("No module named 'croniter'")
FIXED = {{"12 * * * *": "2026-11-01T06:12:00+00:00", "30 1 * * *": "2026-11-02T06:30:00+00:00",
         "0 1-23/2 * * *": "2026-11-01T08:00:00+00:00"}}
PIN = {{"12 * * * *": "2026-11-01T07:12:00+00:00", "30 1 * * *": "2026-11-01T06:30:00+00:00",
       "0 1-23/2 * * *": "2026-11-01T06:00:00+00:00"}}


def compute_next_run(schedule, *, anchor=None, last_run_at=None):
    if MODE == "raises":
        raise RuntimeError("tick lock lost")
    if MODE == "none":
        return None
    if os.environ.get("CRON_TZ") != "America/New_York":
        return "1970-01-01T00:00:00+00:00"
    return (FIXED if MODE == "fixed" else PIN)[schedule["expr"]]
'''


def _plugin_dir(tmp_path: Path, mode: str) -> Path:
    d = tmp_path / f"cron-plus-{mode}"
    d.mkdir()
    (d / "jobs.py").write_text(_PLUGIN.format(mode=mode), encoding="utf-8")
    return d


def test_the_pinned_guard_fails_the_plugin_cases(tmp_path):
    """Real invocation (-I, this interpreter's croniter 6.2.4) against the pin's answers: every
    fall-back case is reported with what it gave and what it should give."""
    proc = _run(None, "--cron-plus", str(_plugin_dir(tmp_path, "pin")))
    assert proc.returncode == 10, proc.stdout + proc.stderr
    verdict, detail = proc.stdout.strip().split("\t", 1)
    assert verdict == "FAIL" and "mis-schedules the fall-back hour" in detail
    assert "gave 2026-11-01T07:12:00+00:00, expected 2026-11-01T06:12:00+00:00" in detail
    assert "gave 2026-11-01T06:30:00+00:00, expected 2026-11-02T06:30:00+00:00" in detail
    assert "gave 2026-11-01T06:00:00+00:00, expected 2026-11-01T08:00:00+00:00" in detail
    assert "dst-fold.patch" in detail, "the remedy must be named"


def test_a_fixed_plugin_passes(tmp_path):
    proc = _run(None, "--cron-plus", str(_plugin_dir(tmp_path, "fixed")))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict, detail = proc.stdout.strip().split("\t", 1)
    assert verdict == "PASS"
    assert "3 DST case(s)" in detail and "passes 3 fall-back case(s)" in detail


def test_judging_a_plugin_leaves_no_bytecode_in_its_tree(tmp_path, monkeypatch):
    """okengine#1064: post-deploy verify runs the check as root in the gateway, so any .pyc the
    import writes lands root-owned in /opt/data/plugins/cron-plus and fails the ownership check.
    Production invocation (-I, no -B, bytecode writing on), with a sibling overlay as jobs.py has."""
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    plugin = _plugin_dir(tmp_path, "fixed")
    (plugin / "fold_overlay.py").write_text("VALUE = 1\n", encoding="utf-8")
    jobs = plugin / "jobs.py"
    jobs.write_text("import fold_overlay\n" + jobs.read_text(encoding="utf-8"), encoding="utf-8")
    proc = _run(None, "--cron-plus", str(plugin))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    written = sorted(str(p.relative_to(plugin)) for p in plugin.rglob("*") if p.suffix == ".pyc"
                     or p.name == "__pycache__")
    assert written == [], f"the self-test wrote bytecode into the plugin tree: {written}"


def test_judging_a_plugin_restores_bytecode_writing(tmp_path, monkeypatch):
    """The in-process run must not leave this interpreter's bytecode switch changed."""
    m = _load()
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    assert m.main(["--cron-plus", str(_plugin_dir(tmp_path, "fixed"))]) == m.PASS
    assert sys.dont_write_bytecode is False


@pytest.mark.parametrize("mode, needle", [
    ("raises", "raised RuntimeError: tick lock lost"),
    ("none", "gave None"),
    ("unimportable", "does not import (ImportError"),
])
def test_a_broken_plugin_fails(tmp_path, mode, needle):
    proc = _run(None, "--cron-plus", str(_plugin_dir(tmp_path, mode)))
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert proc.stdout.startswith("FAIL\t") and needle in proc.stdout, proc.stdout


def test_a_plugin_dir_without_jobs_is_undetectable(tmp_path):
    proc = _run(None, "--cron-plus", str(tmp_path))
    assert proc.returncode == 12, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNDETECTABLE\t") and "no cron-plus jobs.py" in proc.stdout


def test_a_croniter_failure_is_reported_before_the_plugin_is_judged(tmp_path):
    proc = _run(_stub_dir(tmp_path, "shifted"), "--cron-plus", str(_plugin_dir(tmp_path, "fixed")))
    assert proc.returncode == 10 and "croniter stub-shifted mis-computes" in proc.stdout


@pytest.mark.parametrize("prior", [None, "Europe/Paris"])
def test_the_plugin_cases_restore_cron_tz(monkeypatch, tmp_path, prior):
    m = _load()
    if prior is None:
        monkeypatch.delenv("CRON_TZ", raising=False)
    else:
        monkeypatch.setenv("CRON_TZ", prior)
    seen = []

    def compute(schedule, *, last_run_at):
        seen.append(os.environ.get("CRON_TZ"))
        raise ValueError("boom")

    wrong = m.evaluate_cron_plus(compute)
    assert len(wrong) == len(m.CRON_PLUS_CASES)
    assert seen == [m.ZONE] * len(m.CRON_PLUS_CASES)
    assert os.environ.get("CRON_TZ") == prior


def test_the_plugin_cases_sit_in_the_repeated_hour():
    """Each case's last run is inside 2026-11-01's repeated 01:xx, where the bugs live; one keeps
    the second pass (a star hour) and two skip it (explicit hours)."""
    m = _load()
    zone = m.ZoneInfo(m.ZONE)
    for expr, last, want, label in m.CRON_PLUS_CASES:
        local = m.datetime.fromisoformat(last).astimezone(zone)
        assert (local.date().isoformat(), local.hour) == ("2026-11-01", 1), label
    assert [c[0].split()[1].startswith("*") for c in m.CRON_PLUS_CASES] == [True, False, False]


@pytest.mark.parametrize("mode, code, verdict, needle", [
    ("fixed", 0, "PASS", "passes 3 fall-back case(s)"),
    ("pin", 10, "FAIL", "mis-schedules the fall-back hour"),
    ("unimportable", 10, "FAIL", "does not import (ImportError"),
    (None, 12, "UNDETECTABLE", "no cron-plus jobs.py"),
])
def test_main_maps_each_plugin_behavior_to_its_verdict(monkeypatch, capsys, tmp_path, mode, code,
                                                       verdict, needle):
    """In-process twin of the --cron-plus subprocess runs above (their coverage is not measured)."""
    m = _load()
    monkeypatch.setattr(sys, "path", list(sys.path))   # the loader prepends the plugin dir
    plugin = _plugin_dir(tmp_path, mode) if mode else tmp_path
    assert m.main(["--cron-plus", str(plugin)]) == code
    out = capsys.readouterr().out
    assert out.startswith(verdict + "\t") and needle in out and out.count("\n") == 1, out
