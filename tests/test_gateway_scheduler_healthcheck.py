"""The scheduler's continuous detector must not itself depend on the scheduler."""
from pathlib import Path


COMPOSE = Path(__file__).resolve().parents[1] / "templates/pack/skeleton/docker-compose.yml"
DEPLOY = Path(__file__).resolve().parents[1] / "scripts/deploy.sh"


def test_gateway_healthcheck_detects_stale_tick_and_stall_sentinel():
    text = COMPOSE.read_text(encoding="utf-8")
    gateway = text.split("  okengine-reader:", 1)[0]
    assert "healthcheck:" in gateway
    assert ".tick.lock" in gateway
    assert "now-tick" in gateway and "-le 180" in gateway
    assert ".scheduler-stalled" in gateway and "! -s" in gateway
    assert "lock-owner.json" in gateway and "now-mtime" in gateway and "-le 300" in gateway
    assert '\\"running\\"' in gateway and "-mmin +60" in gateway
    assert "/proc/[0-9]*/stat" in gateway and "') D '" in gateway


def test_existing_deployments_receive_the_same_health_signals_via_override():
    deploy = DEPLOY.read_text(encoding="utf-8")
    for signal in (
        ".tick.lock", ".scheduler-stalled", "lock-owner.json", "now-mtime", "-mmin +60",
        "/proc/[0-9]*/stat", "') D '",
    ):
        assert signal in deploy
    assert "cat >\"$gateway_override\" <<'EOF'" in deploy, (
        "the quoted heredoc must preserve Compose $$ interpolation instead of expanding shell PIDs"
    )


# ── okengine#747: the stale-run probe must be bounded to this scheduler generation ─────────

def _gateway_probe(text: str) -> str:
    """The gateway healthcheck exactly as Docker receives it: Compose's `$$` decoded to `$`."""
    import json
    import re
    for match in re.finditer(r'^\s*test: (\["CMD-SHELL", ".*"\])\s*$', text, re.M):
        probe = json.loads(match.group(1))[1]
        if "tick.lock" in probe:
            return probe.replace("$$", "$")
    raise AssertionError("gateway healthcheck not found")


def _generation_clause(probe: str) -> str:
    """The stale-run find, plus whatever the probe computes beforehand to bound it.

    Deliberately does NOT require any particular shape (such as a `gen=` assignment). An earlier
    version asserted one, and restoring the old `-newer /proc/1` probe then failed every test on that
    assertion -- so the drift test "caught" the defect without its logic ever running. The tests below
    must fail because the probe BEHAVES wrongly, not because it looks different."""
    parts = probe.split("; ")
    finds = [i for i, part in enumerate(parts) if part.startswith("! find /opt/data/cron-plus/runs")]
    assert len(finds) == 1, "stale-run probe not found"
    setup = [part for part in parts[:finds[0]] if part.startswith("gen=")]
    return "; ".join(setup + [parts[finds[0]]])


_CLK_TCK = None


def _run_clause(tmp_path, *, container_age_h, records, pid1_mtime_now=False, pid1_stat=True):
    """Execute the REAL clause against fixtures standing in for /proc and the runs directory.

    PID 1's command name contains spaces and parentheses on purpose: /proc/<pid>/stat field 2 is
    the command in parens and may contain both, so a parser that split naively on whitespace would
    read the wrong field for the start time.
    """
    import os
    import subprocess
    import time
    global _CLK_TCK
    if _CLK_TCK is None:
        _CLK_TCK = int(subprocess.run(["getconf", "CLK_TCK"], capture_output=True, text=True, check=True).stdout)

    now = int(time.time())
    btime = now - 10 * 86400
    start = now - int(container_age_h * 3600)
    proc_stat = tmp_path / "stat"
    proc_stat.write_text(f"cpu  1 2 3\nbtime {btime}\nprocesses 99\n", encoding="utf-8")
    pid1 = tmp_path / "pid1"
    pid1.mkdir()
    if pid1_stat:
        after_comm = ["S"] + ["0"] * 18 + [str((start - btime) * _CLK_TCK)] + ["0"] * 10
        (pid1 / "stat").write_text("1 (my init (x) y) " + " ".join(after_comm) + "\n", encoding="utf-8")
    # What an evicted-and-recreated procfs inode looks like: its mtime is roughly NOW.
    os.utime(pid1, (now, now) if pid1_mtime_now else (start, start))

    runs = tmp_path / "runs" / "job"
    runs.mkdir(parents=True)
    anchors = {"start": start, "now": now}
    for name, (status, anchor, offset) in records.items():
        written = anchors[anchor] + offset
        f = runs / f"{name}.json"
        f.write_text('{"status": "%s"}' % status, encoding="utf-8")
        os.utime(f, (written, written))

    clause = (_generation_clause(_gateway_probe(COMPOSE.read_text(encoding="utf-8")))
              .replace("/proc/1/stat", f"{pid1}/stat")
              .replace("/proc/1", str(pid1))
              .replace("/proc/stat", str(proc_stat))
              .replace("/opt/data/cron-plus/runs", str(tmp_path / "runs")))
    return subprocess.run(["sh", "-c", clause + "; exit 0"], capture_output=True, text=True, timeout=30)


def test_orphans_from_a_previous_container_do_not_fail_the_probe(tmp_path):
    """THE ORIGINAL REGRESSION (okengine#747). A lane killed rather than completed leaves its record
    saying "running" forever. Measured 2026-09-13: 246, 36, 43 and 79 such records on four gateways,
    every one of them from before the current container started, all four failing health for 30
    hours while their schedulers ran normally. A probe that is always red carries no information."""
    result = _run_clause(tmp_path, container_age_h=48, records={
        "orphan": ("running", "start", -3600),
    })
    assert result.returncode == 0, f"an orphan from a previous generation failed the probe: {result.stderr}"


def test_a_run_stuck_in_this_generation_still_fails_the_probe(tmp_path):
    """THE NEGATIVE GUARD, executed rather than inspected. Bounding the probe must not blind it: a
    run started after this container came up and still "running" an hour later is a real stall."""
    result = _run_clause(tmp_path, container_age_h=48, records={
        "orphan": ("running", "start", -3600),
        "stuck": ("running", "start", 3600),
    })
    assert result.returncode == 1, "a run stuck in the current generation was not detected"


def test_recent_and_finished_runs_do_not_fail_the_probe(tmp_path):
    """The two bounds that remain: under an hour old, or no longer running."""
    result = _run_clause(tmp_path, container_age_h=48, records={
        "in-progress": ("running", "now", -300),
        "done": ("succeeded", "start", 3600),
    })
    assert result.returncode == 0, result.stderr


def test_the_generation_bound_survives_procfs_inode_drift(tmp_path):
    """THE DEFECT IN THE FIRST VERSION OF THIS FIX.

    It bounded with `-newer /proc/1`, trusting /proc/1's mtime to be the process start. procfs stamps
    an inode when the kernel CREATES it, and an evicted inode is recreated with the current time.
    Measured 2026-09-13 on two of four gateways: /proc/1 read 31.5 hours after start, and the bound
    saw 2 of 1620 and 1 of 1367 of that generation's runs -- so a stuck run reported HEALTHY.

    The fixture's /proc/1 directory is stamped NOW, as a drifted inode would be. A probe reading that
    mtime sees the stuck run as older than "start" and misses it; one computing the start from
    /proc/1/stat still catches it. Reverting to `-newer /proc/1` fails this test."""
    result = _run_clause(tmp_path, container_age_h=48, pid1_mtime_now=True, records={
        "stuck": ("running", "start", 3600),
    })
    assert result.returncode == 1, (
        "a stuck run went undetected once /proc/1's mtime drifted to now -- the generation bound is "
        "reading an inode timestamp instead of the process start")


def test_an_unreadable_process_start_fails_loud_rather_than_passing(tmp_path):
    """If the start cannot be computed, the probe must not quietly treat every record as belonging to
    another generation. An empty start makes the arithmetic invalid, and dash aborts non-zero."""
    result = _run_clause(tmp_path, container_age_h=48, pid1_stat=False, records={
        "stuck": ("running", "start", 3600),
    })
    assert result.returncode != 0, "the probe passed without being able to read PID 1's start time"


def test_the_template_and_the_deploy_override_cannot_drift():
    """The healthcheck exists TWICE — in the skeleton for new packs, and in deploy.sh as an
    override so existing deployments gain it on their next deploy. Fixing one and not the other
    leaves live gateways on the broken probe while the tests pass, which is the multi-surface
    drift this repo treats as a defect everywhere else."""
    assert _gateway_probe(COMPOSE.read_text(encoding="utf-8")) == \
        _gateway_probe(DEPLOY.read_text(encoding="utf-8")), (
            "skeleton and deploy.sh gateway healthchecks differ; they must be changed together. "
            "This compares the WHOLE probe: comparing only the find expression would not notice the "
            "generation start being computed differently in one copy")
