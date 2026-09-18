# Scheduler availability and recovery contract

Status: accepted. The supported topology is one cron-plus scheduler inside one gateway per
deployment. The gateway uses `restart: unless-stopped`; the engine does not support active-active
schedulers. The tick file lock fences concurrent ticks, but it is not a distributed lease and must
not be represented as one.

Targets are 99.5% monthly scheduler availability, RTO 10 minutes from gateway or scheduler death,
and RPO one successfully persisted job claim. A claimed deterministic or agent job may run twice
across an unclean death; every governed write therefore requires idempotency keys or version/hash
preconditions, and completion is established only by durable read-back receipts. No successful
receipt may be synthesized from process exit alone.

The scheduler writes PID leases with PID plus process start time. On every due-job claim it checks
both process existence and creation time, removes dead or PID-reused leases, and leaves a still-live
runner fenced. Runner cleanup removes a lease only if it still owns the record. Thus a stale lease
cannot suppress a lane beyond its next scheduler tick. Run records left `running` after death are
orphan evidence; fleet health reports them and retry remains governed by the lane's idempotency
contract.

Run `scheduler_watchdog.py --evidence <durable-path> <deployment> [...]` from systemd, cron, or an
external monitor at least every two minutes. It reads the host-mounted tick and stalled sentinel,
so it continues to produce failure evidence when the gateway and every in-scheduler alert lane are
dead. It also fails closed when a `running` receipt has no parseable start time or exceeds
`--max-running-age` (default one hour), and when corpus lock-owner evidence is malformed or exceeds
`--max-corpus-lock-age` (default five minutes). Fresh work remains visible in the evidence without
making the deployment unhealthy. Alert after one failed sample and require recovery within the
RTO. `--restart` is an explicit
single-gateway recovery action; without it the watchdog is read-only. After restart, require a tick
newer than the restart and run `post_deploy_verify.sh` before declaring recovery.

Only after Docker reports a successful gateway replacement does the watchdog terminalize the old
stale `running` receipts as `indeterminate`, preserving their original identity and recording the
recovery cause and end time. A failed replacement leaves them `running`: the old process may still
exist, so manufacturing a terminal receipt would be false evidence.

Backups exclude PID leases and the tick lock, preventing restored scheduler state from suppressing
work. Restart reconciliation accepts that overdue work may be claimed again and relies on the
write/receipt idempotency rules above. Operators clean terminal orphan run records only after their
receipt and mutation effects have been reconciled.

Active-active is out of scope. It requires an external consensus lease with owner epoch and expiry,
fencing tokens carried into every write, transactional claim persistence, and tests proving an old
owner cannot commit after losing the lease. The current file lock and PID records are not adequate
fencing for separate hosts or isolated container filesystems.
