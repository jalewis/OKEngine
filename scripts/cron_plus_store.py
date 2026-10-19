#!/usr/bin/env python3
"""Deploy-side access to the cron-plus job store, under the scheduler's own lock (okengine#850).

cron-plus persists `jobs.json` by tempfile + os.replace. Its lock used to be a flock on the
jobs.json inode -- a file about to stop being jobs.json -- and the deploy wrote with a plain
`cat >` holding no lock at all. On a freshly recreated gateway the scheduler read the OLD store,
the deploy wrote the new one, and the scheduler's read-modify-write then persisted the old list
over it: 14 lanes on one deployment kept write-server names the same deploy had retired, while
every step reported success.

The carried patch `patches/cron-plus/jobs-store-lock.patch` moves the scheduler's lock to the
sidecar LOCK_NAME, which is never replaced. This module takes the same lock. Stdlib only: `write`
runs INSIDE the gateway (`docker exec -i … python3 - write …` with this file on stdin).

    write <dest jobs.json> <incoming.json> <timestamp>
        snapshot the live store, carry operator pauses forward (okengine#517; RESUME_PAUSED=1
        re-arms them), write atomically, and record exactly what was deployed as DEPLOYED_NAME
        next to it -- all under the lock.
    drift <deployed.json> <live.json>
        exit 1 and list every deployed job whose DEFINITION the live store no longer carries.
        Runtime fields the scheduler owns (RUNTIME_FIELDS, and any `_`-prefixed key) are ignored.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import tempfile

LOCK_NAME = "jobs.json.lock"          # must equal JOBS_LOCK_FILE in jobs-store-lock.patch
DEPLOYED_NAME = "jobs.deployed.json"
# Fields the scheduler rewrites at runtime (cron-plus jobs.py, the after_ordering overlay) or an
# operator pause sets. Only the ones a DEPLOYED job can also carry matter to drift(): it compares the
# keys the deploy wrote, so a runtime key the deploy never writes is ignored by construction -- the
# first live roll FAILed on after_ordering's last_completed_at/last_run_success under a denylist.
RUNTIME_FIELDS = frozenset({"enabled", "paused_at", "next_run_at", "last_run_at", "last_status",
                            "last_error", "last_delivery_error", "last_completed_at",
                            "last_run_success", "after_claim", "after_consumed"})


def _jobs(doc) -> list:
    return doc if isinstance(doc, list) else list((doc or {}).get("jobs") or [])


def _truthy(value: str) -> bool:
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def carry_pauses(new: dict, live_jobs: list, resume: bool) -> list[str]:
    """Keep an operator pause on a regenerated lane, matched on the stable job id. Returns the
    report lines; mutates ``new`` in place."""
    paused = {j["id"]: j for j in live_jobs if j.get("id") and j.get("enabled") is False}
    if not paused:
        return []
    names = sorted((j.get("name") or j["id"]) for j in paused.values())
    if resume:
        return [f"  RESUME_PAUSED=1 — re-arming {len(paused)} operator-paused lane(s): "
                + ", ".join(names)]
    kept = []
    for job in _jobs(new):
        held = paused.get(job.get("id"))
        if not held:
            continue
        job["enabled"] = False
        if held.get("paused_at"):
            job["paused_at"] = held["paused_at"]
        kept.append(job.get("name") or job["id"])
    out = []
    if kept:
        out.append(f"  preserved operator pause on {len(kept)} lane(s): " + ", ".join(sorted(kept)))
    if len(paused) - len(kept):
        out.append(f"  note: {len(paused) - len(kept)} paused lane(s) are no longer in the source "
                   "and were not carried forward")
    return out


def _atomic_json(path: str, doc) -> None:
    tmp = tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path) or ".", prefix=".jobs.",
                                      suffix=".tmp", delete=False, encoding="utf-8")
    try:
        json.dump(doc, tmp, indent=2)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def write(dest: str, incoming: str, stamp: str, resume: bool = False) -> list[str]:
    """Replace the live store with ``incoming`` under the scheduler's lock. Returns report lines."""
    home = os.path.dirname(dest) or "."
    with open(incoming, encoding="utf-8") as fh:
        new = json.load(fh)
    lines: list[str] = []
    fd = os.open(os.path.join(home, LOCK_NAME), os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        live_jobs: list = []
        if os.path.exists(dest):
            shutil.copy2(dest, f"{dest}.bak.{stamp}")
            lines.append(f"  snapshot: {dest}.bak.{stamp}")
            try:
                with open(dest, encoding="utf-8") as fh:
                    live_jobs = _jobs(json.load(fh))
            except ValueError:
                live_jobs = []                 # unreadable live store: deploy as generated
        lines += carry_pauses(new, live_jobs, resume)
        _atomic_json(os.path.join(home, DEPLOYED_NAME), new)
        _atomic_json(dest, new)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    os.unlink(incoming)
    return lines


def drift(deployed_jobs: list, live_jobs: list) -> list[str]:
    """Deployed jobs whose definition the live store no longer carries: missing, or any field the
    deploy WROTE (other than runtime state) now differs or is gone. Keys only the live store has are
    runtime additions and are not judged -- a stale write-back shows up as an old value, or a missing
    key, among the deployed ones."""
    live = {j.get("id"): j for j in live_jobs if j.get("id")}
    out = []
    for job in deployed_jobs:
        jid = job.get("id")
        now = live.get(jid)
        if now is None:
            out.append(f"{job.get('name') or jid}: missing from the live store")
            continue
        changed = sorted(k for k in job
                         if k not in RUNTIME_FIELDS and not k.startswith("_")
                         and job.get(k) != now.get(k))
        if changed:
            out.append(f"{job.get('name') or jid}: {', '.join(changed)} differ from what was deployed")
    return out


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[0] == "write":
        for line in write(argv[1], argv[2], argv[3],
                          resume=_truthy(os.environ.get("RESUME_PAUSED", "0"))):
            print(line)
        return 0
    if len(argv) == 3 and argv[0] == "drift":
        with open(argv[1], encoding="utf-8") as a, open(argv[2], encoding="utf-8") as b:
            problems = drift(_jobs(json.load(a)), _jobs(json.load(b)))
        for line in problems:
            print(line)
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
