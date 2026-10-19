# cron-plus carried patches

OKEngine pins cron-plus as an external dependency in `engine-manifest.yaml`.
Extension config requires two capabilities not present at the current pin:

- job-local `env` values must be applied inside the isolated runner subprocess
  before the wake-gate or agent starts;
- `after:` dependencies must hold a due downstream job until every upstream
  has a fresh successful completion.
- a finishing runner must not remove a newer runner's PID record and reopen
  the same-job overlap window.

`job-env.patch`, `after-ordering.patch`, `pid-ownership.patch`, and the directly tested
`after_ordering.py` policy overlay add those boundary behaviors.
`run-records.patch`, `run_records.py`, and `artifact_records.py` also make each
runner invocation durable. A deterministic job may opt into an
`artifact_contract` and print one JSON object per output using
`OKENGINE_ARTIFACT: {"path":"wiki/report.md","operation":"create"}`. The
runner strips those control lines from delivery, confines paths to the mounted
vault/runtime roots, verifies each file and any reported digest, computes a
readback SHA-256, and stores the records under `artifacts` without attributing
model/provider spend. `min_artifacts` fails an otherwise-zero exit when promised
outputs are absent; jobs without the contract retain their existing behavior.
For model-authored `completion: run` contracts, `run_receipts.py` requires at
least one execution-time governed write and reads it back before success. An
optional `required_write_path` contract (with `{date}` expanded from the UTC
run date) pins predictable publications to the artifact that run actually owed.
`jobs-store-lock.patch` moves the job store's flock from the `jobs.json` inode (which every
save replaces) to a sidecar `jobs.json.lock` that is never replaced; `scripts/cron_plus_store.py`
takes the same lock when a deploy writes the store, and records `jobs.deployed.json` so
`post_deploy_verify.sh` can prove the scheduler did not write an older list back (okengine#850).
`reconcile-orphans-on-tick.patch` makes each scheduler tick call
`run_records.reconcile_all_orphaned_runs`, which closes any lane's newest record still reading
`running` whose worker is gone. Before it, a runner killed from outside (by the gateway's MCP
connect cleanup, okengine#1010) stayed `running`, failing the gateway healthcheck, until the same
lane next started: up to a day for a daily lane. Liveness keeps the conservative `_worker_alive`
rule (an uninterpretable pid, another user's pid, or a zombie counts as alive). The record is read
again after the worker is known to be gone, so a run that closed its own record during the probe
keeps its outcome. Only each lane's newest record is read: an older orphan is closed by the newer
run's own start. A sweep failure is logged and never stops the schedule.
`slug-identity-guard.patch` and `slug_identity_guard.py` quarantine a separator- or
case-equivalent page a `no_agent` run introduced. The job is failed only when the page is
attributed to it: it declared the file as an artifact, or no other lane's run record overlaps
its window. A collision written while other lanes ran is quarantined and reported as
`unattributed` instead of failing a bystander (okengine#883).
For unreadable or malformed records, file mtimes include a bounded two-second uncertainty
interval before the run window: kernel timestamps can round a write to before the high-resolution
snapshot, including across a second boundary (okengine#946). Integer nanoseconds preserve the
exact interval edge; older records stay excluded. Readable run timestamps retain full precision.
The uncertain lane remains visible in the quarantine receipt; an explicit artifact declaration
still attributes the collision to its writer.
`dst-fold.patch` and `dst_fold.py` replace the pin's DST fall-back guard in `compute_next_run`.
When clocks fall back, the 01:xx wall-clock hour happens twice. A slot in the second pass is
skipped when the expression names its hour (`30 1 * * *`, `0 1-23/2 * * *`) and kept when the hour
field starts with `*` (`12 * * * *`, `45 */1 * * *`), as in Vixie cron and cronie. The pin's guard
compared the candidate against the claim time instead, so a claim one minute late re-fired a daily
01:xx slot (okengine#1055) while every hourly fixed-minute lane lost its second-pass fire
(okengine#1061 [4]). `croniter_dst_selftest.py --cron-plus` runs the installed plugin's
`compute_next_run` on those cases in the gateway, through the `scheduler-dst` deployment check.
`scripts/install-cron-plus.sh` applies both idempotently and fails loudly when
a future cron-plus pin changes the patch context. When bumping the pin:

1. test whether upstream now supports either capability;
2. remove any absorbed patch and its installer hook;
3. otherwise refresh the remaining patches against the new pin;
4. run `tests/test_cron_plus_deploy.py` and the full suite.

This directory is separate from the root `patches/*.patch` set, which patches
the Hermes runtime itself and has an independently documented patch count.
