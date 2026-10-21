# cron-plus policy-module mirror

OKEngine pins [hermes-cron-plus](https://github.com/jalewis/hermes-cron-plus) in
`engine-manifest.yaml` (`dependencies.cron-plus.pinned_sha`). Until hermes-cron-plus v0.2.0 the
engine carried its scheduler changes here as patches and applied them on every deploy. They now
live upstream, and this directory holds no patches.

What remains is the engine's tested mirror of the plugin's policy modules:

| Module | What it decides |
|---|---|
| `after_ordering.py` | a due job waits until every `after:` upstream has a fresh success |
| `artifact_records.py` | `OKENGINE_ARTIFACT:` declarations: confinement, digest and read-back checks |
| `dst_fold.py` | the DST fall-back fold: an explicit hour fires once, a star hour in both passes (okengine#1055, #1061 [4]) |
| `model_slots.py`, `run_timeout.py` | model concurrency slots and the run time limit |
| `pid_ownership.py` | a finishing runner removes only its own PID record |
| `run_receipts.py` | `completion: run` receipts: governed writes read back before success |
| `run_records.py` | durable per-run records and the per-tick orphan sweep (okengine#1010) |
| `slug_identity_guard.py` | quarantine of separator- or case-equivalent pages a `no_agent` run created (okengine#883) |

They stay here because the engine's unit tests, coverage floor and mutation campaign measure them.
`scripts/install-cron-plus.sh` refuses to deploy a pin whose copy of any of them is not
byte-identical to this mirror, and a pin whose `jobs.py` does not lock the job store through
`jobs.json.lock`, the sidecar `scripts/cron_plus_store.py` takes when a deploy writes the store
(okengine#850).

To change cron-plus behavior:

1. land the change and its tests in hermes-cron-plus;
2. copy any changed policy module here and update the engine's tests;
3. move `pinned_sha` to the new upstream commit in the same engine change;
4. run `tests/test_cron_plus_deploy.py` and the full suite.

This directory is separate from `patches/target-*/`, which patches the Hermes runtime itself and
is baked into the gateway image.
