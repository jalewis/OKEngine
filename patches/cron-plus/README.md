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
`scripts/install-cron-plus.sh` applies both idempotently and fails loudly when
a future cron-plus pin changes the patch context. When bumping the pin:

1. test whether upstream now supports either capability;
2. remove any absorbed patch and its installer hook;
3. otherwise refresh the remaining patches against the new pin;
4. run `tests/test_cron_plus_deploy.py` and the full suite.

This directory is separate from the root `patches/*.patch` set, which patches
the Hermes runtime itself and has an independently documented patch count.
