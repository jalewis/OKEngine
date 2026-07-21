# cron-plus carried patches

OKEngine pins cron-plus as an external dependency in `engine-manifest.yaml`.
Extension config requires two capabilities not present at the current pin:

- job-local `env` values must be applied inside the isolated runner subprocess
  before the wake-gate or agent starts;
- `after:` dependencies must hold a due downstream job until every upstream
  has a fresh successful completion.

`job-env.patch`, `after-ordering.patch`, and the directly tested
`after_ordering.py` policy overlay add those boundary behaviors.
`scripts/install-cron-plus.sh` applies both idempotently and fails loudly when
a future cron-plus pin changes the patch context. When bumping the pin:

1. test whether upstream now supports either capability;
2. remove any absorbed patch and its installer hook;
3. otherwise refresh the remaining patches against the new pin;
4. run `tests/test_cron_plus_deploy.py` and the full suite.

This directory is separate from the root `patches/*.patch` set, which patches
the Hermes runtime itself and has an independently documented patch count.
