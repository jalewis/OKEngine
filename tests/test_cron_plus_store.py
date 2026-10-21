"""The deploy writes the cron-plus job store under the scheduler's own lock (okengine#850).

cron-plus replaces jobs.json on every save (tempfile + os.replace), and it used to lock the jobs.json
INODE -- a file about to stop being jobs.json -- while the deploy wrote with a bare `cat >`. On a
freshly recreated gateway the scheduler read the old store, the deploy wrote the new one, and the
scheduler's read-modify-write put the old list back: 14 lanes kept write-server names the same
deploy had retired, and every step reported success. The fix has three surfaces that must agree:
the carried cron-plus patch (scheduler side), `scripts/cron_plus_store.py` (deploy side), and the
post-deploy check that the live store still holds what was deployed.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import cron_plus_store as store  # noqa: E402



def _jobs(*jobs):
    return {"jobs": list(jobs)}


def _job(jid="a1", **over):
    job = {"id": jid, "name": f"lane-{jid}", "enabled": True, "prompt": "p",
           "enabled_toolsets": [f"okengine-write-{jid}"], "schedule": {"kind": "cron", "expr": "0 * * * *"}}
    job.update(over)
    return job


def _home(tmp_path: Path, live=None) -> Path:
    home = tmp_path / "cron-plus"
    home.mkdir()
    if live is not None:
        (home / "jobs.json").write_text(json.dumps(live), encoding="utf-8")
    return home


def _incoming(home: Path, doc) -> Path:
    p = home / ".jobs.incoming.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# ---- write ------------------------------------------------------------------------------------

def test_write_replaces_the_store_snapshots_it_and_records_what_was_deployed(tmp_path):
    home = _home(tmp_path, _jobs(_job(enabled_toolsets=["okengine-write-old-name"])))
    new = _jobs(_job())
    lines = store.write(str(home / "jobs.json"), str(_incoming(home, new)), "T1")
    assert json.loads((home / "jobs.json").read_text()) == new
    assert json.loads((home / store.DEPLOYED_NAME).read_text()) == new
    assert "okengine-write-old-name" in (home / "jobs.json.bak.T1").read_text()
    assert not (home / ".jobs.incoming.json").exists(), "the staged input is consumed"
    assert any("snapshot" in line for line in lines)
    assert oct((home / "jobs.json").stat().st_mode & 0o777) == "0o600"


def test_write_waits_for_a_holder_of_the_schedulers_lock(tmp_path):
    """The mechanism of the fix: while the scheduler holds the sidecar lock (its read-modify-write
    in progress), the deploy's write must not land -- it lands after, so the scheduler's save can
    never overwrite it with a list read before the deploy."""
    home = _home(tmp_path, _jobs(_job(prompt="old")))
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDONLY|os.O_CREAT, 0o644);"
         "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(1.5)",
         str(home / store.LOCK_NAME)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    started = time.monotonic()
    store.write(str(home / "jobs.json"), str(_incoming(home, _jobs(_job(prompt="new")))), "T2")
    waited = time.monotonic() - started
    holder.wait(timeout=10)
    assert waited >= 1.0, f"the write did not wait for the lock holder (waited {waited:.2f}s)"
    assert json.loads((home / "jobs.json").read_text())["jobs"][0]["prompt"] == "new"


def test_the_installer_requires_the_pins_lock_and_the_deploy_uses_the_store():
    """The two sides of the lock must name one file, or the lock excludes nothing. The scheduler's
    side lives upstream since hermes-cron-plus v0.2.0, so the installer refuses a pin whose jobs.py
    does not take this module's lock file."""
    install = (REPO / "scripts" / "install-cron-plus.sh").read_text(encoding="utf-8")
    assert f'JOBS_LOCK_FILE = CRON_PLUS_HOME / "{store.LOCK_NAME}"' in install
    deploy = (REPO / "scripts" / "deploy-cron-plus-jobs.sh").read_text(encoding="utf-8")
    assert "python3 - write" in deploy and "scripts/cron_plus_store.py" in deploy
    assert "cat > '$DEST_IN'" not in deploy, "an unlocked write into the live store is the bug"
    verify = (REPO / "scripts" / "post_deploy_verify.sh").read_text(encoding="utf-8")
    assert "python3 - drift" in verify and store.DEPLOYED_NAME in verify


# ---- drift ------------------------------------------------------------------------------------

def test_runtime_fields_the_scheduler_owns_are_not_drift():
    deployed = [_job(next_run_at=None)]
    live = [_job(enabled=False, paused_at="x", next_run_at="t", last_run_at="t", last_status="ok",
                 last_error=None, last_delivery_error=None, _okengine_artifacts=[1],
                 last_completed_at="t", last_run_success=True, after_claim="c", after_consumed={})]
    assert store.drift(deployed, live) == []


def test_a_key_only_the_live_store_has_is_a_runtime_addition_not_drift():
    """The first live roll FAILed here: after_ordering records last_completed_at/last_run_success on
    the job, keys the deploy never writes. Judging only deployed keys makes that class impossible."""
    assert store.drift([_job()], [_job(some_future_runtime_marker=1)]) == []


def test_a_definition_written_back_over_the_deploy_is_drift():
    deployed = [_job("a1"), _job("b2")]
    live = [_job("a1", enabled_toolsets=["okengine-write-old-name"])]
    problems = store.drift(deployed, live)
    assert problems == ["lane-a1: enabled_toolsets differ from what was deployed",
                        "lane-b2: missing from the live store"]


def test_a_deployed_field_changed_or_dropped_after_deploy_is_drift():
    assert store.drift([_job(model="new")], [_job(model="old")]) == \
        ["lane-a1: model differ from what was deployed"]
    no_prompt = _job()
    del no_prompt["prompt"]
    assert store.drift([_job()], [no_prompt]) == ["lane-a1: prompt differ from what was deployed"]


def test_jobs_without_an_id_are_matched_by_nothing():
    assert store.drift([{"name": "x"}], [{"name": "x"}]) == ["x: missing from the live store"]


# ---- CLI --------------------------------------------------------------------------------------

def _cli(*args, env=None):
    return subprocess.run([sys.executable, "-", *args], input=(REPO / "scripts" / "cron_plus_store.py").read_text(),
                          capture_output=True, text=True, env=env)


def test_cli_drift_exit_codes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(_jobs(_job())))
    b.write_text(json.dumps([_job(next_run_at="t")]))
    assert _cli("drift", str(a), str(b)).returncode == 0, "a bare-list live store is read too"
    b.write_text(json.dumps(_jobs(_job(prompt="stale"))))
    bad = _cli("drift", str(a), str(b))
    assert bad.returncode == 1 and "prompt differ" in bad.stdout


def test_cli_write_honours_resume_paused(tmp_path):
    home = _home(tmp_path, _jobs(_job(enabled=False)))
    inc = _incoming(home, _jobs(_job()))
    out = _cli("write", str(home / "jobs.json"), str(inc), "T3", env=dict(os.environ, RESUME_PAUSED="1"))
    assert out.returncode == 0 and "RESUME_PAUSED=1" in out.stdout
    assert json.loads((home / "jobs.json").read_text())["jobs"][0]["enabled"] is True


def test_cli_usage_error():
    assert _cli("nonsense").returncode == 2


def test_a_failed_write_leaves_no_tempfile_and_the_store_intact(tmp_path, monkeypatch):
    home = _home(tmp_path, _jobs(_job(prompt="old")))
    inc = _incoming(home, _jobs(_job(prompt="new")))

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(store.os, "fsync", boom)
    with pytest.raises(OSError):
        store.write(str(home / "jobs.json"), str(inc), "T4")
    assert json.loads((home / "jobs.json").read_text())["jobs"][0]["prompt"] == "old"
    assert not list(home.glob(".jobs.*.tmp"))


# ---- in-process coverage of the same contract (subprocess runs above prove the real invocation;
# ---- coverage cannot see inside them) --------------------------------------------------------

def test_carry_pauses_keeps_a_pause_and_its_timestamp_and_reports_it():
    new = _jobs(_job("a1"), _job("b2"))
    lines = store.carry_pauses(new, [_job("a1", enabled=False, paused_at="T")], resume=False)
    assert new["jobs"][0]["enabled"] is False and new["jobs"][0]["paused_at"] == "T"
    assert new["jobs"][1]["enabled"] is True
    assert lines == ["  preserved operator pause on 1 lane(s): lane-a1"]


def test_carry_pauses_without_a_timestamp_and_for_a_retired_lane():
    new = _jobs(_job("a1"))
    lines = store.carry_pauses(new, [_job("a1", enabled=False), {"id": "gone", "enabled": False}],
                               resume=False)
    assert new["jobs"][0]["enabled"] is False and "paused_at" not in new["jobs"][0]
    assert "1 paused lane(s) are no longer in the source" in lines[-1]


def test_carry_pauses_resume_and_nothing_paused():
    new = _jobs(_job("a1"))
    assert "RESUME_PAUSED=1" in store.carry_pauses(new, [_job("a1", enabled=False)], resume=True)[0]
    assert new["jobs"][0]["enabled"] is True
    assert store.carry_pauses(new, [_job("a1")], resume=False) == []


def test_carry_pauses_reports_only_dropped_lanes_when_nothing_is_kept():
    lines = store.carry_pauses(_jobs(_job("a1")), [{"id": "gone", "name": "old", "enabled": False}],
                               resume=False)
    assert len(lines) == 1 and "no longer in the source" in lines[0]


def test_write_into_an_empty_runtime_dir_and_over_an_unreadable_store(tmp_path):
    home = _home(tmp_path)
    assert store.write(str(home / "jobs.json"), str(_incoming(home, _jobs(_job()))), "T5") == []
    (home / "jobs.json").write_text("not json", encoding="utf-8")
    lines = store.write(str(home / "jobs.json"), str(_incoming(home, _jobs(_job(prompt="q")))), "T6")
    assert lines == [f"  snapshot: {home / 'jobs.json'}.bak.T6"]
    assert json.loads((home / "jobs.json").read_text())["jobs"][0]["prompt"] == "q"


def test_write_reads_a_bare_list_live_store(tmp_path):
    home = _home(tmp_path)
    (home / "jobs.json").write_text(json.dumps([_job(enabled=False)]), encoding="utf-8")
    store.write(str(home / "jobs.json"), str(_incoming(home, _jobs(_job()))), "T7")
    assert json.loads((home / "jobs.json").read_text())["jobs"][0]["enabled"] is False


def test_a_tempfile_that_cannot_be_removed_does_not_mask_the_real_error(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")

    def cannot_unlink(*_a, **_k):
        raise OSError("gone already")
    monkeypatch.setattr(store.os, "fsync", boom)
    monkeypatch.setattr(store.os, "unlink", cannot_unlink)
    with pytest.raises(OSError, match="disk full"):
        store._atomic_json(str(tmp_path / "x.json"), {"jobs": []})


def test_main_in_process(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, _jobs(_job(enabled=False)))
    inc = _incoming(home, _jobs(_job()))
    monkeypatch.setenv("RESUME_PAUSED", "yes")
    assert store.main(["write", str(home / "jobs.json"), str(inc), "T8"]) == 0
    assert "RESUME_PAUSED=1" in capsys.readouterr().out
    live = home / "jobs.json"
    assert store.main(["drift", str(home / store.DEPLOYED_NAME), str(live)]) == 0
    live.write_text(json.dumps(_jobs(_job(prompt="stale"))), encoding="utf-8")
    assert store.main(["drift", str(home / store.DEPLOYED_NAME), str(live)]) == 1
    assert "prompt differ" in capsys.readouterr().out
    assert store.main([]) == 2 and "cron-plus job store" in capsys.readouterr().err


@pytest.mark.parametrize("value, expected", [("1", True), ("yes", True), ("0", False), ("", False),
                                             ("off", False), (" FALSE ", False)])
def test_truthy(value, expected):
    assert store._truthy(value) is expected
