#!/usr/bin/env python3
"""croniter_dst_selftest.py: does THIS interpreter's croniter compute next-fire across DST correctly?

cron-plus computes each lane's ``next_run_at`` as
``croniter(expr, last_run.astimezone(TZ)).get_next(datetime)``. croniter 6.0.0 (the version Hermes's
frozen ``uv.lock`` installs) keeps the BASE's UTC offset when the next fire lies across a DST
transition, so a daily lane fires an hour late on fall-back and an hour early (then again on time)
on spring-forward. CI and the host lock 6.2.4, which is correct, so only a test run by the
scheduler's own interpreter can see the difference.

Run it with the interpreter cron-plus runs in, isolated so nothing but that venv supplies croniter:

    /opt/hermes/.venv/bin/python -I croniter_dst_selftest.py

The gateway image build runs it right after installing croniter (an image that computes DST wrong
does not build); ``deployment_checks.check_scheduler_dst`` runs it in the gateway (daily validator,
post-deploy verify). The cases are fixed America/New_York transitions: they test croniter's
arithmetic, not the deployment's TZ.

With ``--cron-plus DIR`` it also runs the deployed plugin's own ``compute_next_run`` from DIR on the
fall-back hour (okengine#1055, okengine#1061 [4]): croniter can be right while the plugin's fold
policy re-fires a daily slot or drops an hourly one. The gateway check passes the installed plugin.

Prints one ``VERDICT<TAB>detail`` line. Exit status: 0 PASS, 10 FAIL (wrong next fire, or croniter
raised), 11 MISSING (croniter not importable), 12 UNDETECTABLE (no tz database to build the cases).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZONE = "America/New_York"
#: (cron expression, base wall time, expected next wall time, label). Each base and its expected
#: fire sit on opposite sides of a transition; croniter 6.0.0 gets every one wrong.
CASES = (
    ("0 7 * * *", (2026, 10, 31, 7, 0), (2026, 11, 1, 7, 0), "daily 07:00 across fall-back"),
    ("45 23 * * *", (2026, 10, 31, 23, 45), (2026, 11, 1, 23, 45), "daily 23:45 across fall-back"),
    ("0 7 * * *", (2027, 3, 13, 7, 0), (2027, 3, 14, 7, 0), "daily 07:00 across spring-forward"),
)
#: (cron expression, last run UTC, expected next fire UTC, label) for the plugin's compute_next_run.
#: 2026-11-01 01:00-01:59 happens twice in America/New_York: 05:xxZ (EDT), then 06:xxZ (EST).
CRON_PLUS_CASES = (
    ("12 * * * *", "2026-11-01T05:12:00+00:00", "2026-11-01T06:12:00+00:00",
     "hourly lane fires in both passes of the repeated hour"),
    ("30 1 * * *", "2026-11-01T05:31:00+00:00", "2026-11-02T06:30:00+00:00",
     "daily 01:30 claimed a minute late fires once"),
    ("0 1-23/2 * * *", "2026-11-01T05:02:00+00:00", "2026-11-01T08:00:00+00:00",
     "every-other-hour lane claimed late skips the repeated 01:00"),
)
PASS, FAIL, MISSING, UNDETECTABLE = 0, 10, 11, 12


def _croniter_version(module) -> str:
    """The imported module's own version when it declares one, else the installed distribution's
    (croniter 6.x declares none), else "unknown"."""
    declared = getattr(module, "__version__", None)
    if declared:
        return str(declared)
    try:
        return version("croniter")
    except PackageNotFoundError:
        return "unknown"


def evaluate(croniter, zone) -> list[str]:
    """Return one message per case whose next fire differs from the expected instant."""
    wrong = []
    for expr, base, want, label in CASES:
        base_local = datetime(*base, tzinfo=zone)
        expected = datetime(*want, tzinfo=zone)
        try:
            got = croniter(expr, base_local).get_next(datetime)
        except Exception as exc:  # a scheduler that cannot compute the case is broken, not unknown
            wrong.append(f"{label}: '{expr}' from {base_local.isoformat()} raised "
                         f"{type(exc).__name__}: {exc}")
            continue
        if got.astimezone(timezone.utc) != expected.astimezone(timezone.utc):
            wrong.append(f"{label}: '{expr}' from {base_local.isoformat()} gave "
                         f"{got.astimezone(zone).isoformat()}, expected {expected.isoformat()}")
    return wrong


def _load_plugin_jobs(plugin: str):
    """Import ``jobs.py`` from the cron-plus tree at ``plugin``, its overlays resolving beside it."""
    sys.path.insert(0, plugin)
    spec = importlib.util.spec_from_file_location("cron_plus_jobs_selftest",
                                                  os.path.join(plugin, "jobs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evaluate_cron_plus(compute_next_run) -> list[str]:
    """Return one message per CRON_PLUS_CASES case whose next fire differs from the expected one.
    The plugin reads its zone from CRON_TZ per call, so the cases set it for their duration."""
    wrong = []
    saved = os.environ.get("CRON_TZ")
    os.environ["CRON_TZ"] = ZONE
    try:
        for expr, last, want, label in CRON_PLUS_CASES:
            try:
                got = compute_next_run({"kind": "cron", "expr": expr}, last_run_at=last)
            except Exception as exc:
                wrong.append(f"cron-plus {label}: '{expr}' after {last} raised "
                             f"{type(exc).__name__}: {exc}")
                continue
            if got is None or datetime.fromisoformat(got) != datetime.fromisoformat(want):
                wrong.append(f"cron-plus {label}: '{expr}' after {last} gave {got}, expected {want}")
    finally:
        if saved is None:
            os.environ.pop("CRON_TZ", None)
        else:
            os.environ["CRON_TZ"] = saved
    return wrong


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cron-plus", metavar="DIR",
                        help="also judge compute_next_run of the cron-plus plugin at DIR")
    args = parser.parse_args(argv)
    try:
        import croniter as module
        from croniter import croniter
    except ImportError as exc:
        print(f"MISSING\tcroniter is not importable by {sys.executable} ({exc}); "
              "cron-plus cannot schedule any cron lane")
        return MISSING
    try:
        zone = ZoneInfo(ZONE)
    except ZoneInfoNotFoundError as exc:
        print(f"UNDETECTABLE\tno tz database entry for {ZONE} ({exc}); the DST cases cannot be built")
        return UNDETECTABLE
    found = _croniter_version(module)
    wrong = evaluate(croniter, zone)
    if wrong:
        print(f"FAIL\tcroniter {found} mis-computes next fire across DST: {'; '.join(wrong)}")
        return FAIL
    if args.cron_plus is None:
        print(f"PASS\tcroniter {found} computes {len(CASES)} DST case(s) correctly")
        return PASS
    if not os.path.isfile(os.path.join(args.cron_plus, "jobs.py")):
        print(f"UNDETECTABLE\tno cron-plus jobs.py under {args.cron_plus}; the plugin's fall-back "
              "policy cannot be judged")
        return UNDETECTABLE
    # Write no bytecode while the plugin is imported and run (okengine#1064): post-deploy verify
    # runs this as root in the gateway, and a .pyc beside the plugin is then root-owned in the
    # mounted /opt/data tree, which the gateway uid cannot replace.
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            compute_next_run = _load_plugin_jobs(args.cron_plus).compute_next_run
        except Exception as exc:  # a plugin that cannot import cannot schedule anything
            print(f"FAIL\tcron-plus at {args.cron_plus} does not import "
                  f"({type(exc).__name__}: {exc})")
            return FAIL
        wrong = evaluate_cron_plus(compute_next_run)
    finally:
        sys.dont_write_bytecode = saved
    if wrong:
        print(f"FAIL\tcron-plus at {args.cron_plus} mis-schedules the fall-back hour: "
              f"{'; '.join(wrong)}. Rerun deploy.sh (install-cron-plus.sh installs the pinned hermes-cron-plus, whose fold policy is dst_fold.py)")
        return FAIL
    print(f"PASS\tcroniter {found} computes {len(CASES)} DST case(s) correctly; cron-plus "
          f"compute_next_run passes {len(CRON_PLUS_CASES)} fall-back case(s)")
    return PASS


if __name__ == "__main__":
    sys.exit(main())
