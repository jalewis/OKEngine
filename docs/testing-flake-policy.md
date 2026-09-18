# Test nondeterminism and quarantine policy

OKEngine does not retry a failed test automatically. A failing test remains a
failed gate until its cause is established and a new run of the exact commit
passes in an environment shown to satisfy the same prerequisites. “Passed on
retry” is evidence of nondeterminism, not evidence that the first result was
wrong.

Classify every intermittent failure as one of:

- **product/test regression** — deterministic failure in code or assertion;
- **test nondeterminism** — ordering, time, randomness, leaked state, or race;
- **environment indeterminate** — a measured prerequisite failed before or
  during the test (egress, service startup, resource ceiling, runner loss);
- **unknown** — insufficient evidence, which remains red.

Bounded retries are allowed only for idempotent prerequisite acquisition before
tests start (for example, an image pull). They must retain every attempt and end
nonzero with an environment diagnosis. Test commands, assertions, and whole CI
jobs are never retry-until-green.

## Quarantine

`config/test-quarantine.yaml` is the only quarantine registry. Every entry names
an exact node ID, owner, tracking issue, reason, first-seen date, expiry no more
than 14 days later, and durable failure evidence. `ci/flake_policy.py` fails CI
on missing, malformed, duplicate, or expired entries and publishes the active
count. Quarantine is triage metadata: it does not add a pytest skip, `xfail`,
`allow_failure`, or exclusion to any required gate. Security, governed-write,
data-integrity, release smoke/E2E, and release performance tests may not be
removed from their blocking lane.

The owner must either fix the cause, replace an invalid test with an equivalent
deterministic detector, or obtain an explicit release waiver before expiry.
Deleting a test is not a disposition unless its requirement is documented as no
longer applicable. Close the tracking issue only after the original failure has
not recurred across three scheduled runs and the registry entry is removed.

## Performance measurements

Absolute latency budgets are authoritative only on the declared cgroup-bounded
release runner, with `performance-environment.json` retained. Shared-host
contention does not turn a budget miss green: it changes the classification to
environment indeterminate, remains a failed release gate, and requires a fresh
run after contention is measured absent. The artifact classifies a run
deterministically as `environment_indeterminate` when cgroup CPU-pressure
`some.avg10` is unavailable or exceeds 20%; the threshold is retained alongside
the raw start/end values. Do not widen a budget from one noisy
sample. A budget change requires a controlled baseline on the same resource
class, three runs, retained raw samples, and review in the tracking issue.
