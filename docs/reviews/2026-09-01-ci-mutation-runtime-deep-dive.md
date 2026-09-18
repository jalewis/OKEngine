# CI mutation runtime deep dive (2026-09-01)

## Decision

Do not start, retry, schedule, or trigger another okengine CI pipeline until the
recovery conditions in this review are met.  Pipelines 7443, 7470, and 7471 were
canceled.  This review was prepared from repository inspection and read-only
GitLab job/pipeline evidence; it has deliberately not been pushed because a push
would trigger CI.

## Executive finding

The delay is not one stuck test.  The system combines three independent costs:

1. **A permanently red mutation workload.** Mutation coverage has not had a green
   nightly since 2026-08-05 (#612). Recent `mutation-critical` jobs still spend
   3.5--4 hours before reporting a known failure.
2. **A sequential campaign with no valid completion bound.** The nightly job runs
   39 critical targets serially. Their declared per-target session deadlines total
   122 hours, inside an 11-hour campaign budget and 12-hour job timeout.
3. **Runner queue/capacity loss.** Runnable jobs have waited 3--47 minutes, including
   observed periods when one proven executor slot was idle (#652). Ordinary successful
   main pipelines consequently take roughly 1--3 hours before mutation is considered.

The result feels like “days” because expensive red pipelines accumulate and overlap:
new pushes and scheduled runs enter the same constrained runner queue while prior work
is still consuming an executor.

## Evidence

### Recent scheduled mutation jobs

| Pipeline | Job | Result | Job runtime | Queue time |
|---:|---:|---|---:|---:|
| 7433 | 77967 | failed | 3h 37m | 3m 44s |
| 7384 | 77291 | failed | 3h 47m | 21m 41s |
| 7367 | 77027 | failed | 3h 32m | 3m 38s |
| 7359 | 76930 | failed | 4h 01m | 30m 15s |
| 7332 | 76592 | failed | 3h 42m | 12m 16s |
| 7323 | 76406 | failed | 3h 38m | 45m 33s |
| 7207 | 74706 | failed | 8h 57m | 3m 15s |
| 7025 | 71674 | canceled | 8h 44m | 37m 43s |
| 6892 | 69571 | failed | 8h 54m | 41m 13s |

The normal `full-suite` passed in 7--9 minutes in each inspected scheduled pipeline.
Therefore the multi-hour failure is specific to the mutation lane, not a generally
broken test suite.

### Repeated baseline failure is amplified

The manifest has 173 targets, 39 critical targets, and only 25 distinct critical
test commands. Thirteen write-service targets share the same 580-character test
command. The current gate runs a fresh Cosmic Ray baseline for every target and
retries a failed baseline in a second fresh worktree.

In job 77967, 11 write-service targets failed both baseline attempts between
13:00:24 and 13:10:51. The campaign did not fail fast. It recorded those targets as
unrunnable, continued processing, and emitted the final failure at 13:44:51. The
same pattern occurred in the preceding three inspected jobs, with 11 or 12 targets
unmeasured.

The failures are not equivalent to the green full suite. Examples include changing
assertion outcomes in separator convergence and identity merge tests. Different
fresh attempts can fail at different assertions, indicating state/order sensitivity
in the mutation test environment or reduced test selection. This must be reproduced
with the exact CI dependency set before mutation execution is re-enabled.

### Even a clean baseline would still end red

Job 77967 measured 8,421 killed and 1,198 surviving mutants and reported a partial
critical score of 87.55%, below the 90% floor. Other explicit target scores included
80.72%, 79.23%, and 81.70%. Thus eliminating the baseline errors alone would not make
the nightly useful: it would still consume hours and fail its policy floor.

### The configured deadlines do not form an SLO

`mutation-critical` is a single job and does not use the three-way sharding applied
to `mutation-full`. Its 39 targets are sequential; four workers only parallelize
mutants within the current target. The sum of target session deadlines is 122 hours.
The campaign stops starting new targets at 11 hours, while GitLab kills the job at
12 hours. Those numbers guarantee only eventual termination, not completion.

Three full-campaign shards do not by themselves establish a safe design either:
each shard starts four Cosmic Ray HTTP workers. Three simultaneous shards therefore
request twelve mutation workers on a runner where a job container is documented as
four CPU. This can trade elapsed time for CPU contention and increase queue pressure.

### Pipeline duplication is a smaller, real race

The workflow normally suppresses a branch push pipeline when an open merge request
exists. A push made immediately before creating the MR can still create a push
pipeline, followed by a detached MR pipeline for the same commit (7470 and 7471).
This is not the primary multi-hour cause, but it duplicated all automatic gates and
consumed scarce runner capacity. Tooling that creates an MR should open it without a
triggering push race, or cancel the superseded push pipeline immediately.

### Observability is inadequate but not the root cause

Main currently emits no target transition or heartbeat during long mutation work.
MR !983 implements structured starts, 60-second heartbeats, completions, termination
counts, and watcher staleness detection (#653). That makes a wedge diagnosable; it
does not make an invalid four-hour campaign worth running. The MR pipeline was
canceled under this moratorium and must not be restarted merely to validate logging.

## Required redesign

1. **Add a baseline preflight.** Group selected targets by effective test command and
   environment. Run each unique baseline once (with one fresh-checkout retry). If a
   shared baseline fails deterministically, stop before any mutants run and publish a
   small diagnostic artifact. Do not repeat the same broken suite for every module.
2. **Repair and stress the write-service baseline locally.** Reproduce the exact CI
   dependency install and mutation environment. Run the shared suite repeatedly and
   in isolated worktrees. Fix global/module/cache/tmp state until it is deterministic.
3. **Shard the critical lane by measured cost, not manifest position.** Persist recent
   per-target elapsed time and balance shards by weight. Set worker count so total
   workers across concurrent shards stays within the container CPU budget.
4. **Impose a completion budget.** The sum of assigned target budgets per shard must be
   below the job SLO with margin. Reject a manifest whose declared workload cannot fit.
   A target with a six-hour allowance cannot be part of a daily signal unless isolated
   or reduced to a bounded representative scope.
5. **Separate policy remediation from infrastructure validation.** A smoke campaign
   proves the harness. The nightly should not resume until below-floor targets have
   tests or explicit dated waivers; knowingly scheduling a four-hour red result is not
   monitoring.
6. **Fix runner handoff and alerting (#652).** Verify host `concurrent`, runner `limit`,
   `request_concurrency`, and adaptive request concurrency. Require a queued replacement
   to start within 60 seconds while capacity is available.
7. **Retain progress telemetry (#653).** Merge it only after local validation and as
   part of the redesigned lane, not as permission to resume the existing workload.

## Recovery gate

CI mutation jobs may resume only when all of the following are demonstrated without
starting a GitLab pipeline:

- every unique critical baseline passes repeatedly in fresh local worktrees;
- the critical workload is cost-balanced into bounded shards;
- declared shard budgets fit an agreed wall-clock SLO with at least 20% margin;
- aggregate mutation worker count does not exceed the verified CPU allocation;
- the current below-floor policy result has an owned remediation or dated waiver;
- progress/heartbeat tests pass locally;
- runner capacity settings and the idle-slot incident have been inspected on the host.

After that, run one deliberately authorized canary pipeline on an exact SHA. Stop on
baseline-preflight failure; do not enqueue a second run. Success requires complete
artifacts, no unmeasured critical targets, no heartbeat gap over 120 seconds, and an
end-to-end duration within the declared SLO.

## Local remediation status

Implemented locally on `fix/mutation-progress-heartbeats`, without pushing or
starting CI:

- the GitLab pipeline schedule (ID 2) is disabled;
- selected targets are grouped by effective baseline command and preflighted once;
- a failed shared baseline stops before mutation execution and names its full group;
- shards use greedy declared-cost balancing rather than manifest-position round robin;
- campaigns reject selected target deadlines that exceed 80% of their wall-clock budget;
- structured preflight progress records supplement target heartbeats; and
- 111 focused mutation-gate tests pass locally; Ruff and `git diff --check` are clean.

The moratorium remains necessary. The current critical manifest declares 122 target-hours,
so the new budget guard correctly rejects it. Session deadlines now need measurement-backed
reduction and/or the lane needs additional CPU-bounded shards before a canary is eligible.

### Baseline reproduction result

The three assertions named in CI passed in five consecutive fresh detached worktrees. The
complete 14-file write-service command then passed three times on current exact-main ancestry
and three times on the failing job's exact SHA (`5ad972bd`). Finally, the actual Cosmic Ray
baseline invocation, including `ci/mutation_timeout.py` and fresh-checkout isolation, passed
three consecutive times locally with the full development and MCP dependency sets.

This does not prove which host interaction caused CI to fail, but it rules out a deterministic
failure in the selected repository SHA, command, or timeout wrapper. Combined with green full
suites and changing failed assertions between CI retry attempts, the remaining leading cause is
runner-only shared state/resource interference. A canary is therefore ineligible until #652's
host configuration and concurrent-build isolation are inspected; retries would consume hours
without testing a new hypothesis.

### Recovery implementation evidence (2026-09-07)

On `fix/612-655-mutation-recovery`, the complete 40-target critical selection was preflighted
three consecutive times. All 26 unique effective commands passed in fresh detached worktrees on
every run; the 13-target shared write-service command therefore passed once per run, not once per
module. The redesign records observed worker-second costs (GitLab jobs 94246 and 69571), balances
four one-worker shards at 31,845--31,983 declared seconds, and admits at most 33,120 seconds per
shard (80% of the 11.5-hour campaign budget). A manifest-bound proof from the single preflight job
is now required by every shard, and a separate aggregate job is authoritative for the campaign.

The runner's sanitized effective configuration was re-inspected through its container: global
`concurrent = 5`; the project runner has `limit = 1`, both runner entries have
`request_concurrency = 4`, build containers have `cpus = "4"` and `memory = "12g"`, and runner
19.2.0 is online. The critical campaign deliberately starts one mutation worker in each of four
shards, so it cannot exceed that four-CPU allocation even if all shards are runnable together.

## Related work

- #612: critical mutation surface has been unmeasured since 2026-08-05
- #649: external pipeline watcher
- #652: runner leaves proven executor capacity idle
- #653 / MR !983: target progress and heartbeat telemetry
- #583: host-core versus container-CPU worker sizing evidence (closed)
